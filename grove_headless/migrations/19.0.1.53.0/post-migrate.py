"""Converge duplicate WV sale taxes onto the hierarchy root (GOL-2449 follow-up).

The 19.0.1.47.0 migration bound the WV 6% state tax by calling
``setup_wv_sales_tax``. On production (2026-09-23) that run reported
**"bound for only 1 of 3 companies"**: the nursery branch already owned a
``"WV State Sales Tax 6%"`` seeded on 2026-08-08, the hook created a second one
on the hierarchy root, and Odoo 19 — which scopes ``account.tax`` name
uniqueness to the root and validates ``@api.constrains`` at *flush* rather than
at *create* — then raised "Tax names must be unique!" on every subsequent
flush. ``setup_wv_sales_tax`` swallows that per company, so two companies were
left on the demo 15% default while the upgrade still exited 0.

That leaves the table in a permanently constraint-violating state, so the next
``-u`` fails the same way until the duplicates are collapsed. ``setup_wv_sales_tax``
now converges each hierarchy to exactly ONE root-owned record per WV tax name
(keeping the oldest, which carries the product/accounting history) before it
binds. Re-running it here applies that to an already-installed DB.

Idempotent — safe to re-run, and a no-op on a hierarchy already converged.
"""

from odoo import SUPERUSER_ID, api
from odoo.addons.grove_headless.hooks import setup_wv_sales_tax


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    setup_wv_sales_tax(env)
