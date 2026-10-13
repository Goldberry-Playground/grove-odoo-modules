"""Load Sora's approved guild card purpose lines (GOL-3304).

Prepends each approved one-line guild description to the matching guild
template's ``description_ecommerce`` as a new first ``<p>``, matched by product
name (case-insensitive prefix) rather than raw product.template id (the GOL-3292
Part B approach, which was not stable across QA and prod). The storefront Guilds
card purpose line is the first sentence of the PDP description, so this fixes the
guild-card copy (e.g. the factual "redbud for pollination" error on the Heirloom
Fruit Package). Runs on ``-u grove_headless``.

Idempotent: the helper skips a template whose first paragraph already reads the
approved line, so re-running the upgrade never duplicates the paragraph.
"""

from odoo import SUPERUSER_ID, api
from odoo.addons.grove_headless.hooks import load_guild_card_lines


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    load_guild_card_lines(env)
