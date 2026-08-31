"""product.availability transition events (GOL-1896).

A sold-out product kept advertising "In stock" on the ISR /shop grid for up to a
minute. These tests prove the Odoo side now emits a signed `product.availability`
webhook the moment on-hand crosses zero (or sale_ok / website_published flip),
reusing the grove.publish.event machinery — and that it emits on the *transition*
(one event per template), never raises into the write, and caps a bulk storm.

Runs in Odoo's TransactionCase harness (the install-smoke-test CI job). The
transaction never commits, so the commit-time flush is invoked directly via
`_flush_availability_events()` — exactly what `cr.precommit` would call.
"""

import os
from unittest import mock

from odoo.addons.grove_headless.models import grove_publish, grove_publish_event
from odoo.tests import TransactionCase, tagged

WEBHOOK_ENV = {
    "GROVE_PUBLISH_WEBHOOK_URL_GOLDBERRY": "https://goldberry.test/api/webhooks/publish",
    "GROVE_PUBLISH_WEBHOOK_SECRET_GOLDBERRY": "unit-test-tenant-secret",
}

AVAILABILITY = grove_publish_event.EVENT_PRODUCT_AVAILABILITY


class _FakePost:
    """requests.post stand-in that records deliveries (mirrors test_publish_event)."""

    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text
        self.calls = []

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return mock.Mock(status_code=self.status_code, text=self.text)


@tagged("grove_headless", "publish_event", "post_install", "-at_install")
class TestAvailabilityEvents(TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")  # → 'goldberry' tenant
        self.warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.company.id)], limit=1)
        self.location = self.warehouse.lot_stock_id
        self.Event = self.env["grove.publish.event"]
        # Drain any transaction-scoped scratch left by a prior test on this cursor.
        self._reset_buffer()

    # ── helpers ──────────────────────────────────────────────────────────
    def _reset_buffer(self):
        data = self.env.cr.precommit.data
        data.pop(grove_publish_event._AVAIL_BEFORE_KEY, None)
        data.pop(grove_publish_event._AVAIL_REGISTERED_KEY, None)

    def _make_product(self, name, **vals):
        return self.env["product.product"].create(
            {"name": name, "type": "consu", "is_storable": True, "list_price": 25.0, **vals}
        )

    def _add_stock(self, product, delta):
        self.env["stock.quant"]._update_available_quantity(product, self.location, delta)
        product.invalidate_recordset(["qty_available", "free_qty"])

    def _flush(self):
        """Invoke the commit-time flush directly (no commit in a TransactionCase)."""
        self.Event._flush_availability_events()

    def _patch_env(self, env=None):
        return mock.patch.dict(os.environ, env or WEBHOOK_ENV, clear=False)

    def _patch_post(self, fake):
        return mock.patch.object(grove_publish.requests, "post", fake)

    def _availability_events(self, template):
        return self.Event.search([("product_tmpl_id", "=", template.id), ("event_type", "=", AVAILABILITY)])

    def _stock_then_reset(self, product, qty):
        """Bring a product to `qty` on hand and clear the resulting transition so
        the test starts from a known, already-flushed baseline (no webhook env →
        nothing is delivered, the scratch is just drained)."""
        self._add_stock(product, qty)
        self._flush()
        self._reset_buffer()

    # ── on-hand transitions ──────────────────────────────────────────────
    def test_sellout_emits_one_availability_event(self):
        product = self._make_product("Pawpaw 'Shenandoah'")
        self._stock_then_reset(product, 5)

        fake = _FakePost()
        with self._patch_env(), self._patch_post(fake):
            self._add_stock(product, -5)  # last unit sells → 0
            self._flush()

        events = self._availability_events(product.product_tmpl_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events.state, "delivered")
        self.assertEqual(events.tenant, "goldberry")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0]["headers"][grove_publish.EVENT_HEADER], AVAILABILITY)

    def test_restock_emits(self):
        product = self._make_product("Persimmon")
        self._stock_then_reset(product, 0)

        fake = _FakePost()
        with self._patch_env(), self._patch_post(fake):
            self._add_stock(product, 3)  # 0 → in stock
            self._flush()

        self.assertEqual(len(self._availability_events(product.product_tmpl_id)), 1)

    def test_decrement_without_crossing_zero_emits_nothing(self):
        product = self._make_product("Serviceberry")
        self._stock_then_reset(product, 10)

        fake = _FakePost()
        with self._patch_env(), self._patch_post(fake):
            self._add_stock(product, -4)  # 10 → 6, still in stock
            self._flush()

        self.assertFalse(self._availability_events(product.product_tmpl_id))
        self.assertEqual(len(fake.calls), 0)

    def test_multi_line_order_emits_one_event_per_template(self):
        """One order selling out three species → three events, not thirty."""
        products = [self._make_product(f"Species {i}") for i in range(3)]
        for product in products:
            self._stock_then_reset(product, 1)

        fake = _FakePost()
        with self._patch_env(), self._patch_post(fake):
            for product in products:  # each crosses 1 → 0 in the same transaction
                self._add_stock(product, -1)
            self._flush()

        for product in products:
            self.assertEqual(len(self._availability_events(product.product_tmpl_id)), 1)
        self.assertEqual(len(fake.calls), 3)

    def test_multiple_writes_same_template_coalesce_to_one_event(self):
        """Two on-hand writes that net to a single crossing emit once."""
        product = self._make_product("Jujube")
        self._stock_then_reset(product, 5)

        fake = _FakePost()
        with self._patch_env(), self._patch_post(fake):
            self._add_stock(product, -3)  # 5 → 2, no cross
            self._add_stock(product, -2)  # 2 → 0, crosses
            self._flush()

        self.assertEqual(len(self._availability_events(product.product_tmpl_id)), 1)
        self.assertEqual(len(fake.calls), 1)

    # ── template-field transitions ───────────────────────────────────────
    def test_sale_ok_flip_emits(self):
        template = self._make_product("Elderberry").product_tmpl_id
        self._reset_buffer()

        fake = _FakePost()
        with self._patch_env(), self._patch_post(fake):
            template.write({"sale_ok": False})
            self._flush()

        self.assertEqual(len(self._availability_events(template)), 1)

    def test_website_published_flip_emits(self):
        template = self._make_product("Chestnut").product_tmpl_id
        self._reset_buffer()

        fake = _FakePost()
        with self._patch_env(), self._patch_post(fake):
            template.write({"website_published": True})
            self._flush()

        self.assertEqual(len(self._availability_events(template)), 1)

    def test_unrelated_template_write_emits_nothing(self):
        template = self._make_product("Hazelnut").product_tmpl_id
        self._reset_buffer()

        fake = _FakePost()
        with self._patch_env(), self._patch_post(fake):
            template.write({"grove_seo_description": "a new blurb"})
            self._flush()

        self.assertFalse(self._availability_events(template))

    # ── resilience + storm guard ─────────────────────────────────────────
    def test_missing_webhook_config_never_raises_and_records_nothing(self):
        product = self._make_product("Mulberry")
        self._stock_then_reset(product, 2)

        # No webhook env at all → the emit must be swallowed, the stock write must
        # still succeed, and no event row is left behind.
        with mock.patch.dict(os.environ, {}, clear=True):
            self._add_stock(product, -2)
            self._flush()

        self.assertEqual(product.qty_available, 0)
        self.assertFalse(self._availability_events(product.product_tmpl_id))

    def test_bulk_transition_is_capped_per_transaction(self):
        products = [self._make_product(f"Bulk {i}") for i in range(3)]
        for product in products:
            self._stock_then_reset(product, 1)

        fake = _FakePost()
        # Shrink the cap so the test stays fast but still exercises the guard.
        with (
            mock.patch.object(grove_publish_event, "_AVAILABILITY_EMIT_CAP", 2),
            self._patch_env(),
            self._patch_post(fake),
        ):
            for product in products:
                self._add_stock(product, -1)
            self._flush()

        emitted = sum(len(self._availability_events(p.product_tmpl_id)) for p in products)
        self.assertEqual(emitted, 2)  # capped: the 3rd degrades to the ISR window
        self.assertEqual(len(fake.calls), 2)
