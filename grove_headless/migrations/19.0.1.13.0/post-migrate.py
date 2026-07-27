"""Backfill the product-photo resolution guardrail fields (GOL-837).

Adds grove_image_width / grove_image_height / grove_image_low_res to
product.template. Odoo initializes new stored computed fields on upgrade, but we
force the recompute here so the low-resolution flag + stored dimensions are
populated for every existing product in one deterministic pass on
``-u grove_headless`` — that is the whole point of the guardrail (surface the
current low-res offenders immediately, not lazily on next image write).

Idempotent — safe to re-run.
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    templates = env["product.template"].search([])
    if templates:
        templates.invalidate_recordset(["grove_image_width", "grove_image_height", "grove_image_low_res"])
        templates._compute_grove_image_resolution()
        templates.flush_recordset(["grove_image_width", "grove_image_height", "grove_image_low_res"])
