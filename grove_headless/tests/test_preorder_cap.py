"""Preorder cap (GOL-2171): a product sells out once its preorder count exceeds
its cap. Global default in ir.config_parameter (seed 50), per-template override
wins; 0/unset inherits the global. Threshold is strict > (the 51st preorder
flips a cap of 50). Counting rule ratified by Josh 2026-09-07: sum deposit-paid
preorder line quantities, rolled up to the template."""

from odoo.addons.grove_headless.models.product_template import (
    PREORDER_CAP_PARAM,
    PREORDER_CAP_SEED,
    _parse_preorder_variant_ids,
)
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin


@tagged("post_install", "-at_install")
class TestPreorderCap(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create({"name": "Preorder Buyer", "email": "pre@example.com"})
        self.tmpl = self.env["product.template"].create({"name": "Elderberry", "type": "consu"})
        self.variant = self.tmpl.product_variant_id
        # A second template to prove roll-up isolation (its preorders never leak
        # into Elderberry's count).
        self.other = self.env["product.template"].create({"name": "Persimmon", "type": "consu"})

    # ── helpers ──────────────────────────────────────────────────────────
    def _preorder(self, variant, qty, *, status="deposit_paid", in_deposit_set=True):
        """Create an order carrying ``qty`` of ``variant`` at ``status``. When
        ``in_deposit_set`` the variant is listed in grove_preorder_variant_ids
        (i.e. it was charged as a deposit)."""
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "order_line": [(0, 0, {"product_id": variant.id, "product_uom_qty": qty})],
            }
        )
        order.write(
            {
                "grove_checkout_status": status,
                "grove_preorder_variant_ids": str(variant.id) if in_deposit_set else "",
            }
        )
        return order

    # ── the production trap: unset override must inherit, not sell out ─────
    def test_unset_override_inherits_global_default_not_zero(self):
        """A brand-new product with no cap set and no preorders behaves as the
        global 50 — never as an instant 'cap of zero' sold-out (Q2 emphasis)."""
        self.assertEqual(self.tmpl.grove_preorder_cap, 0)
        self.assertEqual(self.tmpl.grove_preorder_count, 0)
        self.assertEqual(self.tmpl.grove_preorder_cap_effective, PREORDER_CAP_SEED)
        self.assertFalse(self.tmpl.grove_preorder_cap_reached)

    def test_unset_override_under_and_at_and_over_default(self):
        # 50 units against a cap of 50 is NOT sold out (strict >).
        self._preorder(self.variant, PREORDER_CAP_SEED)
        self.tmpl.invalidate_recordset()
        self.assertEqual(self.tmpl.grove_preorder_count, PREORDER_CAP_SEED)
        self.assertFalse(self.tmpl.grove_preorder_cap_reached)
        # One more unit (the 51st) flips it.
        self._preorder(self.variant, 1)
        self.tmpl.invalidate_recordset()
        self.assertEqual(self.tmpl.grove_preorder_count, PREORDER_CAP_SEED + 1)
        self.assertTrue(self.tmpl.grove_preorder_cap_reached)

    # ── per-product override wins over the global ─────────────────────────
    def test_override_wins_over_global(self):
        self.tmpl.grove_preorder_cap = 20
        self._preorder(self.variant, 20)
        self.tmpl.invalidate_recordset()
        self.assertEqual(self.tmpl.grove_preorder_cap_effective, 20)
        self.assertFalse(self.tmpl.grove_preorder_cap_reached)  # 20 is not > 20
        self._preorder(self.variant, 1)
        self.tmpl.invalidate_recordset()
        self.assertTrue(self.tmpl.grove_preorder_cap_reached)  # 21 > 20

    def test_override_can_exceed_global(self):
        # Elderberry can hold 100 preorders even though the global is 50.
        self.tmpl.grove_preorder_cap = 100
        self._preorder(self.variant, 60)
        self.tmpl.invalidate_recordset()
        self.assertEqual(self.tmpl.grove_preorder_cap_effective, 100)
        self.assertFalse(self.tmpl.grove_preorder_cap_reached)

    # ── counting rule: only deposit-paid deposit-set lines count ──────────
    def test_non_deposit_paid_orders_excluded(self):
        for status in ("pending", "paid", "expired", "refunded_oversell"):
            self._preorder(self.variant, 100, status=status)
        self.tmpl.invalidate_recordset()
        self.assertEqual(self.tmpl.grove_preorder_count, 0)
        self.assertFalse(self.tmpl.grove_preorder_cap_reached)

    def test_deposit_paid_but_variant_not_in_deposit_set_excluded(self):
        # A full-charge line on an otherwise deposit_paid order (variant absent
        # from grove_preorder_variant_ids) is not a preorder → not counted.
        self._preorder(self.variant, 100, in_deposit_set=False)
        self.tmpl.invalidate_recordset()
        self.assertEqual(self.tmpl.grove_preorder_count, 0)

    def test_rollup_isolated_per_template(self):
        self._preorder(self.variant, 5)
        self._preorder(self.other.product_variant_id, 99)
        self.env.invalidate_all()
        self.assertEqual(self.tmpl.grove_preorder_count, 5)
        self.assertEqual(self.other.grove_preorder_count, 99)
        self.assertTrue(self.other.grove_preorder_cap_reached)  # 99 > 50
        self.assertFalse(self.tmpl.grove_preorder_cap_reached)

    # ── global default is adjustable in Odoo ──────────────────────────────
    def test_global_param_is_adjustable(self):
        self.env["ir.config_parameter"].sudo().set_param(PREORDER_CAP_PARAM, "10")
        self._preorder(self.variant, 10)
        self.tmpl.invalidate_recordset()
        self.assertEqual(self.tmpl.grove_preorder_cap_effective, 10)
        self.assertFalse(self.tmpl.grove_preorder_cap_reached)
        self._preorder(self.variant, 1)
        self.tmpl.invalidate_recordset()
        self.assertTrue(self.tmpl.grove_preorder_cap_reached)  # 11 > 10

    def test_global_param_missing_or_garbage_falls_back_to_seed(self):
        Param = self.env["ir.config_parameter"].sudo()
        Tmpl = self.env["product.template"]
        Param.set_param(PREORDER_CAP_PARAM, "not-a-number")
        self.assertEqual(Tmpl._grove_global_preorder_cap(), PREORDER_CAP_SEED)
        Param.set_param(PREORDER_CAP_PARAM, "")
        self.assertEqual(Tmpl._grove_global_preorder_cap(), PREORDER_CAP_SEED)
        # Row deleted entirely (get_param returns the seed default / False) must
        # NOT collapse to int(False)==0 and silently disable the cap site-wide.
        Param.search([("key", "=", PREORDER_CAP_PARAM)]).unlink()
        self.assertEqual(Tmpl._grove_global_preorder_cap(), PREORDER_CAP_SEED)

    def test_global_zero_disables_cap(self):
        # An explicit 0 is the deliberate off switch: never sells out on count.
        self.env["ir.config_parameter"].sudo().set_param(PREORDER_CAP_PARAM, "0")
        self._preorder(self.variant, 1000)
        self.tmpl.invalidate_recordset()
        self.assertEqual(self.tmpl.grove_preorder_cap_effective, 0)
        self.assertFalse(self.tmpl.grove_preorder_cap_reached)

    # ── parse helper ──────────────────────────────────────────────────────
    def test_parse_preorder_variant_ids(self):
        self.assertEqual(_parse_preorder_variant_ids("1, 2 ,3"), {1, 2, 3})
        self.assertEqual(_parse_preorder_variant_ids(""), set())
        self.assertEqual(_parse_preorder_variant_ids(None), set())
        self.assertEqual(_parse_preorder_variant_ids("1,,x,2"), {1, 2})
