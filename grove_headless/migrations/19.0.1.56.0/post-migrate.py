"""Restructure the public-category tree into departments (GOL-2744).

Backfills ``grove_slug`` to today's slugify(name) on every existing public
category (so no storefront URL changes), converts "Food Forest Packages" into
the Guilds collection, stands up the Orchard / Mycoforestry / Forest farming /
Seed & scion departments (adopting existing categories by name so IDs are kept),
and creates the coming-soon child categories.

Runs on ``-u grove_headless`` after the new fields exist. Idempotent — the same
helper runs from the post_init hook on a fresh install, so QA and prod converge
identically.
"""

from odoo import SUPERUSER_ID, api
from odoo.addons.grove_headless.hooks import restructure_department_tree


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    restructure_department_tree(env)
