"""Checkout gate: compliance exemption + farm-pickup-only overrides (GOL-2587).

Prod carries ZERO ``mrp.bom`` records, so the GOL-2132 carve-out gate treats
every bundle as a standalone line and its unparseable ``"Bundle: …"`` botanical
hits the fail-safe → blocked into all 9 regulated states. These tests cover the
two admin escape hatches added to ``_create_draft_order``:

  * ``grove_compliance_exempt`` — skip the per-line carve-out gate (ships
    anywhere on the green list); an exempt "Bundle: …" line with no kit BoM
    still gets a manual-verify packing note.
  * ``grove_pickup_only`` — reject any SHIP order containing the line (400),
    independent of shipping tier; a pickup order passes.

Kept out of ``test_stripe_checkout.py`` on purpose: those touch the ``*stripe*``
protected path (human code-owner review), and this gate is unrelated to Stripe.
"""

from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


@tagged("post_install", "-at_install")
class TestCheckoutComplianceGate(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {"name": "Cart Customer", "email": "cart@example.com", "company_id": self.company.id}
        )
        self.product = self.env["product.product"].create(
            {"name": "Remembrance Grove", "type": "consu", "is_storable": True, "list_price": 120.0}
        )

    # ── helpers ──────────────────────────────────────────────────────────

    def _website(self):
        return self.env["website"].search([("company_id", "=", self.company.id)], limit=1) or self.env[
            "website"
        ].search([], limit=1)

    def _cart_payload(self, state, **extra):
        payload = {
            "contact": {"name": "Ship Test", "email": "ship@example.com", "phone": "3045551212"},
            "items": [{"variant_id": self.product.id, "quantity": 1}],
            "shipping": {"street": "1 Rd", "city": "Town", "state": state, "zip": "10001"},
        }
        payload.update(extra)
        return payload

    def _bundle_product(self, exempt=False, note=False):
        """The prod shape: an unparseable "Bundle: …" botanical and no kit BoM,
        which fail-safe-blocks into every regulated state. Bareroot so it clears
        the potted gate and reaches the carve-out loop."""
        tmpl = self.product.product_tmpl_id
        tmpl.grove_shipping_tier = "bareroot"
        tmpl.grove_botanical_name = "Bundle: Castanea spp., Prunus americana"
        tmpl.grove_compliance_exempt = exempt
        if note:
            tmpl.grove_compliance_note = "Cleared by Josh 2026-09-29"
        return tmpl

    # ── compliance exemption ─────────────────────────────────────────────

    def test_exempt_bundle_ships_into_regulated_state(self):
        """An admin-exempt bundle (no kit BoM) skips the carve-out gate and ships
        into a regulated green state (OH), where a non-exempt unparseable line
        would fail-safe-block. A "Bundle: …" line also gets a manual-verify
        packing note so the packer checks components by hand."""
        self._bundle_product(exempt=True, note=True)
        payload = self._cart_payload("OH", fulfillment="ship")
        with mock.patch.object(grove_main, "_apply_shipping_line", return_value=16.0):
            order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(error)
        self.assertTrue(order)
        self.assertIn("COMPLIANCE: verify components", order.grove_substitution_note or "")
        self.assertIn("OH", order.grove_substitution_note or "")

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_nonexempt_unparseable_still_failsafes(self):
        """The fail-safe is unchanged for a NON-exempt line: an unparseable
        botanical into a regulated green state is hard-rejected at 400."""
        self._bundle_product(exempt=False)
        payload = self._cart_payload("OH", fulfillment="ship")
        with mock.patch.object(grove_main, "_apply_shipping_line", return_value=16.0):
            order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("can't confirm", error.data.decode().lower())
        self.assertFalse(self.env["sale.order"].search([("partner_id.email", "=", "ship@example.com")]))

    # ── farm-pickup-only ─────────────────────────────────────────────────

    def test_pickup_only_line_blocks_ship_order(self):
        """A farm-pickup-only product hard-rejects any ship order — independent of
        shipping tier (bareroot here, which would otherwise ship)."""
        tmpl = self.product.product_tmpl_id
        tmpl.grove_shipping_tier = "bareroot"
        tmpl.grove_pickup_only = True
        payload = self._cart_payload("WV", fulfillment="ship")
        with mock.patch.object(grove_main, "_apply_shipping_line", return_value=16.0):
            order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("farm pickup only", error.data.decode().lower())
        self.assertFalse(self.env["sale.order"].search([("partner_id.email", "=", "ship@example.com")]))

    def test_pickup_only_line_allows_pickup_order(self):
        """The same pickup-only product goes through when the buyer chooses farm
        pickup."""
        tmpl = self.product.product_tmpl_id
        tmpl.grove_shipping_tier = "bareroot"
        tmpl.grove_pickup_only = True
        payload = self._cart_payload("WV", fulfillment="pickup")
        order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(error)
        self.assertTrue(order)
