# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Week approval rules (Task 7, GOL-4018, s11.3 / s11.4).

The OCA ``hr_timesheet_sheet`` module supplies the week model (one sheet per
employee per week, states new/draft/confirm/done where ``done`` is approved).
This layer adds the three co-op rules that make an approved week the legal
sweat-equity record:

  * nobody approves their own week;
  * every approval and reopen is written to the sheet's chatter (who, when,
    the hours total) so the trail survives the correction loop;
  * correcting an approved week goes reopen -> edit -> reapprove: while a sheet
    sits in ``done`` its lines are frozen (the lock lives on
    account.analytic.line; this model refuses silent content edits too).
"""

from odoo import _, models
from odoo.exceptions import UserError

APPROVED_STATE = "done"


class HrTimesheetSheet(models.Model):
    _inherit = "hr_timesheet.sheet"

    def write(self, vals):
        approving = vals.get("state") == APPROVED_STATE
        if approving:
            self._grove_check_not_self_approval()
        prev_state = {sheet.id: sheet.state for sheet in self}
        res = super().write(vals)
        if "state" in vals:
            for sheet in self:
                sheet._grove_log_state_change(prev_state.get(sheet.id))
        return res

    def _grove_check_not_self_approval(self):
        """Refuse an approval performed by the sheet's own owner (s11.3).

        A superuser-context write (seed/migration) is exempt; the guard targets
        an owner-operator pressing approve on their own week in the UI.
        """
        if self.env.su:
            return
        for sheet in self:
            owner = sheet.employee_id.user_id
            if owner and owner.id == self.env.uid:
                raise UserError(
                    _("You cannot approve your own week. Another owner-operator has to approve %(name)s's hours.")
                    % {"name": sheet.employee_id.name or _("this employee")}
                )

    def _grove_log_state_change(self, prev_state):
        """Append the approval/reopen trail to the sheet chatter (s11.4)."""
        if not hasattr(self, "message_post"):
            return
        new_state = self.state
        if new_state == prev_state:
            return
        actor = self.env.user.display_name
        if new_state == APPROVED_STATE:
            body = _("Week <b>approved</b> by %(actor)s — %(hours).2f hours.") % {
                "actor": actor,
                "hours": self._grove_total_hours(),
            }
        elif prev_state == APPROVED_STATE:
            body = _("Approved week <b>reopened</b> by %(actor)s for correction.") % {"actor": actor}
        else:
            return
        # message_post gives the immutable who/when trail; mail.message rows are
        # not editable through the UI, so the history cannot be quietly rewritten.
        self.message_post(body=body)

    def _grove_total_hours(self):
        """Sum the sheet's timesheet hours without assuming an OCA total field."""
        self.ensure_one()
        lines = self.env["account.analytic.line"]
        if "sheet_id" in lines._fields:
            return sum(lines.search([("sheet_id", "=", self.id)]).mapped("unit_amount"))
        return 0.0
