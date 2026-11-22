"""project.milestone -> calendar.event bridge (GOL-4075).

Odoo-runtime TransactionCase (runs under the install-smoke-test, not pytest).
grove_ops is NOT installed here, so these exercise the sprint-by-name path (the
"Grove Odoo" project) and plain non-publishable projects; the grove_is_build
branch is a defensive field read covered by the publishability helper.
"""

from datetime import date

from odoo.tests.common import TransactionCase, tagged


@tagged("grove_calendar", "post_install", "-at_install")
class MilestoneCalendarCase(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Project = self.env["project.project"]
        self.Milestone = self.env["project.milestone"]
        self.Event = self.env["calendar.event"]
        self.sprint = self.Project.create({"name": "Grove Odoo", "allow_milestones": True})
        self.other = self.Project.create({"name": "Some other project", "allow_milestones": True})
        self.deadline = date(2026, 12, 2)

    def _events_for(self, milestone):
        return self.Event.search([("grove_milestone_id", "=", milestone.id)])

    def _milestone(self, project, name="Train #6", deadline=None):
        vals = {"name": name, "project_id": project.id}
        if deadline is not None:
            vals["deadline"] = deadline
        return self.Milestone.create(vals)

    def test_sprint_milestone_with_deadline_creates_one_all_day_event(self):
        milestone = self._milestone(self.sprint, deadline=self.deadline)
        events = self._events_for(milestone)
        self.assertEqual(len(events), 1, "exactly one event per dated milestone")
        self.assertTrue(events.allday)
        self.assertEqual(events.name, "Grove Odoo — Train #6")
        self.assertEqual(events.start_date, self.deadline)
        self.assertEqual(events.stop_date, self.deadline)
        # Respects the visibility model: shared/public but no organizer and no
        # Website tag, so it is not force-pushed to Discord or the web feed.
        self.assertEqual(events.privacy, "public")
        self.assertFalse(events.user_id)
        public = self.Event.search(self.Event._grove_public_domain(3650))
        self.assertNotIn(events, public, "milestone mirror must not reach Discord")

    def test_dateless_milestone_publishes_nothing(self):
        milestone = self._milestone(self.sprint, deadline=None)
        self.assertFalse(milestone.deadline)
        self.assertFalse(self._events_for(milestone))

    def test_non_sprint_non_build_project_publishes_nothing(self):
        milestone = self._milestone(self.other, deadline=self.deadline)
        self.assertFalse(self._events_for(milestone))

    def test_moving_the_deadline_moves_the_event(self):
        milestone = self._milestone(self.sprint, deadline=self.deadline)
        event = self._events_for(milestone)
        self.assertEqual(event.start_date, self.deadline)
        moved = date(2026, 12, 9)
        milestone.deadline = moved
        # Same single event, new date (not a second one).
        self.assertEqual(self._events_for(milestone), event)
        self.assertEqual(event.start_date, moved)
        self.assertEqual(event.stop_date, moved)

    def test_clearing_the_deadline_removes_the_event(self):
        milestone = self._milestone(self.sprint, deadline=self.deadline)
        self.assertTrue(self._events_for(milestone))
        milestone.deadline = False
        self.assertFalse(self._events_for(milestone))

    def test_rerun_creates_no_duplicate(self):
        milestone = self._milestone(self.sprint, deadline=self.deadline)
        self.assertEqual(len(self._events_for(milestone)), 1)
        # Re-reconciling (e.g. a later seed run) must not add a second event.
        milestone._grove_sync_calendar_event()
        milestone._grove_sync_calendar_event()
        self.assertEqual(len(self._events_for(milestone)), 1)

    def test_renaming_milestone_updates_event_title(self):
        milestone = self._milestone(self.sprint, name="Train #6", deadline=self.deadline)
        event = self._events_for(milestone)
        milestone.name = "Train #7"
        self.assertEqual(event.name, "Grove Odoo — Train #7")

    def test_deleting_milestone_removes_event(self):
        milestone = self._milestone(self.sprint, deadline=self.deadline)
        event = self._events_for(milestone)
        self.assertTrue(event.exists())
        milestone.unlink()
        self.assertFalse(event.exists())

    def test_project_becoming_sprint_publishes_its_dated_milestones(self):
        # A milestone on a non-publishable project: no event yet.
        milestone = self._milestone(self.other, deadline=self.deadline)
        self.assertFalse(self._events_for(milestone))
        # Rename the project to the sprint name: the project write hook
        # re-reconciles its milestones and the event appears.
        self.other.name = "Grove Odoo"
        event = self._events_for(milestone)
        self.assertEqual(len(event), 1)
        self.assertEqual(event.name, "Grove Odoo — Train #6")
        # Renaming it away again removes the event.
        self.other.name = "Parked project"
        self.assertFalse(self._events_for(milestone))

    def test_publishable_helper_reads_grove_is_build_defensively(self):
        # grove_ops is not installed, so the field is absent; the helper must
        # not raise and must fall through to the sprint-name check.
        self.assertNotIn("grove_is_build", self.Project._fields)
        self.assertFalse(self.Milestone._grove_project_is_calendar_publishable(self.other))
        self.assertTrue(self.Milestone._grove_project_is_calendar_publishable(self.sprint))
        self.assertFalse(self.Milestone._grove_project_is_calendar_publishable(self.Project.browse()))
