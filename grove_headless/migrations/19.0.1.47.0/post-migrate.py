"""Converge the WV sales-tax binding to 6% state-only on upgrade (GOL-2449).

post_init_hook only runs on a *fresh* install, so an already-installed QA /
production database needs this post-migration to apply the repaired binding:

  * default sale tax → WV **6% state only** (the demo 15% / old 7% group is
    dropped — Josh 2026-09-22 ruling: no municipal on the web path),
  * retrofit of products that carry a cross-company or demo tax, and
  * the GROVE-SHIP shipping SKU forced to the company's WV 6% state tax.

Idempotent — safe to re-run. Prod already had this data corrected by hand on
2026-09-22; this makes QA (and any future rebuild) converge on -u grove_headless
so the fix can never silently regress again.
"""

from odoo import SUPERUSER_ID, api
from odoo.addons.grove_headless.hooks import setup_wv_sales_tax


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    setup_wv_sales_tax(env)
