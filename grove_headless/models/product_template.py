from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    grove_featured = fields.Boolean(
        string="Grove Featured",
        default=False,
        help="Mark this product as featured in the headless storefront.",
    )
    grove_seo_description = fields.Text(
        string="Grove SEO Description",
        translate=True,
        help="SEO-optimized description used by the headless frontend meta tags.",
    )
