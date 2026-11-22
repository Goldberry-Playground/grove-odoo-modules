"""End-to-end order attribution on a real sale.order (GOL-4077).

Two layers, both against a real DB (so this file is in tests/__init__.py AND
conftest.py's collect_ignore_glob — it needs the Odoo runtime):

  * TransactionCase — drive ``_create_draft_order`` and assert that a checkout
    carrying an ``attribution`` object stamps the dedicated fields (UTM values
    lowercased), that an order with NO attribution still succeeds, and that
    source/medium/campaign mirror onto Odoo's built-in utm.mixin m2o fields when
    the build exposes them. The build-exposes-utm assertion is the live
    confirmation of the open question on GOL-4077.
  * HttpCase — the ``/grove/api/v1/orders/attribution`` read endpoint groups the
    tenant's paid orders by source and returns per-bucket revenue for the Sunday
    insights routine.
"""

import json
from datetime import datetime, timedelta

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import HttpCase, get_db_name, new_test_user

from .common import GroveTaxFixtureMixin

ATTRIBUTION = {
    "utm_source": "Instagram",
    "utm_medium": "Email",
    "utm_campaign": "Fall-Sale-2026",
    "utm_content": "Hero-CTA",
    "utm_term": "Pawpaw",
    "landing_page": "/shop/pawpaw?ref=home",
    "referrer": "https://l.instagram.com/",
    "ga_client_id": "GA1.2.1234567890.1699999999",
    "marketing_tags_eligible": True,
}


@tagged("post_install", "-at_install")
class TestOrderAttributionWrite(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.product = self.env["product.product"].create(
            {"name": "Pawpaw (bareroot)", "type": "consu", "list_price": 22.0}
        )
        # A product defaults to the "potted" shipping tier, which only sells
        # inside the leafed window (through Oct 15) — so a pickup checkout built
        # any later in the year is rejected by the potted-season gate in
        # _create_draft_order before attribution ever runs. Tag it bareroot so
        # the checkout succeeds year-round and these tests exercise attribution,
        # not the season gate.
        self.product.product_tmpl_id.grove_shipping_tier = "bareroot"

    def _website(self):
        return self.env.ref("grove_headless.website_nursery")

    def _payload(self, attribution=None):
        payload = {
            "contact": {"name": "Attr Buyer", "email": "attr@example.com", "phone": "3045551212"},
            "items": [{"variant_id": self.product.id, "quantity": 1}],
            "fulfillment": "pickup",  # pickup skips the ship-to / shipping gates
        }
        if attribution is not None:
            payload["attribution"] = attribution
        return payload

    def _create(self, attribution=None):
        return grove_main._create_draft_order(self._website(), self.env, self._payload(attribution))

    def test_attribution_is_stamped_and_lowercased(self):
        order, error = self._create(ATTRIBUTION)
        self.assertIsNone(error)
        self.assertTrue(order)
        self.assertEqual(order.grove_utm_source, "instagram")
        self.assertEqual(order.grove_utm_medium, "email")
        self.assertEqual(order.grove_utm_campaign, "fall-sale-2026")
        self.assertEqual(order.grove_utm_content, "hero-cta")
        self.assertEqual(order.grove_utm_term, "pawpaw")
        self.assertEqual(order.grove_landing_page, "/shop/pawpaw?ref=home")
        self.assertEqual(order.grove_referrer, "https://l.instagram.com/")
        self.assertEqual(order.grove_ga_client_id, "GA1.2.1234567890.1699999999")
        self.assertTrue(order.grove_marketing_tags_eligible)

    def test_order_without_attribution_still_succeeds(self):
        order, error = self._create(None)
        self.assertIsNone(error)
        self.assertTrue(order)
        self.assertFalse(order.grove_utm_source)
        self.assertFalse(order.grove_utm_medium)
        self.assertFalse(order.grove_marketing_tags_eligible)

    def test_malformed_attribution_never_fails_the_order(self):
        order, error = self._create(
            {"utm_source": "<script>", "utm_medium": "email", "utm_campaign": "a" * 999, "bogus": {"x": 1}}
        )
        self.assertIsNone(error)
        self.assertTrue(order)
        # The bad source + over-length campaign are dropped; the good medium lands.
        self.assertFalse(order.grove_utm_source)
        self.assertEqual(order.grove_utm_medium, "email")
        self.assertFalse(order.grove_utm_campaign)

    def test_build_exposes_builtin_utm_fields(self):
        """Confirm the open GOL-4077 question: this Grove v19 build carries the
        utm.mixin m2o fields on sale.order (website_sale + crm + mass_mailing all
        depend on `utm`). If this ever fails, the built-in mirror is dormant and
        only the dedicated fields carry attribution."""
        fields = self.env["sale.order"]._fields
        for m2o in ("source_id", "medium_id", "campaign_id"):
            self.assertIn(m2o, fields, f"sale.order is missing built-in UTM field {m2o}")

    def test_source_medium_campaign_mirror_onto_builtin_utm(self):
        order, _error = self._create(ATTRIBUTION)
        # Reuses existing utm records case-insensitively (the seeded "Email"
        # medium absorbs our lowercased "email") and creates otherwise.
        self.assertEqual((order.source_id.name or "").lower(), "instagram")
        self.assertEqual((order.medium_id.name or "").lower(), "email")
        self.assertEqual((order.campaign_id.name or "").lower(), "fall-sale-2026")


@tagged("post_install", "-at_install")
class TestOrderAttributionReport(GroveTaxFixtureMixin, HttpCase):
    def _key_for(self, user):
        try:
            return (
                self.env["res.users.apikeys"]
                .with_user(user)
                ._generate("rpc", "grove-attribution-report-test", datetime.now() + timedelta(days=1))
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"could not mint an API key for bearer auth: {exc}")

    def _paid_order(self, source, qty):
        website = self.env.ref("grove_headless.website_nursery")
        company = website.company_id
        product = self.env["product.product"].create({"name": f"Attr {source}", "type": "consu", "list_price": 10.0})
        partner = self.env["res.partner"].create({"name": f"Buyer {source}", "email": f"{source}@e.com"})
        order = (
            self.env["sale.order"]
            .with_company(company)
            .create(
                {
                    "partner_id": partner.id,
                    "company_id": company.id,
                    "order_line": [(0, 0, {"product_id": product.id, "product_uom_qty": qty})],
                    "grove_utm_source": source,
                    "grove_checkout_status": "paid",
                }
            )
        )
        return order

    def test_report_groups_paid_orders_by_source_with_revenue(self):
        google_a = self._paid_order("google", 1)
        google_b = self._paid_order("google", 2)
        instagram = self._paid_order("instagram", 1)
        # A draft (unpaid) order must be excluded from the revenue read.
        draft = self._paid_order("google", 5)
        draft.grove_checkout_status = False
        self.env.flush_all()

        user = new_test_user(
            self.env, login="attr_report_key", groups="base.group_user,sales_team.group_sale_salesman_all_leads"
        )
        today = datetime.now().date().isoformat()
        resp = self.url_open(
            f"/grove/api/v1/orders/attribution?group_by=source&from={today}&to={today}",
            headers={
                "X-Odoo-Database": get_db_name(),
                "X-Grove-Tenant": "nursery",
                "Authorization": f"Bearer {self._key_for(user)}",
            },
            allow_redirects=False,
        )
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.content)
        self.assertEqual(body["group_by"], "source")
        buckets = {row["source"]: row for row in body["groups"]}
        self.assertEqual(set(buckets), {"google", "instagram"})
        self.assertEqual(buckets["google"]["order_count"], 2)
        self.assertEqual(buckets["instagram"]["order_count"], 1)
        expected_google = round(google_a.amount_total + google_b.amount_total, 2)
        self.assertAlmostEqual(buckets["google"]["revenue"], expected_google, places=2)
        self.assertAlmostEqual(buckets["instagram"]["revenue"], round(instagram.amount_total, 2), places=2)
        # Top line totals exclude the draft order entirely.
        self.assertEqual(body["order_count"], 3)

    def test_report_rejects_a_bad_group_by(self):
        user = new_test_user(self.env, login="attr_bad_group", groups="base.group_user")
        resp = self.url_open(
            "/grove/api/v1/orders/attribution?group_by=carrier",
            headers={
                "X-Odoo-Database": get_db_name(),
                "X-Grove-Tenant": "nursery",
                "Authorization": f"Bearer {self._key_for(user)}",
            },
            allow_redirects=False,
        )
        self.assertEqual(resp.status_code, 400)
