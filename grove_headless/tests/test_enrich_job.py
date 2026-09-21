"""Perenual enrich-job queue + budgeted drain cron (GOL-2391, spec section B).

Exercises the queue arithmetic end-to-end against a fake Perenual ``get`` (the
provider's injectable seam) and the real Ficus carica fixtures, so no network
and no live key are touched. What must hold:

  * the cron drains queued jobs oldest-first and STOPS at the first job that
    would not fit the remaining daily budget (it does not skip ahead);
  * yesterday's spend never blocks today — the counter key is per-UTC-day;
  * a successful drain fills only the empty fields Perenual is authoritative
    for, records provenance, and caches the species id;
  * HTTP 429 marks the whole day exhausted and requeues the job (never fails);
  * a lookup that keeps erroring is retried once, then fails with the HTTP
    status in the note.

Needs a DB (product.template, grove.enrich.job), so it is listed in
tests/__init__.py AND excluded from pytest in the root conftest (GOL-1936).
"""

import json
import os
from unittest import mock

import requests

from odoo.addons.grove_headless.models.grove_enrich_job import PERENUAL_BUDGET_PARAM
from odoo.addons.grove_headless.services.plant_data.perenual import PerenualProvider
from odoo.tests import TransactionCase, tagged

_FX = os.path.join(os.path.dirname(__file__), "fixtures", "plant_data")


def _fx(name):
    with open(os.path.join(_FX, name)) as fh:
        return json.load(fh)


_LIST = _fx("perenual_ficus_carica_list.json")
_DETAILS = _fx("perenual_ficus_carica_details.json")


class _Resp:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._body


def _ok_get(url, params=None, timeout=None):
    if url.endswith("species-list"):
        return _Resp(_LIST)
    if "/species/details/" in url:
        return _Resp(_DETAILS)
    return _Resp({}, 404)


def _details_429(url, params=None, timeout=None):
    # species-list succeeds; the details call hits the rate limit
    if url.endswith("species-list"):
        return _Resp(_LIST)
    return _Resp({}, 429)


def _details_500(url, params=None, timeout=None):
    if url.endswith("species-list"):
        return _Resp(_LIST)
    return _Resp({"error": "boom"}, 500)


@tagged("post_install", "-at_install")
class TestEnrichJob(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Job = self.env["grove.enrich.job"]
        self.ICP = self.env["ir.config_parameter"].sudo()

    # ── helpers ─────────────────────────────────────────────────────────────
    def _product(self, name="Ficus carica"):
        return self.env["product.template"].create(
            {"name": f"Plant {name}", "grove_botanical_name": name}
        )

    def _queue(self, tmpl):
        return self.Job.create({"product_tmpl_id": tmpl.id, "provider": "perenual"})

    def _run(self, fake_get):
        def fake_provider(job_self, on_call):
            return PerenualProvider(get=fake_get, api_key="TEST", on_call=on_call)

        with mock.patch.object(type(self.Job), "_perenual_provider", fake_provider):
            self.Job._cron_process_enrich_jobs()

    def _counter(self):
        return int(self.ICP.get_param(self.Job._counter_key_today(), 0) or 0)

    # ── tests ───────────────────────────────────────────────────────────────
    def test_success_fills_empty_perenual_fields_and_caches_id(self):
        tmpl = self._product()
        self._queue(tmpl)
        self._run(_ok_get)
        # 2 real HTTP calls (species-list + details)
        self.assertEqual(self._counter(), 2)
        job = self.Job.search([("product_tmpl_id", "=", tmpl.id)])
        self.assertEqual(job.state, "done")
        # Perenual is authoritative-first for zones/watering; hardiness 7–10
        self.assertEqual(tmpl.grove_zone_min, 7)
        self.assertEqual(tmpl.grove_zone_max, 10)
        self.assertEqual(tmpl.grove_watering, "moderate")  # "Average" -> moderate
        # species id cached for the cheaper next fetch
        self.assertEqual(tmpl.grove_perenual_id, 3)
        # provenance stamped for a written field
        self.assertEqual((tmpl.grove_facts_provenance or {}).get("grove_zone_min", {}).get("source"), "perenual")

    def test_does_not_overwrite_existing_fields(self):
        tmpl = self._product()
        tmpl.grove_zone_min = 4  # pre-existing human value
        self._queue(tmpl)
        self._run(_ok_get)
        self.assertEqual(tmpl.grove_zone_min, 4)  # untouched
        self.assertNotIn("grove_zone_min", tmpl.grove_facts_provenance or {})

    def test_drains_to_cap_then_stops(self):
        self.ICP.set_param(PERENUAL_BUDGET_PARAM, "5")  # room for 2 jobs (2 calls each), not a 3rd
        tmpls = [self._product() for _ in range(3)]
        jobs = [self._queue(t) for t in tmpls]
        self._run(_ok_get)
        states = [j.state for j in jobs]
        self.assertEqual(states, ["done", "done", "queued"])  # oldest-first; 3rd left queued
        self.assertEqual(self._counter(), 4)  # 2 jobs × 2 calls, stopped before the 3rd

    def test_cheaper_cached_job_counts_one_call(self):
        tmpl = self._product()
        tmpl.grove_perenual_id = 3  # id already cached -> 1 call (details only)
        self._queue(tmpl)
        self._run(_ok_get)
        self.assertEqual(self._counter(), 1)

    def test_rolls_over_utc_midnight(self):
        # Yesterday's key is fully exhausted; today's counter is fresh, so the
        # job still runs. Proves the per-UTC-day key resets without a job.
        from odoo.addons.grove_headless.services.plant_data import mapping

        self.ICP.set_param(PERENUAL_BUDGET_PARAM, "100")
        self.ICP.set_param(mapping.counter_key("1999-01-01"), "100")
        tmpl = self._product()
        job = self._queue(tmpl)
        self._run(_ok_get)
        self.assertEqual(job.state, "done")
        self.assertEqual(self._counter(), 2)

    def test_429_exhausts_day_and_requeues(self):
        self.ICP.set_param(PERENUAL_BUDGET_PARAM, "100")
        t1, t2 = self._product(), self._product()
        j1, j2 = self._queue(t1), self._queue(t2)
        self._run(_details_429)
        # first job hit 429 -> requeued (not failed); second never attempted
        self.assertEqual(j1.state, "queued")
        self.assertIn("429", j1.note)
        self.assertEqual(j2.state, "queued")
        self.assertEqual(j2.attempts, 0)
        # day marked exhausted: counter clamped to the full budget
        self.assertEqual(self._counter(), 100)

    def test_action_fetch_facts_applies_usda_and_queues_perenual(self):
        from odoo.addons.grove_headless.services.plant_data.mapping import FactValue, PlantFacts

        tmpl = self._product()

        class _FakeUSDA:
            def __init__(self, *a, **k):
                pass

            def lookup(self, name, cached_id=None):
                # grove_layer is USDA-authoritative-first; grove_sun is not
                return PlantFacts(
                    fields={
                        "grove_layer": FactValue("canopy", "usda", "ref"),
                        "grove_sun": FactValue("full", "usda", "ref"),
                    },
                    hints=["USDA matched symbol DIVI5"],
                    resolved_id="DIVI5",
                )

        with mock.patch("odoo.addons.grove_headless.models.product_template.USDAProvider", _FakeUSDA):
            tmpl.action_fetch_facts()
            tmpl.action_fetch_facts()  # idempotent: no duplicate queued job

        self.assertEqual(tmpl.grove_layer, "canopy")  # USDA-authoritative-first, written
        self.assertFalse(tmpl.grove_sun)  # Perenual owns sun -> USDA must not write it
        self.assertEqual(tmpl.grove_usda_symbol, "DIVI5")  # symbol cached
        jobs = self.Job.search([("product_tmpl_id", "=", tmpl.id), ("provider", "=", "perenual")])
        self.assertEqual(len(jobs), 1)  # exactly one Perenual job enqueued

    def test_second_failure_fails_with_http_status(self):
        self.ICP.set_param(PERENUAL_BUDGET_PARAM, "100")
        tmpl = self._product()
        job = self._queue(tmpl)
        # first drain: attempt 1 fails -> requeued
        self._run(_details_500)
        self.assertEqual(job.state, "queued")
        self.assertEqual(job.attempts, 1)
        # second drain: attempt 2 fails -> failed, with the HTTP status recorded
        self._run(_details_500)
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.attempts, 2)
        self.assertIn("500", job.note)
