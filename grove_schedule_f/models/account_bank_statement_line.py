from odoo import fields, models
from odoo.exceptions import AccessError

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
AGENT_WRITABLE_STATEMENT_FIELDS = frozenset(PROPOSAL_FIELDS | {"grove_proposal_state", "message_main_attachment_id"})


def _grove_is_agent(env) -> bool:
    """True for a Grove Agents user acting as themselves (sudo/system code is never restricted)."""
    return not env.su and env.user.has_group(AGENT_GROUP)


class AccountBankStatementLine(models.Model):
    _inherit = "account.bank.statement.line"

    grove_proposed_account_id = fields.Many2one("account.account", string="Proposed Account", copy=False)
    grove_proposed_enterprise_id = fields.Many2one(
        "account.analytic.account",
        string="Proposed Enterprise",
        domain="[('plan_id.name', '=', 'Enterprise')]",
        copy=False,
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

    def write(self, vals):
        if _grove_is_agent(self.env):
            if set(vals) - AGENT_WRITABLE_STATEMENT_FIELDS:
                raise AccessError(self.env._("Grove Agents can only write proposal fields."))
            if PROPOSAL_FIELDS & set(vals) and "accepted" in self.mapped("grove_proposal_state"):
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
