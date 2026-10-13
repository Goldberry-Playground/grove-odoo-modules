"""Load approved department teasers + coming lists (GOL-3292).

Fills ``grove_teaser`` / ``grove_coming_list`` on the department tree with
Sora's approved GOL-2746 copy (match by ``grove_slug``, write only when the
field is empty), so the storefront department pages render the real teasers and
"What's coming" lists instead of the fallback blurb. Runs on ``-u grove_headless``
after the GOL-2744 restructure has stood up the tree.

Idempotent: the helper only fills empty fields, so re-running the upgrade never
clobbers an edit made later in Odoo.
"""

from odoo import SUPERUSER_ID, api
from odoo.addons.grove_headless.hooks import load_department_copy


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    load_department_copy(env)
