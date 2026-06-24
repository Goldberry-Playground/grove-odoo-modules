"""Bind the WV sales tax on upgrade to 19.0.1.4.0.

post_init_hook only runs on a *fresh* install, so an already-installed
production database needs this post-migration to apply the same binding
(default sale tax + retrofit of existing products) on ``-u grove_headless``.
Idempotent — safe to re-run.
"""

from odoo import SUPERUSER_ID, api
from odoo.addons.grove_headless.hooks import setup_wv_sales_tax


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    setup_wv_sales_tax(env)
