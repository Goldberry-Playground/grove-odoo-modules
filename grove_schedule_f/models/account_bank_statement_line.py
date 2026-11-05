from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

AGENT_GROUP = "grove_schedule_f.group_grove_agent"
PROPOSAL_FIELDS = frozenset(
    {
        "grove_proposed_account_id",
        "grove_proposed_enterprise_id",
        "grove_proposed_model_id",
        "grove_proposal_note",
        "grove_proposal_uncertain",
    }
)
REVIEW_THRESHOLD_PARAM = "grove_schedule_f.review_threshold"
ALLOWED_PROPOSAL_ACCOUNT_TYPES = frozenset(
    {
        "income",
        "income_other",
        "expense",
        "expense_direct_cost",
        "expense_depreciation",
        "equity",
        "asset_current",
        "asset_fixed",
        "liability_current",
        "liability_credit_card",
    }
)
AGENT_WRITABLE_STATEMENT_FIELDS = frozenset(PROPOSAL_FIELDS | {"grove_proposal_state", "message_main_attachment_id"})


def _grove_is_agent(env) -> bool:
    """True for a Grove Agents user acting as themselves (sudo/system code is never restricted)."""
    return not env.su and env.user.has_group(AGENT_GROUP)


class AccountBankStatementLine(models.Model):
    _inherit = "account.bank.statement.line"

    grove_proposed_account_id = fields.Many2one(
        "account.account", string="Proposed Account", copy=False, check_company=True
    )
    grove_proposed_enterprise_id = fields.Many2one(
        "account.analytic.account",
        string="Proposed Enterprise",
        domain="[('plan_id.name', '=', 'Enterprise')]",
        copy=False,
        check_company=True,
    )
    grove_proposed_model_id = fields.Many2one("account.reconcile.model", string="Proposed Model", copy=False)
    grove_proposal_note = fields.Text(string="Proposal Note", copy=False)
    grove_proposal_uncertain = fields.Boolean(string="Proposal Uncertain", copy=False)
    grove_proposal_state = fields.Selection(
        [("none", "None"), ("proposed", "Proposed"), ("accepted", "Accepted"), ("rejected", "Rejected")],
        string="Proposal State",
        default="none",
        copy=False,
    )

    grove_needs_review = fields.Boolean(
        string="Needs Review",
        compute="_compute_grove_needs_review",
        search="_search_grove_needs_review",
    )

    @api.model
    def _grove_review_threshold(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(REVIEW_THRESHOLD_PARAM, "500")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 500.0

    @api.depends("is_reconciled", "grove_proposal_uncertain", "grove_proposed_model_id", "amount")
    def _compute_grove_needs_review(self):
        threshold = self._grove_review_threshold()
        for line in self:
            line.grove_needs_review = not line.is_reconciled and bool(
                line.grove_proposal_uncertain or not line.grove_proposed_model_id or abs(line.amount) >= threshold
            )

    @api.model
    def _search_grove_needs_review(self, operator, value):
        if operator not in ("=", "!="):
            raise UserError(self.env._("Unsupported operator for Needs Review."))
        threshold = self._grove_review_threshold()
        domain = [
            "&",
            ("is_reconciled", "=", False),
            "|",
            "|",
            ("grove_proposal_uncertain", "=", True),
            ("grove_proposed_model_id", "=", False),
            "|",
            ("amount", ">=", threshold),
            ("amount", "<=", -threshold),
        ]
        wanted = bool(value) == (operator == "=")
        if wanted:
            return domain
        return ["!", *domain]

    def write(self, vals):
        if _grove_is_agent(self.env):
            if set(vals) - AGENT_WRITABLE_STATEMENT_FIELDS:
                raise AccessError(self.env._("Grove Agents can only write proposal fields."))
            if (PROPOSAL_FIELDS | {"grove_proposal_state"}) & set(vals) and "accepted" in self.mapped(
                "grove_proposal_state"
            ):
                raise AccessError(self.env._("Proposal already accepted."))
            if vals.get("grove_proposal_state", "proposed") != "proposed":
                raise AccessError(self.env._("Grove Agents can only mark a proposal as proposed."))
        if PROPOSAL_FIELDS & set(vals) and "grove_proposal_state" not in vals:
            open_lines = self.filtered(lambda ln: ln.grove_proposal_state != "accepted")
            accepted = self - open_lines
            result = True
            if open_lines:
                result = super(AccountBankStatementLine, open_lines).write({**vals, "grove_proposal_state": "proposed"})
            if accepted:
                result = super(AccountBankStatementLine, accepted).write(vals)
            return result
        return super().write(vals)

    def reconcile_bank_line(self):
        if _grove_is_agent(self.env):
            raise AccessError(self.env._("Grove Agents cannot reconcile bank lines."))
        return super().reconcile_bank_line()

    def action_grove_accept_proposal(self):
        self.ensure_one()
        if _grove_is_agent(self.env):
            raise AccessError(self.env._("Grove Agents cannot accept proposals."))
        account = self.grove_proposed_account_id
        enterprise = self.grove_proposed_enterprise_id
        if self.grove_proposal_state != "proposed" or not account:
            raise UserError(self.env._("There is no proposal to accept on this line."))
        if self.journal_id.grove_enterprise_required and not enterprise:
            raise UserError(self.env._("Pick an enterprise before accepting."))
        journal = self.journal_id
        if (
            self.company_id not in account.company_ids
            or account.account_type not in ALLOWED_PROPOSAL_ACCOUNT_TYPES
            or account in (journal.suspense_account_id, journal.default_account_id)
        ):
            raise UserError(self.env._("Proposed account is not usable for this line."))
        if enterprise and (enterprise.plan_id.name != "Enterprise" or enterprise.company_id != self.company_id):
            raise UserError(self.env._("Proposed enterprise is not usable for this line."))
        info = self._default_reconcile_data()
        suspense = [entry for entry in info["data"] if entry["kind"] == "suspense"]
        if len(suspense) != 1:
            raise UserError(self.env._("This line has no open amount to reconcile."))
        entry = suspense[0]
        entry["account_id"] = [account.id, account.display_name]
        entry["kind"] = "other"
        if enterprise:
            entry["analytic_distribution"] = {str(enterprise.id): 100}
        info["can_reconcile"] = True
        self.reconcile_data_info = info
        self.reconcile_bank_line()
        self.grove_proposal_state = "accepted"
        self.move_id.message_post(
            body=self.env._(
                "Accepted: %(account)s / %(enterprise)s",
                account=account.display_name,
                enterprise=enterprise.name or self.env._("no enterprise"),
            )
        )
        return True

    def action_grove_reject_proposal(self):
        self.ensure_one()
        if _grove_is_agent(self.env):
            raise AccessError(self.env._("Grove Agents cannot reject proposals."))
        if self.grove_proposal_state != "proposed":
            raise UserError(self.env._("There is no proposal to reject on this line."))
        self.grove_proposal_state = "rejected"
        self.move_id.message_post(body=self.env._("Proposal rejected."))
        return True
