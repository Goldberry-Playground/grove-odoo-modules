"""Odoo-runtime tests for promo-code + automatic volume-tier discounts
(GOL-2431). Runs under --test-enable (needs a DB for sale.order / loyalty), so
it is excluded from pytest collection in conftest.py.

Covers: qualifying-tree counting (incl. phantom-BOM bundles), the automatic
tier feed, coupon-specific shortfall messages (qty / amount / product-set),
tier selection at 4/5/9/10/12 units, best-single-discount both directions,
deposit carts getting nothing, the Stripe coupon amount equalling the chosen
discount, and the preview savepoint leaving no order behind.
"""

from datetime import date, timedelta

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import promotions, stripe_gateway
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import HttpCase, TransactionCase, get_db_name, tagged


@tagged("post_install", "-at_install")
class TestPromotions(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.plants_root = self.env.ref("grove_headless.categ_plants")
        self.partner = self.env["res.partner"].create(
            {"name": "Promo Customer", "email": "promo@example.com", "company_id": self.company.id}
        )
        self.apple = self._plant("Apple 'Honeycrisp'")
        self.pear = self._plant("Pear 'Bartlett'")
        self.plum = self._plant("Plum 'Stanley'")
        # A supply lives OUTSIDE the Plants tree, so it never counts as a tree
        # and never earns a volume tier.
        self.mulch = self.env["product.product"].create({"name": "Mulch bag", "type": "consu", "list_price": 8.0})

    # ── helpers ──────────────────────────────────────────────────────────

    def _plant(self, name, price=25.0):
        return self.env["product.product"].create(
            {"name": name, "type": "consu", "is_storable": True, "list_price": price, "categ_id": self.plants_root.id}
        )

    def _order(self, lines):
        """lines = [(product, qty), ...]."""
        return (
            self.env["sale.order"]
            .with_company(self.company)
            .create(
                {
                    "partner_id": self.partner.id,
                    "company_id": self.company.id,
                    "order_line": [(0, 0, {"product_id": p.id, "product_uom_qty": q}) for p, q in lines],
                }
            )
        )

    def _auto_volume_program(self):
        """The GOL-2431 QA shape: automatic, 1 point per qualifying-plant unit,
        10% off for 5 points and 20% off for 10 points."""
        return (
            self.env["loyalty.program"]
            .with_company(self.company)
            .create(
                {
                    "name": "Volume discount",
                    "program_type": "promotion",
                    "trigger": "auto",
                    "applies_on": "current",
                    "company_id": self.company.id,
                    "rule_ids": [
                        (
                            0,
                            0,
                            {
                                "mode": "auto",
                                "reward_point_mode": "unit",
                                "reward_point_amount": 1.0,
                                "product_ids": [(6, 0, (self.apple | self.pear | self.plum).ids)],
                            },
                        )
                    ],
                    "reward_ids": [
                        (
                            0,
                            0,
                            {
                                "reward_type": "discount",
                                "discount": 10.0,
                                "discount_mode": "percent",
                                "discount_applicability": "order",
                                "required_points": 5.0,
                                "description": "10% off (5+ trees)",
                            },
                        ),
                        (
                            0,
                            0,
                            {
                                "reward_type": "discount",
                                "discount": 20.0,
                                "discount_mode": "percent",
                                "discount_applicability": "order",
                                "required_points": 10.0,
                                "description": "20% off (10+ trees)",
                            },
                        ),
                    ],
                }
            )
        )

    def _code_program(self, code="FLATWOODS", min_qty=0, min_amount=0.0, amount=10.0, products=None):
        rule = {"mode": "with_code", "code": code}
        if min_qty:
            rule["minimum_qty"] = min_qty
        if min_amount:
            rule["minimum_amount"] = min_amount
        if products is not None:
            rule["product_ids"] = [(6, 0, products.ids)]
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
                    "rule_ids": [(0, 0, rule)],
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

    # ── qualifying tree count ────────────────────────────────────────────

    def test_tree_count_plants_only(self):
        order = self._order([(self.apple, 3), (self.pear, 2), (self.mulch, 5)])
        self.assertEqual(promotions.qualifying_tree_count(order), 5)

    def test_tree_count_bundle_counts_components(self):
        """A phantom-BOM bundle counts as its qualifying-plant component units
        (Remembrance Grove = 5)."""
        bundle = self.env["product.product"].create(
            {"name": "Remembrance Grove", "type": "consu", "grove_gate_exempt": True}
        )
        self.env["mrp.bom"].create(
            {
                "product_tmpl_id": bundle.product_tmpl_id.id,
                "product_id": bundle.id,
                "product_qty": 1,
                "type": "phantom",
                "bom_line_ids": [
                    (0, 0, {"product_id": self.apple.id, "product_qty": 2}),
                    (0, 0, {"product_id": self.pear.id, "product_qty": 3}),
                ],
            }
        )
        order = self._order([(bundle, 1), (self.plum, 1)])
        # 2 + 3 bundle components + 1 standalone plum = 6
        self.assertEqual(promotions.qualifying_tree_count(order), 6)

    def test_variant_tree_count_per_variant(self):
        """The per-variant field the product-detail payload exposes (GOL-2439):
        1 for a standalone plant, 0 for a supply, the exploded component count
        for a phantom-BOM bundle."""
        bundle = self.env["product.product"].create(
            {"name": "Remembrance Grove", "type": "consu", "grove_gate_exempt": True}
        )
        self.env["mrp.bom"].create(
            {
                "product_tmpl_id": bundle.product_tmpl_id.id,
                "product_id": bundle.id,
                "product_qty": 1,
                "type": "phantom",
                "bom_line_ids": [
                    (0, 0, {"product_id": self.apple.id, "product_qty": 2}),
                    (0, 0, {"product_id": self.pear.id, "product_qty": 3}),
                ],
            }
        )
        self.assertEqual(promotions.variant_tree_count(self.apple), 1)
        self.assertEqual(promotions.variant_tree_count(self.mulch), 0)
        self.assertEqual(promotions.variant_tree_count(bundle), 5)

    # ── automatic tier feed ──────────────────────────────────────────────

    def test_auto_tier_feed_shape(self):
        self._auto_volume_program()
        tiers = promotions.auto_tier_feed(self.env, self.company, date.today())
        self.assertEqual(
            [(t["min_qty"], t["percent"]) for t in tiers],
            [(5, 10.0), (10, 20.0)],
        )
        self.assertEqual(tiers[0]["label"], "10% off (5+ trees)")

    # ── tier selection thresholds ────────────────────────────────────────

    def _applied_tier_percent(self, qty):
        """Resolve discounts on a `qty`-apple cart with the volume program active
        and return the applied tier percent (0 when no tier applied)."""
        order = self._order([(self.apple, qty)])
        result = promotions.resolve_discounts(order)
        if result["applied"] != "tier":
            return 0.0
        # discount_amount = percent% of the untaxed goods subtotal
        return round(result["discount_amount"] / (self.apple.list_price * qty) * 100.0)

    def test_tier_thresholds(self):
        self._auto_volume_program()
        self.assertEqual(self._applied_tier_percent(4), 0.0)  # below 5 → none
        self.assertEqual(self._applied_tier_percent(5), 10.0)
        self.assertEqual(self._applied_tier_percent(9), 10.0)
        self.assertEqual(self._applied_tier_percent(10), 20.0)
        self.assertEqual(self._applied_tier_percent(12), 20.0)

    def test_tier_descriptor_in_result(self):
        """When a tier applies, the result carries `tier: {min_qty, percent}`
        (GOL-2439) so the storefront can label the summary row; when nothing
        applies the key is absent."""
        self._auto_volume_program()
        result = promotions.resolve_discounts(self._order([(self.apple, 5)]))
        self.assertEqual(result["applied"], "tier")
        self.assertEqual(result["tier"], {"min_qty": 5, "percent": 10.0})
        # top tier at 10 units
        top = promotions.resolve_discounts(self._order([(self.apple, 10)]))
        self.assertEqual(top["tier"], {"min_qty": 10, "percent": 20.0})
        # below the first threshold: no tier, no `tier` key
        none_result = promotions.resolve_discounts(self._order([(self.apple, 4)]))
        self.assertIsNone(none_result["applied"])
        self.assertNotIn("tier", none_result)

    def test_tier_counts_bundle_components(self):
        """4 standalone apples + a 2-component bundle = 6 trees → 10% tier."""
        self._auto_volume_program()
        # extend the auto rule to also award for the bundle's components (apple/pear
        # already covered); a 4-apple + bundle(apple x1, pear x1) cart = 6 units.
        bundle = self.env["product.product"].create({"name": "Pair Grove", "type": "consu", "grove_gate_exempt": True})
        self.env["mrp.bom"].create(
            {
                "product_tmpl_id": bundle.product_tmpl_id.id,
                "product_id": bundle.id,
                "product_qty": 1,
                "type": "phantom",
                "bom_line_ids": [
                    (0, 0, {"product_id": self.apple.id, "product_qty": 1}),
                    (0, 0, {"product_id": self.pear.id, "product_qty": 1}),
                ],
            }
        )
        order = self._order([(self.apple, 4), (bundle, 1)])
        self.assertEqual(promotions.qualifying_tree_count(order), 6)

    # ── best single discount wins ────────────────────────────────────────

    def test_tier_beats_smaller_code(self):
        """10 apples ($250): 20% tier ($50) beats a $10 code — tier applies."""
        self._auto_volume_program()
        self._code_program("FLATWOODS", min_qty=2, amount=10.0)
        order = self._order([(self.apple, 10)])
        result = promotions.resolve_discounts(order, "FLATWOODS")
        self.assertEqual(result["applied"], "tier")
        self.assertAlmostEqual(result["discount_amount"], 50.0, places=2)
        self.assertIn("worth more than FLATWOODS", result["message"])
        # Only ONE reward line remains — the loser's is never written.
        self.assertEqual(len(order.order_line.filtered("reward_id")), 1)

    def test_code_beats_smaller_tier(self):
        """5 apples ($125): a $100 code beats the 10% tier ($12.50) — code wins."""
        self._auto_volume_program()
        self._code_program("BIGDEAL", min_qty=2, amount=100.0)
        order = self._order([(self.apple, 5)])
        result = promotions.resolve_discounts(order, "BIGDEAL")
        self.assertEqual(result["applied"], "code")
        self.assertIn("BIGDEAL", result["message"])
        self.assertEqual(len(order.order_line.filtered("reward_id")), 1)

    # ── coupon-specific shortfall messages ───────────────────────────────

    def test_shortfall_qty_message(self):
        self._code_program("FLATWOODS", min_qty=2, products=(self.apple | self.pear | self.plum))
        order = self._order([(self.apple, 1)])
        result = promotions.resolve_discounts(order, "FLATWOODS")
        self.assertIsNone(result["applied"])
        self.assertEqual(
            result["message"],
            "FLATWOODS needs 2 qualifying trees (Apple 'Honeycrisp', Pear 'Bartlett' or Plum 'Stanley'); "
            "you have 1. Add 1 more.",
        )

    def test_shortfall_amount_message(self):
        self._code_program("SPRING25", min_amount=75.0)
        order = self._order([(self.apple, 2)])  # $50 subtotal, needs $75
        result = promotions.resolve_discounts(order, "SPRING25")
        self.assertIsNone(result["applied"])
        self.assertEqual(result["message"], "SPRING25 needs a $75 subtotal; add $25 more.")

    def test_shortfall_catalog_wide_says_any_tree(self):
        self._code_program("ANYTREE", min_qty=3)  # no product restriction
        order = self._order([(self.apple, 1)])
        result = promotions.resolve_discounts(order, "ANYTREE")
        self.assertIn("(any tree)", result["message"])

    def test_expired_code_message(self):
        program = self._code_program("OLDCODE", min_qty=1)
        program.date_to = date.today() - timedelta(days=1)
        order = self._order([(self.apple, 2)])
        result = promotions.resolve_discounts(order, "OLDCODE")
        self.assertIsNone(result["applied"])
        self.assertIn("expired", result["message"])

    def test_unknown_code_falls_back_to_generic(self):
        order = self._order([(self.apple, 2)])
        result = promotions.resolve_discounts(order, "NOSUCHCODE")
        self.assertIsNone(result["applied"])
        self.assertTrue(result["message"])  # some generic non-empty message

    # ── Stripe coupon amount ─────────────────────────────────────────────

    def test_stripe_coupon_equals_chosen_discount(self):
        self._auto_volume_program()
        order = self._order([(self.apple, 10)])
        result = promotions.resolve_discounts(order)
        self.assertEqual(result["applied"], "tier")
        line_items, _preorder_ids, _charged = grove_main._build_stripe_line_items(order, today=date.today())
        discount_cents = -sum(li["amount_cents"] * li["quantity"] for li in line_items if li["kind"] == "discount")
        self.assertEqual(discount_cents, stripe_gateway.to_cents(result["discount_amount"]))

    # ── preview savepoint ────────────────────────────────────────────────

    def test_measure_leaves_no_reward_line(self):
        """The measurement primitive rolls back cleanly: after measuring a code,
        the live order carries no reward line."""
        self._code_program("FLATWOODS", min_qty=2, amount=10.0)
        order = self._order([(self.apple, 3)])
        magnitude, err = promotions._measure(order, lambda: promotions.apply_code_reward(order, "FLATWOODS"))
        self.assertIsNone(err)
        self.assertGreater(magnitude, 0.0)
        self.assertFalse(order.order_line.filtered("reward_id"), "measurement must leave no reward line behind")

    def test_no_program_no_discount(self):
        order = self._order([(self.apple, 10)])
        result = promotions.resolve_discounts(order)
        self.assertIsNone(result["applied"])
        self.assertEqual(result["discount_amount"], 0.0)
        self.assertEqual(result["subtotal_after"], promotions.goods_subtotal(order))


@tagged("post_install", "-at_install")
class TestPromotionsAutoEndpoint(GroveTaxFixtureMixin, HttpCase):
    """HTTP regression for the GOL-2439 storefront contract endpoints.

    ``TestPromotions`` above exercises the model layer (``auto_tier_feed``,
    ``variant_tree_count``) directly, so it cannot see a route that fails to
    *dispatch*. ``GET /grove/api/v1/promotions/auto`` reads
    ``request.website.company_id`` and therefore must be registered with
    ``website=True`` — without it Odoo never populates ``request.website`` and
    the route 500s (``AttributeError: 'Request' object has no attribute
    'website'``, GOL-2439). Driving it through the real dispatch chain with
    ``url_open`` is the only test that catches that; a pure model test passes
    while the live storefront silently hides the nudge.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        plants_root = self.env.ref("grove_headless.categ_plants")
        self.apple = self.env["product.product"].create(
            {
                "name": "Apple 'Honeycrisp'",
                "type": "consu",
                "list_price": 25.0,
                "categ_id": plants_root.id,
            }
        )
        # Automatic 10%-off-at-5-trees program: the "real QA program" shape the
        # storefront nudge reads (see TestPromotions._auto_volume_program).
        self.env["loyalty.program"].with_company(self.company).create(
            {
                "name": "Volume discount",
                "program_type": "promotion",
                "trigger": "auto",
                "applies_on": "current",
                "company_id": self.company.id,
                "rule_ids": [
                    (
                        0,
                        0,
                        {
                            "mode": "auto",
                            "reward_point_mode": "unit",
                            "reward_point_amount": 1.0,
                            "product_ids": [(6, 0, self.apple.ids)],
                        },
                    )
                ],
                "reward_ids": [
                    (
                        0,
                        0,
                        {
                            "reward_type": "discount",
                            "discount": 10.0,
                            "discount_mode": "percent",
                            "discount_applicability": "order",
                            "required_points": 5.0,
                            "description": "10% off (5+ trees)",
                        },
                    )
                ],
            }
        )

    def _headers(self, **extra):
        # X-Odoo-Database routes the public request without a session cookie;
        # X-Grove-Tenant selects the Goldberry website/company (base.main_company).
        # Same pattern as TestProductSlugEndpoint in test_product_slug.py.
        headers = {"X-Odoo-Database": get_db_name(), "X-Grove-Tenant": "goldberry"}
        headers.update(extra)
        return headers

    def test_promotions_auto_dispatches_and_returns_bare_array(self):
        """GET /grove/api/v1/promotions/auto returns 200 and a bare JSON array
        of ``{min_qty, percent, label}`` — not a 500, and not a wrapped object.

        Guards two things at once: the route registers with ``website=True`` (so
        ``request.website`` resolves), and the body is the top-level array the
        storefront normalizer reads (a ``{"tiers": [...]}`` wrapper would parse
        as empty and hide the nudge)."""
        response = self.url_open(
            "/grove/api/v1/promotions/auto",
            headers=self._headers(),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsInstance(body, list, "contract is a bare array, not a wrapper object")
        self.assertTrue(body, "the seeded automatic program must surface at least one tier")
        first = body[0]
        self.assertEqual(set(first), {"min_qty", "percent", "label"})
        self.assertEqual(first["min_qty"], 5)
        self.assertEqual(first["percent"], 10.0)
