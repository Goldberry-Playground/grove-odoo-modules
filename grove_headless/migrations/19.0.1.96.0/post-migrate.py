"""Recompute the stored planner stratum for the four-layer model (Josh 2026-11-06).

grove_planner_stratum is a stored compute off grove_layer. Its selection moved
from three values (overstory / understory / mushroom) to four (overstory /
midstory / shrub / ground) and the layer mapping changed with it. Odoo does not
recompute a stored field on upgrade when only its compute logic changes, and
it nulls rows holding a removed selection value, so without this pass every
existing plant would serve a blank stratum to the planner feed until its layer
was next edited.

Read-only with respect to facts: it only refreshes the derived stratum from the
layer each template already has. Idempotent.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    templates = env["product.template"].with_context(active_test=False).search([])
    if templates:
        templates.invalidate_recordset(["grove_planner_stratum"])
        templates._compute_grove_planner_stratum()
        templates.flush_recordset(["grove_planner_stratum"])
