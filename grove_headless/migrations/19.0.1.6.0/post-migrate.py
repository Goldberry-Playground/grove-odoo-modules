"""Stand up the in-person POS channels on upgrade to 19.0.1.6.0.

post_init_hook only runs on a *fresh* install, so an already-installed
production database needs this post-migration to create the Farmer's Market +
Nursery Counter POS configs (payment methods wired to CSH1/CARD/CHCK, sales
teams mapped) on ``-u grove_headless``. Idempotent — safe to re-run.
"""

from odoo import SUPERUSER_ID, api
from odoo.addons.grove_headless.hooks import setup_pos_configs


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    setup_pos_configs(env)
