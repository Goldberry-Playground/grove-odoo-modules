"""grove.planner.suggestion — "suggest a plant we should carry" (GOL-3549,
planner winter beta spec B.6).

Written by ``POST /grove/api/v1/planner/suggest`` (public). A staff button
"Accept as catalog candidate" creates an unpublished, not-for-sale product
template named from the suggestion, with the layer derived from the stratum,
and posts the note to its chatter — so the enrichment pipeline and the drafter
pick it up like any other new listing.
"""

from odoo import fields, models

from ..services.planner.rules import STRATA, STRATUM_TO_LAYER


class GrovePlannerSuggestion(models.Model):
    _name = "grove.planner.suggestion"
    _description = "Planner plant suggestion (community)"
    _order = "create_date desc, id desc"

    name = fields.Char(string="Plant", required=True)
    stratum = fields.Selection(
        [(s, s.capitalize()) for s in STRATA],
        string="Stratum",
    )
    note = fields.Text(string="Why")
    by = fields.Char(string="By", help="Tester name, or 'public'.", default="public")
    status = fields.Selection(
        [("queued", "Queued"), ("accepted", "Accepted"), ("declined", "Declined")],
        string="Status",
        default="queued",
        required=True,
        index=True,
    )
    product_tmpl_id = fields.Many2one("product.template", string="Candidate", readonly=True, ondelete="set null")
    # Stored solely for the documented per-browser-id rate limit on the public
    # suggest route (spec B.6 "same rate limit as signals"); not shown in a view.
    browser_id = fields.Char(string="Browser Id", index=True)

    def action_accept_as_candidate(self):
        """Create an unpublished catalog candidate from the suggestion (spec B.6).

        ``sale_ok``/``website_published`` false so it is invisible until the
        publish gate clears it; the stratum sets the food-forest layer when that
        value exists in the selection (the ``fungal`` value arrives with section
        A, GOL-3548 — until then a mushroom suggestion is created layer-blank
        and the drafter fills it).
        """
        Product = self.env["product.template"]
        layer_values = dict(Product._fields["grove_layer"].selection)
        for suggestion in self:
            if suggestion.status != "queued":
                continue
            vals = {
                "name": suggestion.name,
                "sale_ok": False,
                "website_published": False,
            }
            layer = STRATUM_TO_LAYER.get(suggestion.stratum)
            if layer and layer in layer_values:
                vals["grove_layer"] = layer
            product = Product.create(vals)
            if suggestion.note:
                product.message_post(body=f"Planner suggestion ({suggestion.by or 'public'}): {suggestion.note}")
            suggestion.write({"status": "accepted", "product_tmpl_id": product.id})
