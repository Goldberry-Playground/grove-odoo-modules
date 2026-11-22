"""`project.project` hook keeping milestone calendar events in sync (GOL-4075).

A project's milestone events depend on two project-level facts: whether it is a
build/sprint (so its milestones publish at all) and its name (which is half of
every event title and the sprint's very identity). When either changes, the
project's milestones re-reconcile their calendar events. Milestone date edits
are handled on `project.milestone` itself; this covers the project-level edits.
"""

from odoo import models


class ProjectProject(models.Model):
    _inherit = "project.project"

    def write(self, vals):
        res = super().write(vals)
        # grove_is_build is a grove_ops field; checking the key is safe even when
        # that module is absent (it can never appear in vals then). A rename or a
        # build-flag flip re-evaluates every milestone's event (title + whether
        # it publishes at all).
        if {"grove_is_build", "name"} & vals.keys():
            self.milestone_ids._grove_sync_calendar_event()
        return res
