from odoo import api, fields, models

FORMAT_ATTRIBUTE = "Format"
BAREROOT_VALUE = "Bareroot"
POTTED_VALUE = "Potted"


class ProductProduct(models.Model):
    _inherit = "product.product"

    # The zone-rate engine bills bareroot as a 4 lb slim box and potted as a
    # ~25 lb box. Format is a VARIANT axis on live data, so the tier must be
    # resolved per-variant — the template field alone quotes bareroot pears
    # at potted rates (bug found in the 2026-07-13 design review).
    grove_effective_shipping_tier = fields.Selection(
        [("bareroot", "Bareroot"), ("potted", "Potted")],
        compute="_compute_grove_effective_shipping_tier",
        string="Effective Shipping Tier",
    )

    @api.depends("product_template_variant_value_ids", "product_tmpl_id.grove_shipping_tier")
    def _compute_grove_effective_shipping_tier(self):
        for product in self:
            fmt_values = product.product_template_variant_value_ids.filtered(
                lambda v: v.attribute_id.name == FORMAT_ATTRIBUTE
            )
            if fmt_values and fmt_values[0].name == BAREROOT_VALUE:
                product.grove_effective_shipping_tier = "bareroot"
            elif fmt_values and fmt_values[0].name == POTTED_VALUE:
                # Symmetric override (Josh 2026-09-06): a Potted variant on a
                # template whose tier is "bareroot" (apples, pears) inherited
                # "bareroot" via the fallback below, so the storefront badged it
                # "Peat & bagged" and quoted shipping — Potted is pickup-only.
                product.grove_effective_shipping_tier = "potted"
            else:
                product.grove_effective_shipping_tier = product.product_tmpl_id.grove_shipping_tier or "potted"

    def _grove_potted_pool_siblings(self):
        """Variants sharing this bareroot variant's PHYSICAL stock pool.

        Peat & bagged (GOL-2031): the trees that ship as "Bareroot" during the
        leafed season ARE the potted stock — staff pull the tree from its pot
        and bag the root ball in damp peat at packing (Josh 2026-09-06). So a
        Bareroot variant's sellable pool must include its non-Bareroot sibling:
        same template, identical on every axis except Format. Empty for
        non-bareroot variants and for templates with no Format axis.
        """
        self.ensure_one()
        if self.grove_effective_shipping_tier != "bareroot":
            return self.browse()

        def non_format(variant):
            return variant.product_template_variant_value_ids.filtered(
                lambda v: v.attribute_id.name != FORMAT_ATTRIBUTE
            )

        mine = non_format(self)
        return self.product_tmpl_id.product_variant_ids.filtered(
            lambda s: s.id != self.id and s.grove_effective_shipping_tier != "bareroot" and non_format(s) == mine
        )

    def grove_shared_pool_qty(self, field="free_qty"):
        """Sellable quantity for this variant including its shared pool.

        ``field`` is "free_qty" for the money paths (checkout deposit gate,
        webhook oversell check) and "qty_available" for the PDP display. The
        ships-now vs preorder question stays with the shipping calendar — this
        only answers "how many trees back this option". Company context is the
        caller's: invoke on a ``with_company()`` record where it matters.
        """
        self.ensure_one()
        return self[field] + sum(s[field] for s in self._grove_potted_pool_siblings())
