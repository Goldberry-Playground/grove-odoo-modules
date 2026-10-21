"""Odoo-runtime tests for the 2026-10-15 capture hotfix.

* ``POST /grove/api/v1/leads``: consult / wholesale / farm-visit / state-waitlist
  requests become CRM leads (Josh: customer relationships live in Odoo, not Ghost).
* ``POST /grove/api/v1/products/<template>/notify-restock``: back-in-stock alerts
  ride Odoo's own ``stock_notification_partner_ids``.
* ``product.product._send_availability_email``: the hourly cron, fixed for the
  headless multi-company setup (per-company stock + sender, shared pool,
  storefront link, one email per template).
"""

import json
from datetime import datetime, timedelta
from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import HttpCase, get_db_name

from .common import GroveTaxFixtureMixin


class _ShrubFixture:
    def _build(self):
        self.company = self.env.ref("base.main_company")
        self.warehouse = self.env["stock.warehouse"].search([("company_id", "=", self.company.id)], limit=1)
        self.location = self.warehouse.lot_stock_id
        fmt = self.env["product.attribute"].create({"name": "Format", "create_variant": "always"})
        values = [
            self.env["product.attribute.value"].create({"name": n, "attribute_id": fmt.id})
            for n in ("Potted", "Bareroot")
        ]
        self.tmpl = self.env["product.template"].create(
            {
                "name": "Restock Serviceberry",
                "type": "consu",
                "is_storable": True,
                "sale_ok": True,
                "website_published": True,
                "list_price": 12.0,
                "company_id": self.company.id,
                "attribute_line_ids": [(0, 0, {"attribute_id": fmt.id, "value_ids": [(6, 0, [v.id for v in values])]})],
            }
        )
        self.variants = self.tmpl.product_variant_ids

    def _stock(self, variant, qty):
        self.env["stock.quant"]._update_available_quantity(variant, self.location, qty)
        variant.invalidate_recordset(["qty_available", "free_qty"])


class _BearerMixin:
    def _mint_key(self):
        try:
            admin = self.env.ref("base.user_admin")
            self.api_key = (
                self.env["res.users.apikeys"]
                .with_user(admin)
                ._generate("rpc", "grove-capture-test", datetime.now() + timedelta(days=1))
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"could not mint an API key for bearer auth: {exc}")

    def _post(self, path, body):
        headers = {
            "X-Odoo-Database": get_db_name(),
            "X-Grove-Tenant": "goldberry",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        return self.url_open(path, data=json.dumps(body).encode(), headers=headers)


@tagged("grove_headless", "post_install", "-at_install")
class TestLeadEndpoint(_BearerMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self._mint_key()

    def _leads(self, email):
        return self.env["crm.lead"].sudo().search([("email_from", "=", email)])

    def test_creates_tagged_lead_and_partner(self):
        with mock.patch.object(grove_main, "_notify_discord") as discord:
            resp = self._post(
                "/grove/api/v1/leads",
                {"email": "buyer@example.com", "name": "Bulk Buyer", "kind": "wholesale", "message": "200 pawpaws?"},
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        lead = self._leads("buyer@example.com")
        self.assertEqual(len(lead), 1)
        self.assertEqual(lead.name, "Wholesale inquiry: Bulk Buyer")
        self.assertIn("200 pawpaws?", lead.description or "")
        self.assertIn("Wholesale inquiry", lead.tag_ids.mapped("name"))
        self.assertEqual(lead.partner_id.email, "buyer@example.com")
        self.assertEqual(lead.company_id, self.env.ref("base.main_company"))
        self.assertTrue(resp.json()["created"])
        discord.assert_called_once()

    def test_same_kind_within_a_day_appends_instead_of_duplicating(self):
        with mock.patch.object(grove_main, "_notify_discord"):
            self._post("/grove/api/v1/leads", {"email": "twice@example.com", "kind": "farm_visit", "label": "upick"})
            resp = self._post(
                "/grove/api/v1/leads",
                {"email": "twice@example.com", "kind": "farm_visit", "label": "upick", "message": "Saturday?"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["created"])
        lead = self._leads("twice@example.com")
        self.assertEqual(len(lead), 1)
        self.assertIn("farm-visit:upick", lead.tag_ids.mapped("name"))
        self.assertTrue(any("Saturday?" in (m.body or "") for m in lead.message_ids))

    def test_state_waitlist_records_state(self):
        with mock.patch.object(grove_main, "_notify_discord"):
            resp = self._post(
                "/grove/api/v1/leads", {"email": "ca@example.com", "kind": "state_waitlist", "state": "ca"}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._leads("ca@example.com").name, "New-state waitlist: ca@example.com (CA)")

    def test_validation_errors_are_400(self):
        self.assertEqual(self._post("/grove/api/v1/leads", {"email": "x", "kind": "wholesale"}).status_code, 400)
        self.assertEqual(self._post("/grove/api/v1/leads", {"email": "a@b.co", "kind": "nope"}).status_code, 400)
        self.assertFalse(self._leads("a@b.co"))

    def test_dept_waitlist_is_a_lead_not_a_mailing_subscription(self):
        """Shop "coming soon" waitlist lands as a CRM request tagged for the
        launch pull — and NEVER joins a mailing.list (GOL-3456 §7/§8)."""
        self.env["product.public.category"].create(
            {"name": "Mycoforestry", "grove_slug": "mycoforestry", "grove_node_kind": "department"}
        )
        email = "coming-soon@example.com"
        with mock.patch.object(grove_main, "_notify_discord"):
            resp = self._post(
                "/grove/api/v1/leads",
                {
                    "email": email,
                    "kind": "dept_waitlist",
                    "department": "mycoforestry",
                    "label": "nursery-waitlist-mycoforestry",
                    "source": "notify-me",
                },
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        lead = self._leads(email)
        self.assertEqual(len(lead), 1)
        self.assertEqual(lead.name, "Department waitlist: coming-soon@example.com (Mycoforestry)")
        self.assertIn("Department waitlist", lead.tag_ids.mapped("name"))
        # The contact carries the readable launch-pull tag, same as the
        # newsletter waitlist path (GOL-2744).
        self.assertIn("Waitlist: Mycoforestry", lead.partner_id.category_id.mapped("name"))
        # Structural guarantee: zero Email Marketing footprint.
        self.assertFalse(self.env["mailing.contact"].sudo().search([("email", "=", email)]))
        self.assertFalse(self.env["mailing.subscription"].sudo().search([("contact_id.email", "=", email)]))


@tagged("grove_headless", "post_install", "-at_install")
class TestRestockEndpoint(_ShrubFixture, _BearerMixin, GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self._build()
        self._mint_key()

    def test_subscribes_every_variant_of_the_template(self):
        resp = self._post(f"/grove/api/v1/products/{self.tmpl.id}/notify-restock", {"email": "wait@example.com"})
        self.assertEqual(resp.status_code, 200, resp.content)
        for variant in self.variants:
            self.assertIn("wait@example.com", variant.stock_notification_partner_ids.mapped("email"))

    def test_variant_id_narrows_the_subscription(self):
        target = self.variants[0]
        resp = self._post(
            f"/grove/api/v1/products/{self.tmpl.id}/notify-restock",
            {"email": "one@example.com", "variant_id": target.id},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("one@example.com", target.stock_notification_partner_ids.mapped("email"))
        self.assertNotIn("one@example.com", self.variants[1].stock_notification_partner_ids.mapped("email"))

    def test_unknown_product_404_and_bad_email_400(self):
        self.assertEqual(
            self._post("/grove/api/v1/products/999999/notify-restock", {"email": "a@b.co"}).status_code, 404
        )
        self.assertEqual(
            self._post(f"/grove/api/v1/products/{self.tmpl.id}/notify-restock", {"email": "nope"}).status_code, 400
        )


@tagged("grove_headless", "post_install", "-at_install")
class TestRestockEmail(_ShrubFixture, GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self._build()
        self.partner = self.env["res.partner"].create({"name": "Waiting", "email": "waiting@example.com"})
        self.variants.write({"stock_notification_partner_ids": [(4, self.partner.id)]})

    def _sent(self):
        return self.env["mail.mail"].sudo().search([("email_to", "ilike", "waiting@example.com")])

    def test_no_email_while_sold_out(self):
        self.env["product.product"]._send_availability_email()
        self.assertFalse(self._sent())
        self.assertIn(self.partner, self.variants[0].stock_notification_partner_ids)

    def test_one_email_with_storefront_link_when_restocked(self):
        website = self.env["website"].search([("company_id", "=", self.company.id)], limit=1)
        website.domain = "https://goldberrygrove.farm"
        self._stock(self.variants[0], 5)
        self.env["product.product"]._send_availability_email()
        mails = self._sent()
        self.assertEqual(len(mails), 1, "one email per template, not one per variant")
        self.assertIn(f"https://goldberrygrove.farm/shop/{self.tmpl.id}", mails.body_html)
        for variant in self.variants:
            self.assertNotIn(self.partner, variant.stock_notification_partner_ids)

    def test_unpublished_product_waits(self):
        self.tmpl.website_published = False
        self._stock(self.variants[0], 5)
        self.env["product.product"]._send_availability_email()
        self.assertFalse(self._sent())
