"""Recompute the stored listing-completeness flag for the format gate (GOL-2906).

grove_listing_complete is a stored compute. GOL-2906 changed what it means (it
now also folds in the spec-text format warnings: unitless dimensions, generic
Wildlife values, double spaces) and added grove_wildlife / grove_bloom_season /
grove_harvest_season to its dependencies, but Odoo does not recompute an
existing stored field on upgrade when only its compute logic changes. Without
this pass the published listings GOL-2896 found with those defects would keep
their stale True flag and the nightly audit would never chase them.

Read-only with respect to listing content: it only refreshes the computed flag,
never rewrites a spec value and never touches website_published. Idempotent.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    templates = env["product.template"].with_context(active_test=False).search([])
    if templates:
        templates.invalidate_recordset(["grove_listing_complete"])
        templates._compute_grove_listing_complete()
        templates.flush_recordset(["grove_listing_complete"])
