"""sale.order.grove_usda_zone — the stored, groupable destination USDA zone
(GOL-3056) and the three balance-not-charged saved filters shipped with it.

TransactionCase (DB-backed): the field is a stored compute off the shipping /
farm ZIP via shipping_calendar.usda_zone_for_zip, so it only exists at runtime.
Pure-Python XML consistency of the filter domains lives in
test_usda_zone_filters.py.
"""

from odoo.tests import TransactionCase, tagged

from ..controllers.main import _farm_pickup_zip
from ..models.shipping_calendar import usda_zone_for_zip
from .common import GroveTaxFixtureMixin


@tagged("post_install", "-at_install")
class TestGroveUsdaZone(GroveTaxFixtureMixin, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.SaleOrder = cls.env["sale.order"]
        # A shippable product so order_line is non-trivial; the zone compute only
        # reads the partner/fulfilment, but orders carry at least one line.
        cls.product = cls.env["product.product"].create(
            {"name": "GOL-3056 test tree", "list_price": 25.0, "type": "consu"}
        )

    def _order(self, zip_code, fulfillment="ship"):
        partner = self.env["res.partner"].create(
            {"name": f"Buyer {zip_code or 'none'}", "zip": zip_code, "country_id": self.env.ref("base.us").id}
        )
        order = self.SaleOrder.create(
            {
                "partner_id": partner.id,
                "partner_shipping_id": partner.id,
                "company_id": self.company.id,
                "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1})],
            }
        )
        order.grove_fulfillment = fulfillment
        return order, partner

    def test_known_zip_ship(self):
        order, _ = self._order("10001")  # NYC -> zone 7
        self.assertEqual(order.grove_usda_zone, "7")

    def test_zip_plus_four(self):
        order, _ = self._order("26651-1234")  # Summersville WV +4 -> zone 6
        self.assertEqual(order.grove_usda_zone, "6")

    def test_unknown_zip_is_empty(self):
        order, _ = self._order("33101")  # Miami, not on the green-state matrix
        self.assertFalse(order.grove_usda_zone)

    def test_missing_zip_is_empty(self):
        order, _ = self._order(False)
        self.assertFalse(order.grove_usda_zone)

    def test_pickup_uses_farm_zone(self):
        order, _ = self._order("10001", fulfillment="pickup")
        farm_zone = usda_zone_for_zip((_farm_pickup_zip(self.env, self.company) or "").strip() or None)
        # Pickup ignores the buyer's zone 7 and keys off the farm's origin ZIP.
        self.assertNotEqual(order.grove_usda_zone, "7")
        self.assertEqual(order.grove_usda_zone, str(farm_zone) if farm_zone is not None else False)

    def test_address_edit_recomputes(self):
        order, partner = self._order("10001")  # zone 7
        self.assertEqual(order.grove_usda_zone, "7")
        partner.zip = "26651"  # move to WV -> zone 6
        self.assertEqual(order.grove_usda_zone, "6")

    def test_field_is_stored_and_queryable(self):
        """Stored => the zone is a real column, so it can be searched, grouped
        and pivoted in the UI. Prove it is queried from the DB, not recomputed."""
        order, _ = self._order("10001")  # zone 7
        self.assertTrue(self.SaleOrder._fields["grove_usda_zone"].store)
        hits = self.SaleOrder.search([("grove_usda_zone", "=", "7"), ("id", "=", order.id)])
        self.assertEqual(hits, order)

    # ── Shipped saved filters (data/grove_zone_filters.xml) ─────────────────

    def test_shipped_filters_install_as_records(self):
        """The three filters must actually exist as ir.filters rows after the
        module installs — a bad field name (Odoo 19 renamed user_id → user_ids)
        makes the data file unloadable, which the pure-XML/offline checks cannot
        see. Each is global (empty user_ids) and bound to sale.order."""
        for xmlid in (
            "grove_headless.ir_filter_grove_balance_not_charged",
            "grove_headless.ir_filter_grove_preorders_pickup",
            "grove_headless.ir_filter_grove_preorders_shipping",
        ):
            flt = self.env.ref(xmlid)
            self.assertTrue(flt, f"{xmlid} did not install")
            self.assertFalse(flt.user_ids, f"{xmlid} is not a global filter")
            self.assertEqual(flt.model_id, "sale.order", f"{xmlid} is not on sale.order")

    def test_shipped_filters_match_balance_due_constant(self):
        """Every shipped ir.filters' grove_checkout_status clause must equal
        GROVE_BALANCE_DUE_STATES — the item-4 guard: a new retry state added to
        the constant can never silently drop out of a filter."""
        from ast import literal_eval

        expected = set(self.SaleOrder.GROVE_BALANCE_DUE_STATES)
        for xmlid in (
            "grove_headless.ir_filter_grove_balance_not_charged",
            "grove_headless.ir_filter_grove_preorders_pickup",
            "grove_headless.ir_filter_grove_preorders_shipping",
        ):
            flt = self.env.ref(xmlid)
            domain = literal_eval(flt.domain)
            status_clause = next(
                (leaf for leaf in domain if isinstance(leaf, (list, tuple)) and leaf[0] == "grove_checkout_status"),
                None,
            )
            self.assertIsNotNone(status_clause, f"{xmlid} has no grove_checkout_status clause")
            self.assertEqual(set(status_clause[2]), expected, f"{xmlid} status set drifted from the constant")

    def test_pickup_and_shipping_filters_partition_by_fulfillment(self):
        """The pickup filter returns pickup orders and the shipping filter ship
        orders; both only when the balance is still due."""
        from ast import literal_eval

        pickup, _ = self._order("10001", fulfillment="pickup")
        ship, _ = self._order("26651", fulfillment="ship")
        for order in (pickup, ship):
            order.grove_checkout_status = "deposit_paid"
            order.action_confirm()  # -> state 'sale', in the base domain

        pickup_flt = self.env.ref("grove_headless.ir_filter_grove_preorders_pickup")
        ship_flt = self.env.ref("grove_headless.ir_filter_grove_preorders_shipping")
        pickup_hits = self.SaleOrder.search(literal_eval(pickup_flt.domain))
        ship_hits = self.SaleOrder.search(literal_eval(ship_flt.domain))

        self.assertIn(pickup, pickup_hits)
        self.assertNotIn(ship, pickup_hits)
        self.assertIn(ship, ship_hits)
        self.assertNotIn(pickup, ship_hits)

    def test_settled_order_drops_out_of_balance_filter(self):
        """A settled order (balance charged) must NOT appear in the outstanding
        filter even though it is confirmed."""
        from ast import literal_eval

        order, _ = self._order("10001", fulfillment="ship")
        order.grove_checkout_status = "settled"
        order.action_confirm()
        flt = self.env.ref("grove_headless.ir_filter_grove_balance_not_charged")
        self.assertNotIn(order, self.SaleOrder.search(literal_eval(flt.domain)))
