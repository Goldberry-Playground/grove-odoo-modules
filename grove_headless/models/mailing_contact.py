"""Grove extension of mailing.contact (GOL-3454).

Standard Odoo ``mailing.contact`` is a global (company-less) record keyed by
``email_normalized``. We add one field — the datetime the single-opt-in welcome
email was last sent — so a repeat subscribe within a short window does not fire
a second welcome (Josh's 2026-10-20 ruling, §3: single opt-in, idempotent
re-signup, "no second welcome within a short window"). Gating on a stored field
needs no GC cron and survives a worker restart, unlike an in-memory guard.
"""

from odoo import fields, models


class MailingContact(models.Model):
    _inherit = "mailing.contact"

    grove_welcome_date = fields.Datetime(
        string="Grove welcome sent",
        help="When the Grove single-opt-in welcome email was last sent to this "
        "contact. The newsletter subscribe route (GOL-3454) reads this to "
        "suppress a second welcome on a repeat submit within the welcome "
        "window; it is not sent to subscribers.",
        copy=False,
    )
