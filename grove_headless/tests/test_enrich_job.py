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
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import TransactionCase, tagged

_FX = os.path.join(os.path.dirname(__file__), "fixtures", "plant_data")


def _fx(name):
    with open(os.path.join(_FX, name)) as fh:
        return json.load(fh)


_LIST = _fx("perenual_ficus_carica_list.json")
_DETAILS = _fx("perenual_ficus_carica_details.json")
_ZIJU_LIST = _fx("perenual_ziziphus_jujuba_list.json")  # no exact match (GOL-2542)


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


def _ziju_nomatch_get(url, params=None, timeout=None):
    # Perenual returns only near relatives of jujube -> no exact binomial match,
    # so the lookup yields no fields and the USDA fallback stands (GOL-2542).
    if url.endswith("species-list"):
        return _Resp(_ZIJU_LIST)
    return _Resp({}, 404)


def _details_plan_gated(url, params=None, timeout=None):
    # species-list resolves; the details call is 429 with an "Upgrade Plan"
    # body — a per-species paywall, not day-exhaustion.
    if url.endswith("species-list"):
        return _Resp(_LIST)
    return _Resp(
        {"X-Response": "[429] Please Upgrade Plan - https://perenual.com/subscription-api-pricing - Sorry"},
        429,
    )


@tagged("post_install", "-at_install")
class TestEnrichJob(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.Job = self.env["grove.enrich.job"]
        self.ICP = self.env["ir.config_parameter"].sudo()

    # ── helpers ─────────────────────────────────────────────────────────────
    def _product(self, name="Ficus carica"):
        return self.env["product.template"].create({"name": f"Plant {name}", "grove_botanical_name": name})

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
        # the note reports the fields ACTUALLY written (== provenance keys), not
        # the provider's raw proposals (GOL-2512 cosmetic).
        written = sorted(tmpl.grove_facts_provenance or {})
        self.assertIn(f"Perenual applied: {', '.join(written)}.", job.note)

    def test_does_not_overwrite_existing_fields(self):
        tmpl = self._product()
        tmpl.grove_zone_min = 4  # pre-existing human value
        self._queue(tmpl)
        self._run(_ok_get)
        self.assertEqual(tmpl.grove_zone_min, 4)  # untouched
        # GOL-2543: a bare manual write now stamps `human` provenance, and it is
        # protected *because* it is human-owned — not by the old emptiness
        # heuristic. Perenual (a machine source) can never overwrite it.
        self.assertEqual(
            (tmpl.grove_facts_provenance or {}).get("grove_zone_min", {}).get("source"),
            "human",
        )

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

    def test_plan_gated_species_fails_alone_and_queue_continues(self):
        # A paid-plan (429 "Upgrade Plan") species must NOT slam the day counter
        # or break the drain: it fails alone, the day is left un-exhausted, and
        # the next queued job is still attempted. (Regression: GOL-2512 — one
        # paywalled species used to head-of-line-block the whole catalog.)
        self.ICP.set_param(PERENUAL_BUDGET_PARAM, "100")
        t1, t2 = self._product(), self._product()
        j1, j2 = self._queue(t1), self._queue(t2)
        self._run(_details_plan_gated)
        # head-of-line job failed (terminal, not requeued) with the right reason
        self.assertEqual(j1.state, "failed")
        self.assertIn("plan-gated", j1.note.lower())
        # the drain CONTINUED to the younger job (would stay "queued" under the bug)
        self.assertEqual(j2.state, "failed")
        # counter reflects only the real calls made (2 per job), never the budget
        self.assertEqual(self._counter(), 4)
        # resolved id cached so a re-press skips the wasted species-list call
        self.assertEqual(t1.grove_perenual_id, 3)

    def test_unkeyed_leaves_jobs_queued(self):
        # No PERENUAL_API_KEY -> the cron must NOT drain the backlog into a
        # no-op "done"; jobs stay queued so they enrich once the key lands.
        tmpl = self._product()
        job = self._queue(tmpl)

        def unkeyed_provider(job_self, on_call):
            return PerenualProvider(get=_ok_get, api_key="", on_call=on_call)

        with mock.patch.object(type(self.Job), "_perenual_provider", unkeyed_provider):
            self.Job._cron_process_enrich_jobs()
        self.assertEqual(job.state, "queued")  # untouched, not "done"
        self.assertEqual(self._counter(), 0)  # no HTTP call attempted

    def test_action_fetch_facts_applies_usda_and_queues_perenual(self):
        from odoo.addons.grove_headless.services.plant_data.mapping import FactValue, PlantFacts

        tmpl = self._product()

        class _FakeUSDA:
            def __init__(self, *a, **k):
                pass

            def lookup(self, name, cached_id=None):
                # grove_layer is USDA-preferred; grove_sun is Perenual-preferred
                # but USDA now fills it as a fallback (GOL-2542).
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

        self.assertEqual(tmpl.grove_layer, "canopy")  # USDA-preferred, written
        # GOL-2542: USDA fills the Perenual-preferred field as a fallback, tagged usda
        self.assertEqual(tmpl.grove_sun, "full")
        self.assertEqual((tmpl.grove_facts_provenance or {}).get("grove_sun", {}).get("source"), "usda")
        self.assertEqual(tmpl.grove_usda_symbol, "DIVI5")  # symbol cached
        jobs = self.Job.search([("product_tmpl_id", "=", tmpl.id), ("provider", "=", "perenual")])
        self.assertEqual(len(jobs), 1)  # exactly one Perenual job enqueued

    # ── GOL-2542: USDA fallback for Perenual-preferred fields ────────────────
    def _fetch_with_fake_usda(self, tmpl, fields):
        """Run action_fetch_facts with a stubbed USDA returning ``fields``."""
        from odoo.addons.grove_headless.services.plant_data.mapping import PlantFacts

        class _FakeUSDA:
            def __init__(self, *a, **k):
                pass

            def lookup(self, name, cached_id=None):
                return PlantFacts(fields=dict(fields), hints=["USDA matched symbol TEST"], resolved_id="TEST")

        with mock.patch("odoo.addons.grove_headless.models.product_template.USDAProvider", _FakeUSDA):
            tmpl.action_fetch_facts()

    def test_usda_fallback_then_perenual_overwrites(self):
        # USDA fills the Perenual-preferred fields (source usda) + zones from the
        # minimum temperature (usda_temp); Perenual then overwrites those exact
        # values when it drains, because it is the preferred source.
        from odoo.addons.grove_headless.services.plant_data.mapping import FactValue

        tmpl = self._product()  # "Ficus carica" -> matches the Perenual fixture
        self._fetch_with_fake_usda(
            tmpl,
            {
                "grove_sun": FactValue("partial", "usda", "usda://TEST"),
                "grove_watering": FactValue("high", "usda", "usda://TEST"),
                "grove_zone_min": FactValue(4, "usda_temp", "usda://TEST"),
                "grove_zone_max": FactValue(9, "usda_temp", "usda://TEST"),
            },
        )
        # USDA fallback landed with usda / usda_temp provenance
        self.assertEqual(tmpl.grove_sun, "partial")
        self.assertEqual((tmpl.grove_facts_provenance or {})["grove_sun"]["source"], "usda")
        self.assertEqual((tmpl.grove_facts_provenance or {})["grove_zone_min"]["source"], "usda_temp")

        self._run(_ok_get)  # Perenual drains: Ficus carica -> sun full, watering moderate, zone 7–10
        self.assertEqual(tmpl.grove_sun, "full")  # perenual overwrote the usda fallback
        self.assertEqual(tmpl.grove_watering, "moderate")
        self.assertEqual(tmpl.grove_zone_min, 7)  # perenual hardiness beat usda_temp
        self.assertEqual(tmpl.grove_zone_max, 10)
        self.assertEqual((tmpl.grove_facts_provenance or {})["grove_sun"]["source"], "perenual")
        self.assertEqual((tmpl.grove_facts_provenance or {})["grove_zone_min"]["source"], "perenual")

    def test_perenual_failure_leaves_usda_fallback_standing(self):
        # Jujube: USDA supplies the values, Perenual has no exact match, so the
        # USDA fallback must NOT be blanked (Josh's QA-191 concern).
        from odoo.addons.grove_headless.services.plant_data.mapping import FactValue

        tmpl = self._product("Ziziphus jujuba")
        self._fetch_with_fake_usda(
            tmpl,
            {
                "grove_sun": FactValue("partial", "usda", "usda://TEST"),
                "grove_watering": FactValue("moderate", "usda", "usda://TEST"),
                "grove_zone_min": FactValue(6, "usda_temp", "usda://TEST"),
                "grove_zone_max": FactValue(11, "usda_temp", "usda://TEST"),
            },
        )
        self._run(_ziju_nomatch_get)  # Perenual: no exact match -> nothing applied
        job = self.Job.search([("product_tmpl_id", "=", tmpl.id)])
        self.assertEqual(job.state, "done")  # a clean no-match is a completed lookup
        # USDA values stand, provenance unchanged
        self.assertEqual(tmpl.grove_sun, "partial")
        self.assertEqual(tmpl.grove_watering, "moderate")
        self.assertEqual(tmpl.grove_zone_min, 6)
        self.assertEqual((tmpl.grove_facts_provenance or {})["grove_zone_min"]["source"], "usda_temp")

    def test_human_value_never_overwritten(self):
        # A field a human set by hand is protected from both the USDA pass and
        # the Perenual drain. A manual form edit stamps `human` provenance
        # (GOL-2543) so the guard recognises it as human-owned.
        from odoo.addons.grove_headless.services.plant_data.mapping import FactValue

        tmpl = self._product()  # "Ficus carica"
        tmpl.grove_sun = "shade"  # manual form edit -> stamped human provenance
        self.assertEqual((tmpl.grove_facts_provenance or {})["grove_sun"]["source"], "human")
        self._fetch_with_fake_usda(tmpl, {"grove_sun": FactValue("partial", "usda", "usda://TEST")})
        self.assertEqual(tmpl.grove_sun, "shade")  # USDA did not clobber the human value
        self.assertEqual((tmpl.grove_facts_provenance or {})["grove_sun"]["source"], "human")

        self._run(_ok_get)  # Perenual would say "full"
        self.assertEqual(tmpl.grove_sun, "shade")  # still protected
        self.assertEqual((tmpl.grove_facts_provenance or {})["grove_sun"]["source"], "human")

    def test_human_correction_of_machine_field_never_overwritten(self):
        # GOL-2543 regression: USDA fallback fills a Perenual-preferred field, a
        # human then corrects it in the form; the Perenual drain (a strictly
        # preferred source) must NOT overwrite the human's correction, nor blank
        # it. Before the fix, the field still carried source "usda" after the
        # human edit, so source_outranks("perenual","usda") let Perenual clobber.
        from odoo.addons.grove_headless.services.plant_data.mapping import FactValue

        tmpl = self._product()  # "Ficus carica" -> matches the Perenual fixture
        # 1. USDA fallback fills grove_sun (machine provenance "usda").
        self._fetch_with_fake_usda(tmpl, {"grove_sun": FactValue("partial", "usda", "usda://TEST")})
        self.assertEqual(tmpl.grove_sun, "partial")
        self.assertEqual((tmpl.grove_facts_provenance or {})["grove_sun"]["source"], "usda")
        # 2. Human corrects the machine-filled field in the form.
        tmpl.grove_sun = "shade"
        self.assertEqual((tmpl.grove_facts_provenance or {})["grove_sun"]["source"], "human")
        # 3. Perenual drains and would say "full" (strictly preferred over usda).
        self._run(_ok_get)
        self.assertEqual(tmpl.grove_sun, "shade")  # human correction survived
        self.assertEqual((tmpl.grove_facts_provenance or {})["grove_sun"]["source"], "human")

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
