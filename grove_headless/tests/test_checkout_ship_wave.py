"""Pre-order wave + potted-season validation in ``_create_draft_order``.

``ship_wave`` is optional (a legacy payload keeps today's behaviour and stores
nothing); when present it must be fall/spring, the cart must hold a bareroot
line, and the wave must be open for the destination zone (farm zone for
pickup). The potted-season and mixed-cart rules apply with or without it.
"""

from datetime import date
from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models.shipping_calendar import usda_zone_for_zip
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

ZONE8_ZIP = "08014"  # zone 8 in the ZIP matrix


@tagged("post_install", "-at_install")
class TestCheckoutShipWave(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.potted = self._product("Wave Potted", "potted")
        self.bareroot = self._product("Wave Bareroot", "bareroot")

    def _product(self, name, tier):
        return self.env["product.product"].create(
            {
                "name": name,
                "type": "consu",
                "is_storable": True,
                "list_price": 40.0,
                "grove_shipping_tier": tier,
            }
        )

    def _website(self):
        # The wave / potted-season rules are nursery-only (M1): run them on the
        # real nursery tenant website seeded by data/grove_companies.xml.
        return self.env.ref("grove_headless.website_nursery")

    def _goldberry(self):
        return self.env.ref("website.default_website")

    def _payload(self, products, fulfillment="pickup", zip_code=ZONE8_ZIP, **extra):
        payload = {
            "contact": {"name": "Wave Test", "email": "wave@example.com", "phone": "3045551212"},
            "items": [{"variant_id": p.id, "quantity": 1} for p in products],
            "fulfillment": fulfillment,
        }
        if fulfillment == "ship":
            payload["shipping"] = {"street": "1 Rd", "city": "Town", "state": "GA", "zip": zip_code}
        payload.update(extra)
        return payload

    def _create(self, payload, today, website=None):
        with (
            mock.patch.object(grove_main, "_today_utc", return_value=today),
            mock.patch.object(grove_main, "_apply_shipping_line", return_value=16.0),
        ):
            return grove_main._create_draft_order(website or self._website(), self.env, payload)

    def _assert_400(self, result, fragment):
        order, error = result
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn(fragment.lower(), error.data.decode().lower())
        self.assertFalse(self.env["sale.order"].search([("partner_id.email", "=", "wave@example.com")]))

    def test_farm_zip_is_zone_6(self):
        self.assertEqual(usda_zone_for_zip(grove_main._farm_pickup_zip(self.env, self.company)), 6)

    # ── potted season ────────────────────────────────────────────────────

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_potted_after_oct_15_rejected(self):
        self._assert_400(
            self._create(self._payload([self.potted]), date(2026, 10, 16)),
            "Potted trees are sold through Oct 15. Choose a bareroot pre-order.",
        )

    # ── tenant scope (M1): nursery only ─────────────────────────────────

    def test_tenant_websites_resolve(self):
        self.assertEqual(self._website().grove_tenant_slug(), "nursery")
        self.assertEqual(self._goldberry().grove_tenant_slug(), "goldberry")
        self.assertTrue(grove_main._is_nursery_website(self._website()))
        self.assertFalse(grove_main._is_nursery_website(self._goldberry()))
        self.assertFalse(grove_main._is_nursery_website(self.env.ref("grove_headless.website_ggg")))
        self.assertFalse(grove_main._is_nursery_website(None))

    def test_non_nursery_default_potted_after_oct_15_not_rejected(self):
        # grove_shipping_tier defaults to "potted": a Goldberry / GGG product
        # must keep selling after Oct 15, mixed or not, and a stray ship_wave
        # is ignored (nothing stored).
        default_tier = self.env["product.product"].create({"name": "Goldberry Thing", "type": "consu"})
        self.assertEqual(default_tier.grove_effective_shipping_tier, "potted")
        for website in (self._goldberry(), self.env.ref("grove_headless.website_ggg")):
            with self.subTest(tenant=website.grove_tenant_slug()):
                for products, extra in (
                    ([default_tier], {}),
                    ([default_tier, self.bareroot], {}),
                    ([default_tier], {"ship_wave": "fall"}),
                ):
                    error, wave = grove_main._validate_ship_wave(
                        self.env,
                        self._payload(products, **extra),
                        [mock.Mock(display_type=False, product_id=p) for p in products],
                        "pickup",
                        None,
                        date(2026, 10, 16),
                        website.company_id,
                        website=website,
                    )
                    self.assertEqual((error, wave), (None, None))
        order, error = self._create(self._payload([default_tier]), date(2026, 10, 16), website=self._goldberry())
        self.assertIsNone(error)
        self.assertFalse(order.grove_ship_wave)

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_nursery_default_potted_after_oct_15_still_rejected(self):
        default_tier = self.env["product.product"].create({"name": "Nursery Thing", "type": "consu"})
        self._assert_400(
            self._create(self._payload([default_tier]), date(2026, 10, 16)),
            "Potted trees are sold through Oct 15.",
        )

    def test_potted_on_oct_15_allowed(self):
        order, error = self._create(self._payload([self.potted]), date(2026, 10, 15))
        self.assertIsNone(error)
        self.assertFalse(order.grove_ship_wave)

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_potted_before_may_1_rejected(self):
        self._assert_400(self._create(self._payload([self.potted]), date(2026, 4, 30)), "sold through Oct 15")

    # ── mixed cart ───────────────────────────────────────────────────────

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_mixed_cart_rejected_with_and_without_wave(self):
        msg = "Pre-orders check out on their own. Remove the trees that ship now, or check them out first."
        today = date(2026, 10, 7)
        self._assert_400(self._create(self._payload([self.potted, self.bareroot]), today), msg)
        self._assert_400(
            self._create(self._payload([self.potted, self.bareroot], ship_wave="fall"), today),
            msg,
        )

    # ── wave validation ──────────────────────────────────────────────────

    def test_legacy_payload_without_wave_unchanged(self):
        order, error = self._create(self._payload([self.bareroot]), date(2026, 11, 22))
        self.assertIsNone(error)
        self.assertFalse(order.grove_ship_wave)

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_invalid_wave_rejected(self):
        self._assert_400(
            self._create(self._payload([self.bareroot], ship_wave="winter"), date(2026, 10, 7)),
            "Choose a fall or spring pre-order wave.",
        )

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_wave_without_bareroot_line_rejected(self):
        self._assert_400(
            self._create(self._payload([self.potted], ship_wave="fall"), date(2026, 10, 7)),
            "only applies to bareroot",
        )

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_closed_fall_wave_rejected(self):
        self._assert_400(
            self._create(self._payload([self.bareroot], "ship", ship_wave="fall"), date(2026, 11, 22)),
            "The fall pre-order for zone 8 closed on",
        )

    def test_open_spring_wave_stored(self):
        order, error = self._create(self._payload([self.bareroot], "ship", ship_wave="spring"), date(2026, 11, 22))
        self.assertIsNone(error)
        self.assertEqual(order.grove_ship_wave, "spring")

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_pickup_uses_farm_zone_6(self):
        # Zone 6 fall order-by is Nov 21; a zone-8 destination ZIP is irrelevant.
        self._assert_400(
            self._create(self._payload([self.bareroot], "pickup", ship_wave="fall"), date(2026, 11, 22)),
            "The fall pre-order for zone 6 closed on Nov 21",
        )

    def test_pickup_fall_open_before_deadline(self):
        order, error = self._create(self._payload([self.bareroot], "pickup", ship_wave="fall"), date(2026, 11, 21))
        self.assertIsNone(error)
        self.assertEqual(order.grove_ship_wave, "fall")

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_unknown_zip_rejected(self):
        self._assert_400(
            self._create(self._payload([self.bareroot], "ship", zip_code="00000", ship_wave="fall"), date(2026, 10, 7)),
            "We could not find a planting zone for that ZIP code.",
        )
