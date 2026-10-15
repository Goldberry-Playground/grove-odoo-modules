"""The $0 advice-only planting consult (GOL-3381, spec D).

Covers the Odoo half of spec section D plus the C.4 planner-plan carry:

  * the data product is a $0 service, unpublished, gate-exempt (D.1);
  * the service line never counts as a tree for the deposit rule, the bundle
    tree count, or the box plan — it is a service, not a storable (D.1);
  * ``_grove_consult_eligible`` at the $499.99 vs $500.00 boundary *after a
    promo*, the threshold-0 kill switch, and the consult-built OR-path (D.2);
  * ``_create_draft_order`` rejects the consult line on an ineligible cart with
    a 400 (D.2), and lets it through on an eligible one;
  * ``grove_consult_requested`` is computed from the lines (D.3);
  * the confirmation booking block/email is present only when requested and its
    link carries the order name (D.3);
  * the planner plan is stored on a consult-built order and rendered on the form
    (C.4);
  * the morning-digest "consult requested" section (D.3).

Runs under Odoo's --test-enable runner (needs a DB for sale.order); listed in
tests/__init__.py AND the root conftest's collect_ignore_glob so it is not
double-skipped (GOL-1936).
"""

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import order_digest, promotions
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


@tagged("post_install", "-at_install")
class TestConsultLine(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        # The consult product is scoped to the nursery company (spec D.1,
        # company_id 3) and the live storefront that offers it runs under the
        # nursery website — so the consult-bearing orders here must too. An order
        # on another company can't legally carry a line whose product belongs to
        # the nursery (res.company._check_company crossover), which is exactly
        # what checkout does in production.
        self.nursery = self.env.ref("grove_headless.company_nursery")
        # Company-shared customer (company_id False) so the same partner can sit
        # on a main-company order (the eligibility carts) and a nursery order
        # (the consult-bearing ones) without a company crossover.
        self.partner = self.env["res.partner"].create({"name": "Consult Customer", "email": "consult-line@example.com"})
        self.consult = self.env.ref("grove_headless.product_planting_consult")
        self.consult_variant = self.consult.product_variant_id
        # A plain storable tree — the regression control for the tree-count /
        # deposit / box-plan assertions. Tax-free on purpose: the chartless CI DB
        # binds the WV sale tax to a single company (GroveTaxFixtureMixin), so a
        # taxed product landing on a nursery-company order line would cross
        # companies on the tax; the tree's tax is irrelevant to these assertions.
        self.tree = self.env["product.product"].create(
            {
                "name": "American Plum (bareroot)",
                "type": "consu",
                "is_storable": True,
                "list_price": 22.0,
                "taxes_id": [(6, 0, [])],
            }
        )
        self.icp = self.env["ir.config_parameter"].sudo()

    # ── helpers ──────────────────────────────────────────────────────────

    def _website(self):
        # The nursery website (company 3) — the tenant that actually offers the
        # consult, and the only company whose [company, False] domain resolves
        # the nursery-scoped consult product inside _create_draft_order.
        return self.env.ref("grove_headless.website_nursery")

    def _order(self, lines, company=None):
        company = company or self.company
        return (
            self.env["sale.order"]
            .with_company(company)
            .create(
                {
                    "partner_id": self.partner.id,
                    "company_id": company.id,
                    "order_line": [(0, 0, {"product_id": p.id, "product_uom_qty": q}) for p, q in lines],
                }
            )
        )

    def _consult_built_product(self):
        prod = self.env["product.product"].create(
            {
                "name": "Food Forest 50",
                "type": "consu",
                "is_storable": True,
                "list_price": 200.0,
                "taxes_id": [(6, 0, [])],
            }
        )
        prod.product_tmpl_id.grove_consult_built = True
        return prod

    def _per_order_code(self, code, amount):
        """A with-code promotion knocking a flat ``amount`` off the order, so the
        after-promo goods subtotal can be tuned to an exact dollar for the
        eligibility boundary."""
        return (
            self.env["loyalty.program"]
            .with_company(self.company)
            .create(
                {
                    "name": f"Promo {code}",
                    "program_type": "promotion",
                    "trigger": "with_code",
                    "applies_on": "current",
                    "company_id": self.company.id,
                    "rule_ids": [(0, 0, {"mode": "with_code", "code": code})],
                    "reward_ids": [
                        (
                            0,
                            0,
                            {
                                "reward_type": "discount",
                                "discount": amount,
                                "discount_mode": "per_order",
                                "discount_applicability": "order",
                            },
                        )
                    ],
                }
            )
        )

    def _cart_payload(self, items, **extra):
        payload = {
            "contact": {"name": "Consult Test", "email": "consult-line@example.com", "phone": "3045551212"},
            "items": items,
            "fulfillment": "pickup",
        }
        payload.update(extra)
        return payload

    # ── D.1 the product ────────────────────────────────────────────────────

    def test_product_is_a_zero_service_unpublished_gate_exempt(self):
        self.assertEqual(self.consult.type, "service")
        self.assertEqual(self.consult.list_price, 0.0)
        self.assertTrue(self.consult.sale_ok)
        self.assertFalse(self.consult.website_published)
        self.assertTrue(self.consult.grove_gate_exempt)
        self.assertFalse(self.consult.taxes_id)
        self.assertEqual(self.consult.company_id, self.env.ref("grove_headless.company_nursery"))

    def test_service_line_is_not_a_tree(self):
        """The service never counts as a tree for the deposit rule, the bundle
        tree count, or the box plan — every tree-counting path filters on
        storable products, which a service is not (spec D.1)."""
        # Not storable → the box plan (which only packs storables) skips it.
        self.assertFalse(self.consult_variant.is_storable)
        # Tree count: 0 for the service, and adding it to a cart does not change
        # the qualifying tree count.
        self.assertEqual(promotions.variant_tree_count(self.consult_variant), 0)
        order = self._order([(self.tree, 1), (self.consult_variant, 1)], company=self.nursery)
        self.assertEqual(promotions.qualifying_tree_count(order), 1)
        # Deposit rule: a $0 service never triggers the deposit, and adding it to
        # a deposit-triggering line leaves the reason untouched.
        today = grove_main._date.today()
        self.assertIsNone(
            grove_main._deposit_reason_for_lines(self.env, [(self.consult_variant, 1)], "pickup", today, None)
        )

    # ── D.2 eligibility ──────────────────────────────────────────────────────

    def test_eligibility_boundary_after_promo(self):
        """$499.99 is not eligible, $500.00 is — measured on the goods subtotal
        AFTER a promo (spec D.2 acceptance)."""
        big = self.env["product.product"].create(
            {"name": "Orchard start", "type": "consu", "is_storable": True, "list_price": 600.0}
        )
        self._per_order_code("CONSULT100", 100.01)
        order = self._order([(big, 1)])
        result = promotions.resolve_discounts(order, "CONSULT100")
        self.assertEqual(result["applied"], "code")
        # 600.00 - 100.01 = 499.99 -> below threshold
        self.assertFalse(order._grove_consult_eligible())

        self._per_order_code("CONSULT00", 100.0)
        order2 = self._order([(big, 1)])
        promotions.resolve_discounts(order2, "CONSULT00")
        # 600.00 - 100.00 = 500.00 -> at the threshold, eligible
        self.assertTrue(order2._grove_consult_eligible())

    def test_threshold_zero_disables_the_subtotal_offer(self):
        self.icp.set_param("grove_headless.consult_threshold", "0")
        order = self._order([(self.tree, 1)])
        order.order_line[0].price_unit = 9999.0
        order.invalidate_recordset(["amount_untaxed", "amount_total"])
        self.assertFalse(order._grove_consult_eligible())

    def test_consult_built_cart_is_always_eligible(self):
        """An agroforestry plan arrives as a grove_consult_built line; it is
        eligible regardless of subtotal (even under the kill switch)."""
        self.icp.set_param("grove_headless.consult_threshold", "0")
        order = self._order([(self._consult_built_product(), 1)])
        self.assertTrue(order._grove_consult_eligible())

    def test_create_draft_order_rejects_the_consult_line_when_ineligible(self):
        payload = self._cart_payload([{"variant_id": self.consult_variant.id, "quantity": 1}])
        with mute_logger("odoo.addons.grove_headless.controllers.main"):
            order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(order)
        self.assertIsNotNone(error)
        self.assertEqual(error.status_code, 400)
        self.assertIn("consult", error.get_data(as_text=True).lower())

    def test_create_draft_order_accepts_the_consult_line_when_eligible(self):
        big_tree = self.env["product.product"].create(
            {
                "name": "Specimen tree",
                "type": "consu",
                "is_storable": True,
                "list_price": 500.0,
                "taxes_id": [(6, 0, [])],
            }
        )
        payload = self._cart_payload(
            [
                {"variant_id": big_tree.id, "quantity": 1},
                {"variant_id": self.consult_variant.id, "quantity": 1},
            ]
        )
        order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(error)
        self.assertTrue(order)
        self.assertTrue(order.grove_consult_requested)

    # ── D.3 order flag, digest, email ───────────────────────────────────────

    def test_grove_consult_requested_computed(self):
        with_consult = self._order([(self.tree, 1), (self.consult_variant, 1)], company=self.nursery)
        without = self._order([(self.tree, 1)])
        self.assertTrue(with_consult.grove_consult_requested)
        self.assertFalse(without.grove_consult_requested)

    def test_booking_block_only_when_requested_and_link_carries_order(self):
        # Pure helper: link carries ?order=<name>, & when the URL already has a query.
        self.assertEqual(grove_main.consult_booking_block("S00412", ""), "")
        block = grove_main.consult_booking_block("S00412", "https://cal.example/appt")
        self.assertIn("?order=S00412", block)
        self.assertIn("Book your planting consult", block)
        with_query = grove_main.consult_booking_block("S00412", "https://cal.example/appt?x=1")
        self.assertIn("&order=S00412", with_query)

    def test_booking_email_sent_only_when_requested(self):
        self.icp.set_param("grove_headless.consult_booking_url", "https://cal.example/appt")
        requested = self._order([(self.consult_variant, 1)], company=self.nursery)
        with mute_logger(
            "odoo.addons.grove_headless.controllers.main", "odoo.addons.grove_headless.models.grove_email_log"
        ):
            grove_main._notify_consult_booking(self.env, requested)
        logs = self.env["grove.email.log"].search([("order_id", "=", requested.id), ("kind", "=", "consult")])
        self.assertEqual(len(logs), 1)

        not_requested = self._order([(self.tree, 1)])
        grove_main._notify_consult_booking(self.env, not_requested)
        self.assertFalse(
            self.env["grove.email.log"].search([("order_id", "=", not_requested.id), ("kind", "=", "consult")])
        )

    def test_digest_tags_consult_requests(self):
        digest = order_digest.build_digest(
            [
                {
                    "id": 1,
                    "name": "S00412",
                    "date_order": order_digest.date.today(),
                    "amount_total": 500.0,
                    "partner_name": "Jane Grower",
                    "grove_consult_requested": True,
                },
                {
                    "id": 2,
                    "name": "S00413",
                    "date_order": order_digest.date.today(),
                    "amount_total": 20.0,
                    "partner_name": "No Consult",
                    "grove_consult_requested": False,
                },
            ],
            today=order_digest.date.today(),
        )
        self.assertEqual(digest["consult_request_count"], 1)
        self.assertEqual(digest["consult_requests"][0]["order_name"], "S00412")
        self.assertIn("Consult requested", order_digest.render_digest_text(digest))
        self.assertIn("S00412", order_digest.render_digest_html(digest))

    # ── C.4 planner plan ─────────────────────────────────────────────────────

    def test_planner_plan_stored_and_shown_on_a_consult_built_order(self):
        built = self._consult_built_product()
        plan = {
            "url": "https://nursery.example/plan?zip=26501&track=agro",
            "lines": [{"id": self.tree.product_tmpl_id.id, "qty": 3}],
            "zone": 6,
            "state": "WV",
            "site": "open",
            "track": "agro",
            "subgoal": "silvopasture",
            "timing": "mixed",
        }
        payload = self._cart_payload([{"variant_id": built.id, "quantity": 1}], plan=plan)
        order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(error)
        self.assertEqual(order.grove_planner_plan, plan)
        self.assertEqual(order.grove_planner_plan_url, plan["url"])
        self.assertIn("agro", order.grove_planner_plan_summary)

    def test_planner_plan_ignored_without_a_consult_built_line(self):
        payload = self._cart_payload(
            [{"variant_id": self.tree.id, "quantity": 1}],
            plan={"url": "https://nursery.example/plan", "lines": []},
        )
        order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        self.assertIsNone(error)
        self.assertFalse(order.grove_planner_plan)
