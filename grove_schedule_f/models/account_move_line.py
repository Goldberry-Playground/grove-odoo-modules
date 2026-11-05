from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .account_bank_statement_line import _grove_is_agent

ENTERPRISE_PLAN_NAME = "Enterprise"
ENTERPRISE_EXEMPT_ACCOUNT_TYPES = frozenset(
    {"asset_cash", "liability_credit_card", "asset_receivable", "liability_payable"}
)
ENTERPRISE_VALUES = frozenset({"farm", "nursery", "personal"})
OWNER_DRAW_CODE = "3100"


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    grove_enterprise = fields.Selection(
        [("farm", "Farm"), ("nursery", "Nursery"), ("personal", "Personal")],
        string="Enterprise",
        compute="_compute_grove_enterprise",
        store=True,
    )

    def _grove_enterprise_entries(self):
        """Return [(analytic account name, percentage)] for this line's Enterprise-plan analytic accounts."""
        self.ensure_one()
        entries = []
        distribution = self.analytic_distribution or {}
        ids = {int(i) for key in distribution for i in str(key).split(",") if i}
        accounts = {a.id: a for a in self.env["account.analytic.account"].sudo().browse(ids).exists()}
        for key, pct in distribution.items():
            for raw in str(key).split(","):
                account = accounts.get(int(raw)) if raw else None
                if account and account.plan_id.name == ENTERPRISE_PLAN_NAME:
                    entries.append((account.name, pct))
        return entries

    @api.depends("analytic_distribution")
    def _compute_grove_enterprise(self):
        for line in self:
            shares = {}
            for name, pct in line._grove_enterprise_entries():
                if name.lower() in ENTERPRISE_VALUES:
                    shares[name.lower()] = shares.get(name.lower(), 0.0) + pct
            line.grove_enterprise = max(shares, key=shares.get) if shares else False

    def _grove_enterprise_exempt(self):
        self.ensure_one()
        if self.account_id.account_type in ENTERPRISE_EXEMPT_ACCOUNT_TYPES:
            return True
        if self.move_id.origin_payment_id:
            return True  # payment entries: liquidity, outstanding and partner lines are never enterprise-tagged
        journal, company = self.journal_id, self.company_id
        exempt = journal.suspense_account_id
        if journal.type in ("bank", "cash"):
            # A bank or cash journal's default account is its liquidity side; a sale or purchase journal's default
            # is the income or expense account itself, which must carry an enterprise.
            exempt |= journal.default_account_id
        method_lines = journal.inbound_payment_method_line_ids | journal.outbound_payment_method_line_ids
        exempt |= method_lines.payment_account_id
        for fname in (
            "account_journal_payment_debit_account_id",
            "account_journal_payment_credit_account_id",
            "transfer_account_id",
        ):
            if fname in company._fields:
                exempt |= company[fname]
        return self.account_id in exempt

    @api.constrains("account_id", "analytic_distribution", "journal_id")
    def _check_grove_enterprise(self):
        for line in self:
            if not line.journal_id.grove_enterprise_required:
                continue
            if line.display_type not in ("product", False) or line._grove_enterprise_exempt():
                continue
            entries = line._grove_enterprise_entries()
            if len(entries) != 1 or abs(entries[0][1] - 100.0) > 0.01 or entries[0][0].lower() not in ENTERPRISE_VALUES:
                raise ValidationError(
                    f"Line {line.name or line.account_id.display_name}: "
                    "pick an enterprise (Farm, Nursery or Personal) before it can be saved."
                )
            is_personal = entries[0][0].lower() == "personal"
            is_draw = line.account_id.with_company(line.company_id).code == OWNER_DRAW_CODE
            if is_personal != is_draw:
                raise ValidationError("Personal spending posts only to 3100 Owner draw.")

    @api.model_create_multi
    def create(self, vals_list):
        if _grove_is_agent(self.env):
            default_move_id = self.env.context.get("default_move_id")
            move_ids = {vals.get("move_id", default_move_id) for vals in vals_list} - {False, None}
            if self.env["account.move"].browse(move_ids)._grove_agent_locked():
                raise AccessError(self.env._("Grove Agents cannot change posted entries."))
        return super().create(vals_list)

    def write(self, vals):
        if _grove_is_agent(self.env):
            target = self.env["account.move"].browse([vals["move_id"]]) if vals.get("move_id") else self.move_id
            if (target | self.move_id)._grove_agent_locked():
                raise AccessError(self.env._("Grove Agents cannot change posted entries."))
        return super().write(vals)

    def reconcile(self):
        if self and _grove_is_agent(self.env):
            raise AccessError(self.env._("Grove Agents cannot reconcile journal items."))
        return super().reconcile()

    def remove_move_reconcile(self):
        # Odoo calls this on every line unlink; only an actual unreconcile is refused.
        if _grove_is_agent(self.env) and (self.matched_debit_ids or self.matched_credit_ids):
            raise AccessError(self.env._("Grove Agents cannot unreconcile journal items."))
        return super().remove_move_reconcile()
