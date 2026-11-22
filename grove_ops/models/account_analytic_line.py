# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Approved hours are locked (Task 7, GOL-4018, s11.3 / s11.4).

Once a week is approved, its timesheet lines are the legal record and cannot be
edited or deleted in place. Corrections go through the sheet: reopen the week
(``done`` -> ``draft``), fix the hours, reapprove. The ``sheet_id`` link is the
OCA ``hr_timesheet_sheet`` field; the guard degrades to a no-op if a port drops
it, so it can never crash an install — the non-self-approval rule still stands.
"""

from odoo import _, models
from odoo.exceptions import UserError

APPROVED_STATE = "done"

# The content fields of a timesheet line. Editing any of these on an approved
# line is what the lock forbids; system recomputes that touch other fields
# (links, display helpers) are left alone so the lock never blocks the ORM.
LOCKED_FIELDS = frozenset(
    {"unit_amount", "amount", "date", "name", "project_id", "task_id", "account_id", "employee_id", "user_id"}
)


class AccountAnalyticLine(models.Model):
    _inherit = "account.analytic.line"

    def _grove_locked_lines(self):
        if "sheet_id" not in self._fields:
            return self.browse()
        return self.filtered(lambda line: line.sheet_id and line.sheet_id.state == APPROVED_STATE)

    def write(self, vals):
        if LOCKED_FIELDS.intersection(vals) and not self.env.su and self._grove_locked_lines():
            raise UserError(_("These hours are in an approved week and are locked. Reopen the week to correct them."))
        return super().write(vals)

    def unlink(self):
        locked = self._grove_locked_lines()
        if locked and not self.env.su:
            raise UserError(_("These hours are in an approved week and are locked. Reopen the week to remove them."))
        return super().unlink()
