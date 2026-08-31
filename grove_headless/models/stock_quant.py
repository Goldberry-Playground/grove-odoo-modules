"""stock.quant hook: emit `product.availability` when on-hand crosses zero.

Storefront availability ("In stock" on the /shop grid) is `qty_available > 0`,
which is computed from `stock.quant.quantity` — it never flows through
`product.template.write`, so a template-write override could not see a sellout.
We hook the quant instead: capture the affected templates' availability BEFORE
the on-hand quantity changes, then let `grove.publish.event` emit once per
template that actually crossed the boundary at transaction commit (GOL-1896).

We only note the change when `quantity` (on-hand) is touched — a reservation
writes `reserved_quantity`, which moves `free_qty` but not `qty_available`, so
the hot order-reservation path skips this entirely and the sellout webhook fires
when stock physically leaves (delivery validation, scrap, inventory adjustment).
"""

from odoo import api, models

# Quant fields whose change can move on-hand `qty_available` across zero.
_ONHAND_FIELDS = frozenset({"quantity", "inventory_quantity", "inventory_quantity_auto_apply"})


class StockQuant(models.Model):
    _inherit = "stock.quant"

    def _grove_note_availability(self, templates):
        if templates:
            self.env["grove.publish.event"].sudo().note_availability_candidates(templates)

    @api.model_create_multi
    def create(self, vals_list):
        # A new quant is created BEFORE we can read it, so resolve the affected
        # templates from the incoming values and snapshot their pre-create
        # availability (from any existing quants) first.
        product_ids = [vals.get("product_id") for vals in vals_list if vals.get("product_id")]
        if product_ids:
            templates = self.env["product.product"].browse(product_ids).exists().product_tmpl_id
            self._grove_note_availability(templates)
        return super().create(vals_list)

    def write(self, vals):
        if _ONHAND_FIELDS.intersection(vals):
            self._grove_note_availability(self.product_id.product_tmpl_id)
        return super().write(vals)

    def unlink(self):
        # Removing a quant drops its on-hand contribution — snapshot before it
        # is gone.
        self._grove_note_availability(self.product_id.product_tmpl_id)
        return super().unlink()
