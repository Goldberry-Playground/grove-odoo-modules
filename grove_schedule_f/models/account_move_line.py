from odoo import api, fields, models
from odoo.exceptions import ValidationError

ENTERPRISE_PLAN_NAME = "Enterprise"
ENTERPRISE_EXEMPT_ACCOUNT_TYPES = frozenset({"asset_cash", "liability_credit_card"})
OWNER_DRAW_CODE = "3100"


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    grove_enterprise = fields.Selection(
        [("farm", "Farm"), ("nursery", "Nursery"), ("personal", "Personal")],
        string="Enterprise",
        compute="_compute_grove_enterprise",
        store=True,
    )

    def _grove_enterprise_shares(self):
        """Return {selection value: summed percentage} for this line's Enterprise-plan analytic accounts."""
        self.ensure_one()
        shares = {}
        distribution = self.analytic_distribution or {}
        ids = {int(i) for key in distribution for i in str(key).split(",") if i}
        accounts = {a.id: a for a in self.env["account.analytic.account"].sudo().browse(ids).exists()}
        for key, pct in distribution.items():
            for raw in str(key).split(","):
                account = accounts.get(int(raw)) if raw else None
                if account and account.plan_id.name == ENTERPRISE_PLAN_NAME:
                    value = account.name.lower()
                    shares[value] = shares.get(value, 0.0) + pct
        return shares

    @api.depends("analytic_distribution")
    def _compute_grove_enterprise(self):
        allowed = {"farm", "nursery", "personal"}
        for line in self:
            shares = {k: v for k, v in line._grove_enterprise_shares().items() if k in allowed}
            line.grove_enterprise = max(shares, key=shares.get) if shares else False

    def _grove_enterprise_exempt(self):
        self.ensure_one()
        if self.account_id.account_type in ENTERPRISE_EXEMPT_ACCOUNT_TYPES:
            return True
        journal, company = self.journal_id, self.company_id
        exempt = journal.default_account_id | journal.suspense_account_id
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
            shares = line._grove_enterprise_shares()
            if abs(sum(shares.values()) - 100.0) > 0.01:
                raise ValidationError(
                    f"Line {line.name or line.account_id.display_name}: "
                    "pick an enterprise (Farm, Nursery or Personal) before posting."
                )
            is_personal = line.grove_enterprise == "personal"
            is_draw = line.account_id.code == OWNER_DRAW_CODE
            if is_personal != is_draw:
                raise ValidationError("Personal spending posts only to 3100 Owner draw.")
