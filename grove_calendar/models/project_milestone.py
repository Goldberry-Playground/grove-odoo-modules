"""Publish build/sprint `project.milestone` deadlines onto the shared calendar (GOL-4075).

Farm-ops Phase 2 (GOL-4019, s8/s9) puts the footpad regrade build and the
"Grove Odoo" sprint into Odoo as projects carrying `project.milestone` rows. A
milestone that has a real `deadline` should show on the shared Grove calendar as
an all-day `calendar.event`, so a build/sprint date is visible alongside the
farm's other events.

The seed (grove_ops, GOL-4018/4019) owns the projects and milestones; this module
owns the calendar, so the `project.milestone` -> `calendar.event` bridge lives
here rather than being bolted onto the seed. `grove_calendar` depends only on
`project` (core): a milestone is publishable when its project is the "Grove Odoo"
sprint (matched by name) or carries the grove_ops `grove_is_build` flag. That flag
is read defensively through `_fields`, so `grove_calendar` still installs and
tests without grove_ops; the build-project path simply lies dormant until
grove_ops (and its OCA chain, GOL-4072) is installed in QA/prod.

Each publishable milestone with a deadline maps to exactly one `calendar.event`,
linked by `grove_milestone_id` (the idempotency key):

  - a deadline creates/updates an all-day event titled "<project> — <milestone>"
    on the deadline date;
  - moving the deadline moves the event;
  - clearing the deadline (or the project ceasing to be a build/sprint) removes
    the event;
  - re-running creates no duplicate (the event is found by the link, never
    re-created).

The event is created with privacy ``public`` so it shows on the shared internal
calendar, but with no organizer (``user_id = False``) and no Website tag, so the
bridge does NOT force it onto Discord (``_grove_public_domain`` needs a publishing
organizer) or the open-web feed (``_grove_website_domain`` needs the Website tag).
Josh can still opt a specific milestone event into those feeds later through the
existing switches. A dateless (placeholder) milestone publishes nothing.
"""

from datetime import datetime, time

from odoo import api, models

# Sprint projects whose milestones publish even though they are not builds.
# Mirrors scripts/setup_ops.py SPRINT_PROJECT_NAME (GOL-4019, s9). A tuple so a
# second named sprint can join without touching the gating logic.
GROVE_SPRINT_PROJECT_NAMES = ("Grove Odoo",)


class ProjectMilestone(models.Model):
    _inherit = "project.milestone"

    @api.model
    def _grove_project_is_calendar_publishable(self, project):
        """Whether a project's milestone dates belong on the Grove calendar.

        True for a build (grove_ops ``grove_is_build``, read defensively so this
        module stays installable without grove_ops) or a named sprint project.
        """
        if not project:
            return False
        field = project._fields.get("grove_is_build")
        if field and project.grove_is_build:
            return True
        return project.name in GROVE_SPRINT_PROJECT_NAMES

    def _grove_milestone_event_vals(self):
        """calendar.event values mirroring this milestone's all-day marker."""
        self.ensure_one()
        deadline = self.deadline
        # All-day convention (addons/calendar _compute_dates): start/stop are
        # plain datetimes on the deadline date, times dropped for display. 08:00
        # / 18:00 matches Odoo's own all-day mapping; both must land on the same
        # date so start_date == stop_date == deadline (a single-day event).
        return {
            "name": f"{self.project_id.name} — {self.name}",
            "allday": True,
            "start": datetime.combine(deadline, time(8, 0)),
            "stop": datetime.combine(deadline, time(18, 0)),
            "privacy": "public",
            "user_id": False,
            "grove_milestone_id": self.id,
        }

    def _grove_sync_calendar_event(self):
        """Reconcile each milestone in ``self`` with its linked calendar.event.

        Publishable milestone + deadline -> exactly one event (created or
        updated in place). Otherwise -> no event (an existing one is removed).
        Idempotent: the event is located by ``grove_milestone_id``, so a re-run
        never duplicates. Runs sudo because milestones may be written by users
        without calendar write rights, and the event is a system-owned mirror.
        """
        Event = self.env["calendar.event"].sudo()
        for milestone in self:
            existing = Event.search([("grove_milestone_id", "=", milestone.id)])
            publishable = milestone.deadline and milestone._grove_project_is_calendar_publishable(milestone.project_id)
            if publishable:
                vals = milestone._grove_milestone_event_vals()
                if existing:
                    existing.write(vals)
                else:
                    Event.create(vals)
            elif existing:
                existing.unlink()

    @api.model_create_multi
    def create(self, vals_list):
        milestones = super().create(vals_list)
        milestones._grove_sync_calendar_event()
        return milestones

    def write(self, vals):
        res = super().write(vals)
        # Only the fields that change the event (its date, title, or owning
        # project) trigger a re-sync; other milestone edits leave it untouched.
        if {"deadline", "name", "project_id"} & vals.keys():
            self._grove_sync_calendar_event()
        return res

    def unlink(self):
        # Remove the mirror through the ORM (so calendar.event.unlink runs)
        # before the milestones go; grove_milestone_id's ondelete='cascade' is
        # only the belt-and-suspenders for a milestone deleted outside this path.
        self.env["calendar.event"].sudo().search([("grove_milestone_id", "in", self.ids)]).unlink()
        return super().unlink()
