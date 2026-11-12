"""Staff consult routes ``/grove/api/v1/consult/*`` (spec C.7).

Bearer-auth'd, company-scoped JSON routes the consult dashboard BFF calls. DB-
backed (HttpCase), so listed in tests/__init__.py AND conftest.py's
collect_ignore_glob. Covers the 503 kill switch, the read routes, lead
create/update + stage, the draft-plan skeleton, a section write/approve round
trip, and the travel estimate.
"""

import json
from datetime import datetime, timedelta

from odoo.tests.common import HttpCase, get_db_name, tagged

from .common import GroveTaxFixtureMixin

ENABLED_PARAM = "grove_headless.consult_enabled"


@tagged("post_install", "-at_install")
class TestConsultRoutes(GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        admin = self.env.ref("base.user_admin")
        try:
            self.api_key = (
                self.env["res.users.apikeys"]
                .with_user(admin)
                ._generate("rpc", "grove-consult-test", datetime.now() + timedelta(days=1))
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"could not mint an API key for bearer auth: {exc}")
        websites = self.env["website"]
        website = websites._resolve_tenant_slug("goldberry") or websites.search([], limit=1)
        self.company = website.company_id
        self.env["ir.config_parameter"].sudo().set_param(ENABLED_PARAM, "True")
        self.module = self.env["grove.consult.module"].create(
            {"key": "lifecycle", "name": "Lifecycle", "section_title": "Lifecycle"}
        )
        self.practice = self.env["grove.consult.practice"].create(
            {"key": "silvopasture", "name": "Silvopasture", "row_spacing_m": 12.0, "in_row_spacing_m": 6.0}
        )

    # helpers
    def _headers(self, auth=True):
        headers = {
            "X-Odoo-Database": get_db_name(),
            "X-Grove-Tenant": "goldberry",
            "Content-Type": "application/json",
        }
        if auth:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get(self, path, auth=True):
        self.env.flush_all()
        resp = self.url_open(f"/grove/api/v1/consult{path}", headers=self._headers(auth), timeout=60)
        self.env.invalidate_all()
        return resp

    def _post(self, path, body, auth=True):
        self.env.flush_all()
        resp = self.url_open(
            f"/grove/api/v1/consult{path}",
            data=json.dumps(body).encode(),
            headers=self._headers(auth),
            timeout=60,
        )
        self.env.invalidate_all()
        return resp

    def _lead(self, **vals):
        return (
            self.env["crm.lead"]
            .with_company(self.company)
            .create({"name": "Consult lead", "company_id": self.company.id, "type": "lead", **vals})
        )

    # ── the 503 kill switch ──────────────────────────────────────────────────
    def test_disabled_returns_503(self):
        self.env["ir.config_parameter"].sudo().set_param(ENABLED_PARAM, "False")
        for path in ("/pipeline", "/modules", "/practices", "/travel?zip=26651"):
            resp = self._get(path)
            self.assertEqual(resp.status_code, 503, f"{path}: {resp.text}")

    # ── read routes ──────────────────────────────────────────────────────────
    def test_modules_and_practices(self):
        mods = self._get("/modules")
        self.assertEqual(mods.status_code, 200, mods.text)
        self.assertIn("lifecycle", [m["key"] for m in mods.json()["modules"]])
        pracs = self._get("/practices")
        self.assertEqual(pracs.status_code, 200, pracs.text)
        silvo = next(p for p in pracs.json()["practices"] if p["key"] == "silvopasture")
        self.assertEqual(silvo["row_spacing_m"], 12.0)

    def test_pipeline_lists_only_consult_leads(self):
        self._lead(grove_consult_stage="intake")
        self._lead()  # no consult stage -> excluded
        resp = self._get("/pipeline")
        self.assertEqual(resp.status_code, 200, resp.text)
        leads = resp.json()["leads"]
        self.assertTrue(leads)
        self.assertTrue(all(lead["consult_stage"] for lead in leads))

    # ── lead create / update / stage ─────────────────────────────────────────
    def test_lead_create_and_update(self):
        created = self._post(
            "/lead",
            {
                "name": "New consult",
                "grove_consult_stage": "intake",
                "grove_goals": "food forest",
                "module_ids": [self.module.id],
                "areas": [{"name": "North slope", "practice_id": self.practice.id, "acres": 2.5, "aspect": "N"}],
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        lead = created.json()["lead"]
        self.assertEqual(lead["consult_stage"], "intake")
        self.assertEqual(lead["module_ids"], [self.module.id])
        self.assertEqual(len(lead["areas"]), 1)
        self.assertEqual(lead["areas"][0]["name"], "North slope")

        updated = self._post("/lead", {"id": lead["id"], "grove_existing": "old orchard"})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["lead"]["existing"], "old orchard")

    def test_lead_create_requires_name(self):
        resp = self._post("/lead", {"grove_consult_stage": "intake"})
        self.assertEqual(resp.status_code, 422, resp.text)

    def test_stage_change_and_bad_stage(self):
        lead = self._lead(grove_consult_stage="intake")
        ok = self._post(f"/lead/{lead.id}/stage", {"stage": "design"})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()["lead"]["consult_stage"], "design")
        bad = self._post(f"/lead/{lead.id}/stage", {"stage": "nope"})
        self.assertEqual(bad.status_code, 422, bad.text)

    # ── draft plan + section round trip ──────────────────────────────────────
    def test_plan_skeleton_and_section_approve(self):
        lead = self._lead(grove_consult_stage="design", grove_module_ids=[(6, 0, [self.module.id])])
        plan_resp = self._post(f"/lead/{lead.id}/plan", {})
        self.assertEqual(plan_resp.status_code, 200, plan_resp.text)
        plan = plan_resp.json()["plan"]
        self.assertEqual(plan["version"], 1)
        self.assertEqual(len(plan["section_ids"]), 1)
        key = plan["section_ids"][0]["key"]

        # Write a body, then approve the section.
        body = self._post(f"/lead/{lead.id}/plan/1/section/{key}", {"body": "Draft text"})
        self.assertEqual(body.status_code, 200, body.text)
        self.assertEqual(body.json()["section"]["status"], "edited")
        approved = self._post(f"/lead/{lead.id}/plan/1/section/{key}", {"approve": True})
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["section"]["status"], "approved")

        # A second plan bumps the version.
        plan2 = self._post(f"/lead/{lead.id}/plan", {})
        self.assertEqual(plan2.json()["plan"]["version"], 2)

    # ── travel ───────────────────────────────────────────────────────────────
    def test_travel_estimate(self):
        resp = self._get("/travel?zip=26651")  # farm ZIP -> itself: ~0 miles
        self.assertEqual(resp.status_code, 200, resp.text)
        travel = resp.json()["travel"]
        self.assertLess(travel["miles"], 1.0)
        self.assertEqual(travel["billable_hours"], 0.0)
        self.assertFalse(travel["overnight"])

    def test_travel_requires_zip(self):
        self.assertEqual(self._get("/travel").status_code, 422)

    def test_travel_unknown_zip(self):
        self.assertEqual(self._get("/travel?zip=00000").status_code, 422)

    # ── auth ─────────────────────────────────────────────────────────────────
    def test_unauthenticated_is_rejected(self):
        resp = self._get("/modules", auth=False)
        self.assertNotEqual(resp.status_code, 200)
