"""Public planner-learning routes (GOL-3549 spec B). Odoo-runtime HttpCase.

Proves the route contract the storefront BFF depends on: 503 under the kill
switch, strict 400s, the per-browser-id rate limit, tester-token gating on
dispute/propose, and the feed's new ``rules`` object. The body validation
itself is unit-tested in test_planner_rules_pure.py; here we prove the routes
are wired to it and to the ORM.
"""

import json
from unittest import mock

from odoo.tests import tagged
from odoo.tests.common import HttpCase, get_db_name

from .common import GroveTaxFixtureMixin

_LEARNING_PARAM = "grove_headless.planner_learning_enabled"


@tagged("grove_headless", "post_install", "-at_install")
class TestPlannerRoutes(GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        # company_id False so the product resolves under whatever website the
        # X-Grove-Tenant header lands us on.
        self.plant = self.env["product.template"].create(
            {"name": "Route Pawpaw", "type": "consu", "list_price": 20.0, "company_id": False}
        )

    def _post(self, path, body):
        headers = {
            "X-Odoo-Database": get_db_name(),
            "X-Grove-Tenant": "goldberry",
            "Content-Type": "application/json",
        }
        return self.url_open(path, data=json.dumps(body).encode(), headers=headers)

    def _get(self, path):
        return self.url_open(path, headers={"X-Odoo-Database": get_db_name(), "X-Grove-Tenant": "goldberry"})

    # ── happy paths ────────────────────────────────────────────────────────────

    def test_signal_creates_a_row(self):
        resp = self._post(
            "/grove/api/v1/planner/signal",
            {"kind": "removed", "product_tmpl_id": self.plant.id, "zone": 6, "browser_id": "route-b1"},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["ok"])
        signal = self.env["grove.planner.signal"].search([("browser_id", "=", "route-b1")])
        self.assertEqual(len(signal), 1)
        self.assertEqual(signal.product_tmpl_id, self.plant)
        self.assertEqual(signal.zone_band, "6-7")  # derived from zone

    def test_suggest_creates_a_queued_suggestion(self):
        resp = self._post(
            "/grove/api/v1/planner/suggest",
            {"name": "Medlar", "stratum": "understory", "note": "underused", "browser_id": "route-b2"},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        suggestion = self.env["grove.planner.suggestion"].search([("name", "=", "Medlar")])
        self.assertEqual(suggestion.status, "queued")
        self.assertEqual(suggestion.by, "public")

    def test_save_creates_a_plan_with_lines(self):
        resp = self._post(
            "/grove/api/v1/planner/save",
            {
                "name": "Route Plan",
                "zone": 6,
                "browser_id": "route-b3",
                "lines": [{"product_tmpl_id": self.plant.id, "qty": 2, "role": "anchor", "stratum": "overstory"}],
            },
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        plan = self.env["grove.planner.plan"].browse(resp.json()["id"])
        self.assertEqual(plan.name, "Route Plan")
        self.assertEqual(len(plan.line_ids), 1)
        self.assertEqual(plan.line_ids.qty, 2)

    # ── guards ───────────────────────────────────────────────────────────────

    def test_bad_body_is_400(self):
        self.assertEqual(self._post("/grove/api/v1/planner/signal", {"kind": "nope"}).status_code, 400)
        self.assertEqual(
            self._post("/grove/api/v1/planner/signal", {"kind": "removed", "browser_id": "x"}).status_code,
            400,  # product required for 'removed'
        )

    def test_unknown_product_is_400(self):
        resp = self._post(
            "/grove/api/v1/planner/signal",
            {"kind": "removed", "product_tmpl_id": 999999, "browser_id": "route-b4"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_kill_switch_returns_503(self):
        self.env["ir.config_parameter"].sudo().set_param(_LEARNING_PARAM, "False")
        try:
            resp = self._post(
                "/grove/api/v1/planner/signal",
                {"kind": "built", "browser_id": "route-b5"},
            )
            self.assertEqual(resp.status_code, 503)
        finally:
            self.env["ir.config_parameter"].sudo().set_param(_LEARNING_PARAM, "True")

    def test_rate_limit_returns_429(self):
        with mock.patch("odoo.addons.grove_headless.controllers.planner.RATE_LIMIT_PER_HOUR", 1):
            first = self._post(
                "/grove/api/v1/planner/signal",
                {"kind": "built", "browser_id": "route-rl"},
            )
            self.assertEqual(first.status_code, 200, first.content)
            second = self._post(
                "/grove/api/v1/planner/signal",
                {"kind": "built", "browser_id": "route-rl"},
            )
            self.assertEqual(second.status_code, 429)

    def test_dispute_requires_a_tester_token(self):
        # No token → 403.
        resp = self._post(
            "/grove/api/v1/planner/dispute",
            {"product_tmpl_id": self.plant.id, "fact": "zone_min", "note": "wrong"},
        )
        self.assertEqual(resp.status_code, 403)
        # Valid token → evidence row named from the tester.
        self.env["grove.planner.tester"].create({"name": "Route Tester", "token": "route-tok"})
        resp = self._post(
            "/grove/api/v1/planner/dispute",
            {"product_tmpl_id": self.plant.id, "fact": "zone_min", "note": "wrong", "tester_token": "route-tok"},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        evidence = self.env["grove.planner.evidence"].search([("product_tmpl_id", "=", self.plant.id)])
        self.assertEqual(evidence.by, "Route Tester")
        self.assertEqual(evidence.source, "tester")

    def test_propose_creates_a_rule_proposal(self):
        self.env["grove.planner.tester"].create({"name": "Prop Tester", "token": "prop-tok"})
        resp = self._post(
            "/grove/api/v1/planner/propose",
            {"key": "priority_weight", "value": 3.5, "tester_token": "prop-tok"},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        proposal = self.env["grove.planner.proposal"].search([("kind", "=", "rule")], limit=1)
        self.assertEqual(proposal.change, {"value": 3.5})
        self.assertTrue(proposal.rule_id)

    def test_catalog_feed_carries_rules(self):
        resp = self._get("/grove/api/v1/planner/catalog")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertIn("rules", body)
        self.assertEqual(body["rules"].get("priority_weight"), 2.0)
        self.assertEqual(len(body["rules"]), 8)
