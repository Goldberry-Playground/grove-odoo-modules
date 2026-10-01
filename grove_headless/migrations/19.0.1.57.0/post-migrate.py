"""Add the 5-Tree Native Bundle to the Guilds collection (GOL-2882).

tmpl 22 — the "(5-Tree Native Bundle)" memorial SKU ($47, published, sale_ok) —
shipped with ``public_categ_ids=[]``, so it was invisible on /shop/guilds and in
shop category filtering while its five siblings (132/133/134/135/140) were
listed. CEO ruling (GOL-2882): YES, include it alongside them in the Guilds
collection (ex "Food Forest Packages", category 6).

Runs after the 19.0.1.56.0 department restructure (same ``-u grove_headless``
upgrade), so the Guilds category already exists under its slug. Idempotent,
additive, and fully reversible — the same helper is a no-op on re-run.
"""

from odoo import SUPERUSER_ID, api
from odoo.addons.grove_headless.hooks import add_native_bundle_to_guilds


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    add_native_bundle_to_guilds(env)
