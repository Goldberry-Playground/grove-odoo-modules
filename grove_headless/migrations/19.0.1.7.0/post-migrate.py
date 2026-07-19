"""Set per-company transactional sender identity on upgrade to 19.0.1.7.0.

post_init_hook only runs on a *fresh* install, so an already-installed
production database needs this post-migration to stamp each company's
``notifications@<domain>`` From address (GOL-465) on ``-u grove_headless``.
Idempotent — safe to re-run.
"""

from odoo import SUPERUSER_ID, api
from odoo.addons.grove_headless.hooks import setup_transactional_senders


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    setup_transactional_senders(env)
