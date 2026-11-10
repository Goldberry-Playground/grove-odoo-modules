"""product.template.attribute.value hook: signal variant price changes.

A variant's storefront price is `lst_price` = template `list_price` + the sum of
its attribute values' `price_extra`. Editing a `price_extra` never flows through
`product.template.write`, so snapshot the affected templates' signature BEFORE
the write and let `grove.publish.event` emit `product.availability` at commit if
any variant price actually moved (storefront shared cache, 2026-11-09).
"""

from odoo import models


class ProductTemplateAttributeValue(models.Model):
    _inherit = "product.template.attribute.value"

    def write(self, vals):
        if "price_extra" in vals:
            self.env["grove.publish.event"].sudo().note_availability_candidates(self.product_tmpl_id)
        return super().write(vals)
