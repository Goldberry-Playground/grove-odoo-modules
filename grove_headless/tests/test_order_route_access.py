"""Who may call the order routes the Discord bridge uses.

These routes accept any valid login (``auth="bearer"``), then work under ``sudo()``,
where the operator check inside the model always passes. So each route checks the
authenticated caller first. DB-backed, so listed in tests/__init__.py AND
conftest.py's collect_ignore_glob.
"""

import json
from datetime import datetime, timedelta

from odoo.tests.common import HttpCase, get_db_name, new_test_user, tagged

from .common import GroveTaxFixtureMixin

MISSING = 999999
ROUTES = (
    f"/grove/api/v1/orders/{MISSING}/hold",
    f"/grove/api/v1/orders/{MISSING}/hold/release",
    f"/grove/api/v1/orders/{MISSING}/hold/snooze",
    f"/grove/api/v1/orders/{MISSING}/move",
    f"/grove/api/v1/orders/{MISSING}/label-refunded",
    f"/grove/api/v1/orders/{MISSING}/mark-shipped",
    f"/grove/api/v1/orders/{MISSING}/mark-collected",
    f"/grove/api/v1/orders/{MISSING}/assign-wave",
    "/grove/api/v1/labels/batch",
    f"/grove/api/v1/labels/batch/{MISSING}/tracking",
    f"/grove/api/v1/label-batches/{MISSING}/mark-shipped",
)


@tagged("post_install", "-at_install")
class TestOrderRouteAccess(GroveTaxFixtureMixin, HttpCase):
    def _key_for(self, user):
        try:
            return (
                self.env["res.users.apikeys"]
                .with_user(user)
                ._generate("rpc", "grove-route-access-test", datetime.now() + timedelta(days=1))
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"could not mint an API key for bearer auth: {exc}")

    def _post(self, path, headers=None, body=None):
        self.env.flush_all()
        return self.url_open(
            path,
            data=json.dumps(body if body is not None else {"actor": "someone"}),
            headers={
                "X-Odoo-Database": get_db_name(),
                "X-Grove-Tenant": "goldberry",
                "Content-Type": "application/json",
                **(headers or {}),
            },
            allow_redirects=False,
        )

    def _order(self):
        partner = self.env["res.partner"].create({"name": "Access Buyer", "email": "access@example.com"})
        website = self.env["website"]._resolve_tenant_slug("goldberry") or self.env["website"].search([], limit=1)
        product = self.env["product.product"].create({"name": "Pawpaw", "type": "consu", "list_price": 22.0})
        order = (
            self.env["sale.order"]
            .with_company(website.company_id)
            .create(
                {
                    "partner_id": partner.id,
                    "company_id": website.company_id.id,
                    "order_line": [(0, 0, {"product_id": product.id, "product_uom_qty": 1.0})],
                }
            )
        )
        order.grove_fulfillment = "ship"
        return order

    def test_employee_without_sales_access_is_refused_on_every_route(self):
        user = new_test_user(self.env, login="no_sales_key", groups="base.group_user")
        headers = {"Authorization": f"Bearer {self._key_for(user)}"}
        for path in ROUTES:
            with self.subTest(path=path):
                self.assertEqual(self._post(path, headers=headers).status_code, 403)

    def test_employee_without_sales_access_cannot_change_an_order(self):
        user = new_test_user(self.env, login="no_sales_hold", groups="base.group_user")
        order = self._order()
        response = self._post(
            f"/grove/api/v1/orders/{order.id}/hold",
            headers={"Authorization": f"Bearer {self._key_for(user)}"},
            body={"actor": "someone", "until": "2030-01-15", "reason": "not allowed"},
        )
        self.assertEqual(response.status_code, 403)
        order.invalidate_recordset()
        self.assertFalse(order.grove_hold_until)

    def test_logged_in_portal_customer_is_refused(self):
        new_test_user(self.env, login="portal_buyer", groups="base.group_portal")
        self.authenticate("portal_buyer", "portal_buyer")
        response = self._post(
            f"/grove/api/v1/orders/{MISSING}/mark-shipped",
            headers={
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_sales_manager_key_reaches_every_route(self):
        user = new_test_user(
            self.env, login="all_orders_key", groups="base.group_user,sales_team.group_sale_salesman_all_leads"
        )
        headers = {"Authorization": f"Bearer {self._key_for(user)}"}
        for path in ROUTES:
            with self.subTest(path=path):
                self.assertNotEqual(self._post(path, headers=headers).status_code, 403)
