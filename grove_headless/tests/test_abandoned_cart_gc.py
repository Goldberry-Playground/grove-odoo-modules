"""Abandoned headless-API cart sweep (autovacuum).

``POST /grove/api/v1/cart`` creates a website_sale session cart: a draft
sale.order on the website's public partner, by OdooBot, with no Stripe session.
Nothing ever expires those, and the prod synthetic cart-flow journey makes one a
minute (119 farm drafts on 2026-10-30). ``_gc_grove_abandoned_api_carts`` deletes
them once they are a day old. What must hold: ONLY old, anonymous, never-checked-
out website carts go; a recent cart, a cart that started checkout, a named
customer's draft, a back-office quotation and any non-draft order all stay.

Needs a DB (sale.order), so it is listed in tests/__init__.py AND excluded from
pytest in conftest.py (the double-skip guard, GOL-1936).
"""

from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin


@tagged("post_install", "-at_install")
class TestAbandonedApiCartGc(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.website = self.env["website"].search([], limit=1)
        self.public_partner = self.website.user_id.partner_id
        self.customer = self.env["res.partner"].create({"name": "Real Shopper", "email": "shopper@example.com"})
        self.product = self.env["product.product"].create({"name": "GC Tee", "list_price": 25.0})

    def _order(self, *, age, partner=None, website=True, **vals):
        order = self.env["sale.order"].create(
            {
                "partner_id": (partner or self.public_partner).id,
                "website_id": self.website.id if website else False,
                "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1})],
                **vals,
            }
        )
        stamp = fields.Datetime.now() - age
        self.env.cr.execute("UPDATE sale_order SET create_date = %s WHERE id = %s", (stamp, order.id))
        order.invalidate_recordset(["create_date"])
        return order

    def test_old_anonymous_api_cart_is_deleted(self):
        cart = self._order(age=timedelta(days=2))
        self.env["sale.order"]._gc_grove_abandoned_api_carts()
        self.assertFalse(cart.exists())

    def test_everything_else_is_kept(self):
        keep = {
            "recent cart": self._order(age=timedelta(hours=2)),
            "checkout started": self._order(age=timedelta(days=2), grove_stripe_session_id="cs_live_x"),
            "named customer": self._order(age=timedelta(days=2), partner=self.customer),
            "back-office quote": self._order(age=timedelta(days=2), website=False),
        }
        cancelled = self._order(age=timedelta(days=2))
        cancelled.action_cancel()
        keep["cancelled"] = cancelled

        self.env["sale.order"]._gc_grove_abandoned_api_carts()

        for label, order in keep.items():
            self.assertTrue(order.exists(), f"{label} order must survive the sweep")
