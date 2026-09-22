"""Nightly listing-content audit (GOL-2385, spec 2026-09-21 section D).

Covers audit selection, one open activity per product, no-duplicate activities
on re-run, and the Discord payload shape. DB tests — Odoo runner only.
"""

import os
from unittest.mock import patch

from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.fields import Date
from odoo.tests import TransactionCase, tagged

# A fully-complete gated plant (mirrors test_listing_gate._COMPLETE_VALS).
_COMPLETE_VALS = {
    "grove_botanical_name": "Malus domestica",
    "grove_zone_min": 4,
    "grove_zone_max": 8,
    "grove_layer": "canopy",
    "grove_sun": "full",
    "grove_mature_size": "15–20 ft",
    "grove_mature_spread": "12–15 ft",
    "grove_spacing": "15 ft",
    "grove_soil": "well-drained loam",
    "grove_pollination": "Needs a second variety",
    "grove_years_to_fruit": "3–5 years",
    "grove_chill_hours": "800–1000",
    "description_ecommerce": "<p>A hardy heirloom apple for the food forest.</p>",
    "website_description": "<p>Plant in full sun; water weekly the first year.</p>",
    "grove_guide_ready": True,
    "grove_facts_reviewed": True,
}


@tagged("post_install", "-at_install")
class TestListingAudit(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.trees = self.env.ref("grove_headless.categ_trees")
        self.supplies = self.env.ref("grove_headless.categ_supplies")
        self.Template = self.env["product.template"]
        self.todo_type = self.env.ref("mail.mail_activity_data_todo")
        self.model_id = self.env["ir.model"]._get_id("product.template")

    def _complete_plant(self, published=True, **overrides):
        vals = {"name": "Test Apple", "type": "consu", "categ_id": self.trees.id}
        vals.update(_COMPLETE_VALS)
        vals.update(overrides)
        tmpl = self.Template.create(vals)
        if published:
            tmpl.website_published = True
        return tmpl

    def _published_incomplete_plant(self, name="Incomplete Apple", **overrides):
        """A published plant that is later made incomplete.

        Published while complete (the gate blocks publishing an incomplete
        plant), then a required sign-off is cleared — which the gate deliberately
        does NOT re-block, leaving exactly the "published but incomplete" state
        the audit chases.
        """
        tmpl = self._complete_plant(published=True, name=name, **overrides)
        tmpl.grove_facts_reviewed = False
        self.assertFalse(tmpl.grove_listing_complete)
        self.assertTrue(tmpl.website_published)
        return tmpl

    def _open_activities(self, tmpl):
        return self.env["mail.activity"].search(
            [
                ("res_model_id", "=", self.model_id),
                ("res_id", "=", tmpl.id),
                ("activity_type_id", "=", self.todo_type.id),
            ]
        )

    # ── Selection ───────────────────────────────────────────────────────

    def test_selects_only_published_gated_incomplete(self):
        flagged = self._published_incomplete_plant(name="Flagged Apple")

        # Complete + published: nothing to chase.
        self._complete_plant(published=True, name="Complete Apple")
        # Incomplete but unpublished: not selling, so not chased.
        self._complete_plant(published=False, name="Unpublished Apple", grove_facts_reviewed=False)
        # Incomplete + published but gate-exempt (a bundle/supply): not a plant.
        self._complete_plant(published=True, name="Exempt Bundle", grove_facts_reviewed=False, grove_gate_exempt=True)
        # Incomplete + published but outside the Plants tree: not gated.
        self._complete_plant(published=True, name="Supply Item", categ_id=self.supplies.id, grove_facts_reviewed=False)

        selected = self.Template._grove_audit_incomplete_listings()
        self.assertEqual(selected, flagged)

    # ── Activities ──────────────────────────────────────────────────────

    def test_one_activity_per_product_on_responsible_user(self):
        boss = self.env["res.users"].create(
            {"name": "Nursery Boss", "login": "nursery_boss", "email": "boss@example.com"}
        )
        p1 = self._published_incomplete_plant(name="Apple One", responsible_id=boss.id)
        p2 = self._published_incomplete_plant(name="Pear Two")

        self.Template.cron_audit_listing_content()

        a1 = self._open_activities(p1)
        self.assertEqual(len(a1), 1)
        self.assertEqual(a1.user_id, boss)
        self.assertEqual(a1.date_deadline, Date.context_today(self.Template))
        self.assertEqual(len(self._open_activities(p2)), 1)

    def test_no_duplicate_activities_on_rerun(self):
        tmpl = self._published_incomplete_plant()
        self.Template.cron_audit_listing_content()
        self.Template.cron_audit_listing_content()
        self.assertEqual(len(self._open_activities(tmpl)), 1)

    def test_complete_plant_gets_no_activity(self):
        complete = self._complete_plant(published=True, name="All Good Apple")
        self.Template.cron_audit_listing_content()
        self.assertEqual(len(self._open_activities(complete)), 0)

    # ── Discord payload ─────────────────────────────────────────────────

    def test_discord_text_shape(self):
        tmpl = self._published_incomplete_plant(name="Shape Apple")
        text = self.Template._grove_audit_discord_text(tmpl)
        self.assertTrue(text.startswith("1 published listing(s) incomplete: "))
        self.assertIn("Shape Apple — ", text)
        self.assertIn("Facts reviewed", text)  # the cleared sign-off appears in the missing list

    def test_cron_posts_one_discord_message(self):
        self._published_incomplete_plant(name="Post Apple")
        with (
            patch.dict(os.environ, {"DISCORD_OPS_WEBHOOK_URL": "https://discord.example/hook"}),
            patch("odoo.addons.grove_headless.models.product_template.requests") as mock_requests,
        ):
            self.Template.cron_audit_listing_content()
        self.assertEqual(mock_requests.post.call_count, 1)
        _, kwargs = mock_requests.post.call_args
        self.assertIn("content", kwargs["json"])
        self.assertIn("Post Apple", kwargs["json"]["content"])

    def test_cron_no_post_when_webhook_unset(self):
        self._published_incomplete_plant(name="Silent Apple")
        with (
            patch.dict(os.environ, {"DISCORD_OPS_WEBHOOK_URL": ""}),
            patch("odoo.addons.grove_headless.models.product_template.requests") as mock_requests,
        ):
            self.Template.cron_audit_listing_content()
        mock_requests.post.assert_not_called()
