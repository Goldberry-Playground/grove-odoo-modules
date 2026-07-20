from odoo import api, fields, models

FORMAT_ATTRIBUTE = "Format"
BAREROOT_VALUE = "Bareroot"


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
            else:
                product.grove_effective_shipping_tier = product.product_tmpl_id.grove_shipping_tier or "potted"
