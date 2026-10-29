"""grove.planner.signal — anonymous cart/plan signals from the public planner
(GOL-3549, planner winter beta spec B.2).

Written by ``POST /grove/api/v1/planner/signal``. A remove, swap or hand-add of
a plant, a Build-my-order press (``built``) and a plan save (``saved``) each
land one row with a product (except built/saved), the plan's zone band and the
browser id the page keeps in localStorage. No personal data: the browser id is
a random localStorage token, never a customer. The nightly job tallies these
into proposals (spec B.4); the list view groups by product and zone band.
"""

from odoo import fields, models

from ..services.planner.rules import ZONE_BANDS


class GrovePlannerSignal(models.Model):
    _name = "grove.planner.signal"
    _description = "Planner cart/plan signal (anonymous)"
    _order = "create_date desc, id desc"

    kind = fields.Selection(
        [
            ("removed", "Removed"),
            ("swapped", "Swapped"),
            ("added", "Added"),
            ("built", "Built order"),
            ("saved", "Saved plan"),
        ],
        string="Kind",
        required=True,
        index=True,
    )
    product_tmpl_id = fields.Many2one(
        "product.template",
        string="Product",
        ondelete="cascade",
        index=True,
    )
    zone = fields.Integer(string="USDA Zone")
    zone_band = fields.Selection(
        [(b, b) for b in ZONE_BANDS],
        string="Zone Band",
        index=True,
    )
    track = fields.Char(string="Track")
    site = fields.Char(string="Site")
    timing = fields.Char(string="Timing")
    plan_url = fields.Char(string="Plan URL")
    browser_id = fields.Char(string="Browser Id", index=True)
    catalog_version = fields.Integer(string="Catalog Version")
