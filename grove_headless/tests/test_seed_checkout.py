"""Seed pre-order checkout: $1 deposit, mixing rule, and the shipping-rule
bypass (GOL-3257 §6).

Drives the real module functions (``_create_draft_order`` on the nursery
website, ``_build_stripe_line_items``) so the deposit amount, the "seeds check
out on their own" refusals, the non-green-list ship bypass and the harvest-year
stamp are all exercised end-to-end against a sale.order.

Runs under Odoo's --test-enable runner (needs a DB), so it is listed in
tests/__init__.py AND excluded from pytest in conftest.py (GOL-1936).
"""

from datetime import date
from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import stripe_gateway
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from .common import GroveTaxFixtureMixin

TODAY = date(2026, 10, 20)  # inside the chinquapin season, before the Nov 1 order-by


@tagged("post_install", "-at_install")
class TestSeedCheckout(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        # Two seed products in the same (2026) season, one bareroot tree, and one
        # seed product whose order-by has already passed so it rolls to 2027.
        # Prices mirror the locked launch ladder (gom#334): Chinquapin 50-pack
        # $30, Hazelnut 50-pack $26. Pack weight 0.125 lb is the 50-count rung.
        self.seed_a = self._seed("Chinquapin 50-pack", order_by=date(2026, 11, 1))
        self.seed_c = self._seed("Hazelnut 50-pack", order_by=date(2026, 11, 1), price=26.0)
        self.seed_rolled = self._seed("Closed-season seed", order_by=date(2026, 10, 1))
        self.bareroot = self.env["product.product"].create(
            {
                "name": "Pawpaw (bareroot)",
                "type": "consu",
                "is_storable": True,
                "list_price": 40.0,
                "grove_shipping_tier": "bareroot",
            }
        )

    def _seed(self, name, order_by, price=30.0):
        return self.env["product.product"].create(
            {
                "name": name,
                "type": "consu",
                "is_storable": True,
                "list_price": price,
                "grove_shipping_tier": "seed",
                "grove_seed_pack_lb": 0.125,
                "grove_seed_open": True,
                "grove_seed_ship_start": date(2026, 10, 15),
                "grove_seed_ship_end": date(2026, 11, 15),
                "grove_seed_order_by": order_by,
                "grove_seed_cap_lb": 10.0,
            }
        )

    def _website(self):
        return self.env.ref("grove_headless.website_nursery")

    def _order_with(self, products):
        """A bare sale.order carrying one line per product (no checkout flow)."""
        return (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": self.env["res.partner"].create({"name": "X", "email": "x@e.com"}).id,
                    "company_id": self.company.id,
                    "order_line": [(0, 0, {"product_id": p.id, "product_uom_qty": 1.0}) for p in products],
                }
            )
        )

    def _payload(self, products, state="FL"):
        return {
            "contact": {"name": "Seed Test", "email": "seedco@example.com", "phone": "3045551212"},
            "items": [{"variant_id": p.id, "quantity": 1} for p in products],
            "fulfillment": "ship",
            "shipping": {"street": "1 Rd", "city": "Town", "state": state, "zip": "33101"},
        }

    def _create(self, payload):
        with (
            mock.patch.object(grove_main, "_today_utc", return_value=TODAY),
            mock.patch.object(grove_main, "_apply_shipping_line", return_value=16.0),
        ):
            return grove_main._create_draft_order(self._website(), self.env, payload)

    # ── $1 deposit ────────────────────────────────────────────────────────

    def test_seed_cart_is_a_one_dollar_deposit(self):
        order = self._order_with([self.seed_a])
        self.assertTrue(grove_main._order_is_seed(order))
        self.assertTrue(grove_main._order_takes_deposit(order, TODAY))
        self.assertEqual(grove_main._order_deposit_amount(order), stripe_gateway.SEED_DEPOSIT)
        items, preorder_ids, charged = grove_main._build_stripe_line_items(order, today=TODAY)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "deposit")
        self.assertEqual(items[0]["name"], "Seed reservation deposit")
        self.assertEqual(items[0]["amount_cents"], 100)
        self.assertEqual(charged, 100)
        self.assertEqual(set(preorder_ids), {self.seed_a.id})

    def test_tree_cart_keeps_the_ten_dollar_deposit(self):
        order = self._order_with([self.bareroot])
        self.assertFalse(grove_main._order_is_seed(order))
        self.assertEqual(grove_main._order_deposit_amount(order), stripe_gateway.PREORDER_DEPOSIT)

    # ── ships to a non-green-list state (seeds are exempt) ──────────────────

    def test_seed_ships_to_non_green_list_state_and_stamps_harvest(self):
        order, error = self._create(self._payload([self.seed_a], state="FL"))
        self.assertIsNone(error, "a seed order must ship to FL (non-green-list)")
        self.assertTrue(grove_main._order_is_seed(order))
        self.assertTrue(grove_main._order_takes_deposit(order, TODAY))
        # Not quoted at checkout: no GROVE-SHIP line yet (it is created at settlement).
        self.assertFalse(order.order_line.filtered(lambda ol: ol.product_id.default_code == "GROVE-SHIP"))
        # Harvest year stamped on the line and rolled up to the order.
        seed_line = order.order_line.filtered(lambda ol: ol.product_id == self.seed_a)
        self.assertEqual(seed_line.grove_seed_harvest_year, 2026)
        self.assertEqual(order.grove_seed_harvest_year, 2026)

    def test_two_seeds_same_harvest_year_share_a_cart(self):
        order, error = self._create(self._payload([self.seed_a, self.seed_c], state="GA"))
        self.assertIsNone(error, "two seed products in the same harvest year may share one $1 deposit")
        self.assertEqual(order.grove_seed_harvest_year, 2026)

    # ── seed destinations: every state + DC except HI/AK (Josh 2026-10-13) ──

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_seed_refused_to_hawaii_and_alaska(self):
        for state in ("HI", "AK"):
            order, error = self._create(self._payload([self.seed_a], state=state))
            self.assertIsNone(order, f"seeds must not ship to {state}")
            self.assertEqual(error.status_code, 400)
            self.assertIn("hawaii and alaska", error.data.decode().lower())

    def test_seed_ships_to_a_far_non_green_state(self):
        # California is neither green-list nor HI/AK: seeds reach it.
        order, error = self._create(self._payload([self.seed_a], state="CA"))
        self.assertIsNone(error, "seeds ship to every US state except HI/AK")
        self.assertTrue(grove_main._order_is_seed(order))

    # ── mixing refusals ─────────────────────────────────────────────────────

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_seed_plus_tree_refused(self):
        order, error = self._create(self._payload([self.seed_a, self.bareroot], state="GA"))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("seed reservations check out on their own", error.data.decode().lower())

    # ── fail closed: a mixed seed+tree cart never rides the seed bypass ─────
    # (GOL-3257 security review). One seed line in a Castanea tree cart must not
    # skip the green-list/compliance/shipping gates or take the $1 deposit, on
    # ANY tenant.

    def _castanea(self):
        tree = self.env["product.product"].create(
            {
                "name": "American Chestnut (bareroot)",
                "type": "consu",
                "is_storable": True,
                "list_price": 45.0,
                "grove_shipping_tier": "bareroot",
            }
        )
        tree.product_tmpl_id.grove_botanical_name = "Castanea dentata"
        return tree

    def _assert_mixed_refused(self, website):
        castanea = self._castanea()
        before = self.env["sale.order"].search_count([])
        with (
            mock.patch.object(grove_main, "_today_utc", return_value=TODAY),
            mock.patch.object(grove_main, "_apply_shipping_line", return_value=16.0) as ship_line,
        ):
            order, error = grove_main._create_draft_order(
                website, self.env, self._payload([self.seed_a, castanea], state="FL")
            )
        self.assertIsNone(order, "a seed + Castanea cart must be refused")
        self.assertEqual(error.status_code, 400)
        self.assertIn("seed reservations check out on their own", error.data.decode().lower())
        # Refused before any seed bypass or shipping line: no order survives.
        ship_line.assert_not_called()
        self.assertEqual(self.env["sale.order"].search_count([]), before)

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_seed_plus_castanea_refused_on_nursery(self):
        self._assert_mixed_refused(self._website())

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_seed_plus_castanea_refused_on_non_nursery_tenant(self):
        website = self.env.ref("grove_headless.website_ggg")
        self.assertFalse(grove_main._is_nursery_website(website))
        self._assert_mixed_refused(website)

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_mixed_cart_refused_even_if_wave_gate_is_skipped(self):
        # Defense in depth: _create_draft_order refuses a mixed cart on its own,
        # before the seed bypass, even if the wave/mixing gate returned clean.
        castanea = self._castanea()
        with mock.patch.object(grove_main, "_validate_ship_wave", return_value=(None, None)):
            order, error = self._create(self._payload([self.seed_a, castanea], state="FL"))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("seed reservations check out on their own", error.data.decode().lower())

    def test_mixed_order_is_not_a_seed_order(self):
        # All-lines-seed gate: a mixed order is NOT seed, so it never gets the $1
        # deposit, the seed deposit reason, or the $0 seed handling fee.
        castanea = self._castanea()
        order = self._order_with([self.seed_a, castanea])
        self.assertFalse(grove_main._order_is_seed(order))
        self.assertNotEqual(grove_main._order_deposit_amount(order), stripe_gateway.SEED_DEPOSIT)
        self.assertEqual(grove_main._order_deposit_amount(order), stripe_gateway.PREORDER_DEPOSIT)
        reason = grove_main._deposit_reason_for_lines(self.env, [(self.seed_a, 1.0), (castanea, 1.0)], "ship", TODAY)
        self.assertNotEqual(reason, "seed")
        self.assertEqual(grove_main._order_handling_fee(self.env, order), grove_main._shipping_handling_fee(self.env))
        # A tree-only order is not seed either.
        self.assertFalse(grove_main._order_is_seed(self._order_with([castanea])))

    def test_seed_only_order_is_seed_and_pays_no_handling(self):
        order = self._order_with([self.seed_a, self.seed_c])
        self.assertTrue(grove_main._order_is_seed(order))
        self.assertEqual(grove_main._order_handling_fee(self.env, order), 0.0)

    @mute_logger("odoo.addons.grove_headless.controllers.main")
    def test_two_harvest_years_refused(self):
        order, error = self._create(self._payload([self.seed_a, self.seed_rolled], state="GA"))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("seed reservations check out on their own", error.data.decode().lower())
