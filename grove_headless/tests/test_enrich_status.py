"""Enrichment / draft status UX on the product form (GOL-2541). DB tests.

Josh could not tell from the product form whether Fetch-facts / Request-draft
were running, how long they would take, or whether they succeeded/failed. These
tests pin the human status text for each state, the click-time notification
payloads, the stalled-draft flag, and the Retry re-enqueue.
"""

import os
from datetime import timedelta
from unittest import mock

from odoo import fields
from odoo.addons.grove_headless.models.grove_enrich_job import PERENUAL_BUDGET_PARAM
from odoo.addons.grove_headless.models.product_template import CONTENT_DRAFTER_LOGIN_PARAM
from odoo.addons.grove_headless.services.plant_data import mapping
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestEnrichStatus(GroveTaxFixtureMixin, TransactionCase):
    def _tmpl(self, **vals):
        base = {"name": "Test Hazelnut", "type": "consu", "grove_botanical_name": "Corylus americana"}
        base.update(vals)
        return self.env["product.template"].create(base)

    def _job(self, tmpl, state="queued", note=False):
        return self.env["grove.enrich.job"].create(
            {"product_tmpl_id": tmpl.id, "provider": "perenual", "state": state, "note": note}
        )

    def _prov(self, source, *names, at="2026-09-23T20:15:00"):
        return {n: {"source": source, "ref": "X", "at": at} for n in names}

    # ── USDA status ─────────────────────────────────────────────────────
    def test_usda_not_run(self):
        t = self._tmpl()
        self.assertEqual(t.grove_enrich_status_usda, "Not run — press Fetch facts.")

    def test_usda_filled_uses_persisted_note(self):
        t = self._tmpl(grove_usda_fetch_note="Filled 3 field(s) at 2026-09-23 20:15 (symbol COAM3).")
        self.assertIn("Filled 3 field(s)", t.grove_enrich_status_usda)
        self.assertIn("COAM3", t.grove_enrich_status_usda)

    def test_usda_badge_fields_from_provenance(self):
        t = self._tmpl(grove_facts_provenance=self._prov("usda", "grove_zone_min", "grove_sun"))
        self.assertEqual(t.grove_enrich_usda_fields, "grove_sun, grove_zone_min")

    # ── Perenual status ─────────────────────────────────────────────────
    def test_perenual_not_run(self):
        t = self._tmpl()
        self.assertIn("Not run", t.grove_enrich_status_perenual)

    def test_perenual_running(self):
        t = self._tmpl()
        self._job(t, state="running")
        self.assertEqual(t.grove_enrich_status_perenual, "Running now…")

    def test_perenual_done_counts_provenance_fields(self):
        t = self._tmpl(grove_facts_provenance=self._prov("perenual", "grove_soil", "grove_watering"))
        self._job(t, state="done")
        self.assertIn("Done — filled 2 field(s)", t.grove_enrich_status_perenual)

    def test_perenual_failed_shows_note_and_flag(self):
        t = self._tmpl()
        self._job(t, state="failed", note="Failed after 2 attempts — HTTP 500")
        self.assertTrue(t.grove_enrich_failed)
        self.assertIn("HTTP 500", t.grove_enrich_status_perenual)
        self.assertIn("HTTP 500", t.grove_enrich_failed_note)

    def test_perenual_queued_waiting_for_key(self):
        # No PERENUAL_API_KEY in the test env → provider unconfigured.
        t = self._tmpl()
        self._job(t, state="queued")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PERENUAL_API_KEY", None)
            self.assertIn("Waiting for API key", t.grove_enrich_status_perenual)

    def test_perenual_queued_position_when_keyed(self):
        t = self._tmpl()
        self._job(t, state="queued")
        with mock.patch.dict(os.environ, {"PERENUAL_API_KEY": "test-key"}):
            self.assertIn("Queued — position 1 of 1", t.grove_enrich_status_perenual)

    def test_perenual_budget_exhausted_when_keyed(self):
        t = self._tmpl()
        self._job(t, state="queued")
        Job = self.env["grove.enrich.job"].sudo()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param(PERENUAL_BUDGET_PARAM, "5")
        icp.set_param(Job._counter_key_today(), "5")
        with mock.patch.dict(os.environ, {"PERENUAL_API_KEY": "test-key"}):
            self.assertIn("Budget exhausted", t.grove_enrich_status_perenual)

    def test_hints_display_from_persisted_hints(self):
        t = self._tmpl(grove_enrich_hints={"usda": ["USDA zone from min temp"], "perenual": ["spacing hint"]})
        self.assertIn("USDA: USDA zone from min temp", t.grove_enrich_hints_display)
        self.assertIn("PERENUAL: spacing hint", t.grove_enrich_hints_display)

    # ── Draft status ────────────────────────────────────────────────────
    def test_draft_none(self):
        t = self._tmpl()
        self.assertEqual(t.grove_draft_status, "No draft requested.")
        self.assertFalse(t.grove_draft_stale)

    def test_draft_requested_recent_not_stale_but_warns_unconfigured(self):
        t = self._tmpl(grove_draft_state="requested", grove_draft_requested_at=fields.Datetime.now())
        # No drafter login configured → the status warns it will not be picked up.
        self.env["ir.config_parameter"].sudo().set_param(CONTENT_DRAFTER_LOGIN_PARAM, "")
        self.assertIn("Requested at", t.grove_draft_status)
        self.assertIn("Routine not configured", t.grove_draft_status)
        self.assertFalse(t.grove_draft_stale)

    def test_draft_requested_old_is_stale(self):
        old = fields.Datetime.now() - timedelta(minutes=90)
        t = self._tmpl(grove_draft_state="requested", grove_draft_requested_at=old)
        self.assertTrue(t.grove_draft_stale)

    def test_draft_requested_without_timestamp_is_stale(self):
        # A pre-existing 'requested' product (no timestamp) must not be invisible.
        t = self._tmpl(grove_draft_state="requested")
        self.assertTrue(t.grove_draft_stale)

    def test_draft_drafted_shows_by_and_at(self):
        t = self._tmpl()
        t.write({"grove_draft_state": "drafted"})  # write() stamps at/by
        self.assertTrue(t.grove_drafted_at)
        self.assertEqual(t.grove_drafted_by, self.env.user)
        self.assertIn("Drafted at", t.grove_draft_status)
        self.assertIn(self.env.user.name, t.grove_draft_status)

    def test_routine_active_when_drafter_signed_in(self):
        # In Odoo 19 res.users.login_date is a non-stored related field
        # (related='log_ids.create_date', a create_uid-keyed one2many), so writing
        # it directly is a no-op. A recent login is recorded by creating a
        # res.users.log owned by the user, exactly as real XML-RPC auth does via
        # _update_last_login(). The ORM forces create_uid to the acting user, and
        # only group_system may create res.users.log, so we seed as the drafter.
        drafter = self.env["res.users"].create(
            {
                "name": "Content Drafter",
                "login": "content-drafter-test",
                "group_ids": [(4, self.env.ref("base.group_system").id)],
            }
        )
        self.env["res.users.log"].with_user(drafter).create({})
        # login_date is related through a create_uid-keyed one2many, whose inverse
        # cache the ORM does not auto-refresh; re-read it from the DB.
        drafter.invalidate_recordset(["login_date", "log_ids"])
        self.assertTrue(drafter.login_date)  # related field now reflects the log
        self.env["ir.config_parameter"].sudo().set_param(CONTENT_DRAFTER_LOGIN_PARAM, "content-drafter-test")
        t = self._tmpl(grove_draft_state="requested", grove_draft_requested_at=fields.Datetime.now())
        # Active routine → no warning appended.
        self.assertNotIn("Routine not", t.grove_draft_status)

    def test_routine_not_active_when_drafter_never_signed_in(self):
        # Drafter user exists but has never authenticated (no res.users.log),
        # so login_date is empty → the request would sit forever unnoticed.
        self.env["res.users"].create({"name": "Content Drafter", "login": "content-drafter-test"})
        self.env["ir.config_parameter"].sudo().set_param(CONTENT_DRAFTER_LOGIN_PARAM, "content-drafter-test")
        t = self._tmpl(grove_draft_state="requested", grove_draft_requested_at=fields.Datetime.now())
        self.assertIn("Routine not active", t.grove_draft_status)

    # ── Click-time notifications ────────────────────────────────────────
    def test_request_draft_notification_warns_when_unconfigured(self):
        t = self._tmpl(
            grove_facts_provenance=self._prov("usda", "grove_soil"),
            grove_soil="loam",
        )
        self.env["ir.config_parameter"].sudo().set_param(CONTENT_DRAFTER_LOGIN_PARAM, "")
        action = t.action_request_draft()
        self.assertEqual(action["params"]["type"], "warning")
        self.assertIn("Routine not configured", action["params"]["message"])
        self.assertEqual(t.grove_draft_state, "requested")
        self.assertTrue(t.grove_draft_requested_at)

    def test_fetch_facts_notification_summarises(self):
        t = self._tmpl()
        facts = mapping.PlantFacts(
            fields={"grove_zone_min": mapping.FactValue(value=3, source="usda", ref="COAM3")},
            resolved_id="COAM3",
        )
        with mock.patch("odoo.addons.grove_headless.models.product_template.USDAProvider") as prov_cls:
            prov_cls.return_value.lookup.return_value = facts
            action = t.action_fetch_facts()
        msg = action["params"]["message"]
        self.assertIn("USDA:", msg)
        self.assertIn("Perenual queued", msg)
        self.assertFalse(action["params"]["sticky"])
        # a job was actually enqueued
        self.assertTrue(self.env["grove.enrich.job"].search([("product_tmpl_id", "=", t.id)]))

    # ── Retry ───────────────────────────────────────────────────────────
    def test_retry_reenqueues_failed(self):
        t = self._tmpl()
        self._job(t, state="failed", note="boom")
        action = t.action_retry_enrich()
        self.assertEqual(action["params"]["type"], "success")
        queued = self.env["grove.enrich.job"].search([("product_tmpl_id", "=", t.id), ("state", "=", "queued")])
        self.assertEqual(len(queued), 1, "a fresh queued job should be created")

    def test_retry_noop_when_nothing_failed(self):
        t = self._tmpl()
        self._job(t, state="done")
        action = t.action_retry_enrich()
        self.assertEqual(action["params"]["type"], "warning")
