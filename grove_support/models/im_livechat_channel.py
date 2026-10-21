"""grove_support: map a livechat channel to its owning company (GOL-3459).

``im_livechat.channel`` has no company pointer natively, but Goldberry Grove runs
several legal entities behind one Odoo (company 1 Goldberry Grove farm, company 9
At The Grove Nursery, ...). A nursery chat must file its partner + crm.lead under
the nursery — NOT under whichever company the answering operator's user happens
to belong to. Before this, ``_grove_support_company`` fell back to the operator's
company, so a nursery lead handled by a farm-company operator landed in the farm
(GOL-3459 defect 2: lead 1 / partner 509 created in company 1, not 9).

This adds an explicit, optional company pointer on the livechat channel.
``discuss.channel._grove_support_company`` prefers it, so setting it on the
nursery channel is what makes nursery leads land in company 9. Left unset, the
operator-company fallback still applies, so this is additive and safe for
channels that don't set it.

The field is deliberately NOT named ``company_id`` (GOL-3482). The ``website``
module's ``get_base_url()`` override resolves a record's base URL to the website
domain of its ``company_id`` whenever a field by that exact name exists on the
model. ``im_livechat.channel.get_livechat_info()`` emits ``get_base_url()`` as
the embed's ``serverUrl``; with a ``company_id`` field set to company 9 the
loader emitted ``https://atthegrovenursery.com`` (the Next.js storefront) instead
of the Odoo host, the cross-origin fetch failed CORS, and the widget never
mounted. Keying the company pointer off a differently named field sidesteps that
override entirely — ``serverUrl`` stays ``web.base.url`` — with no need to
override ``get_base_url`` ourselves and risk other ``company_id``-aware core
behaviour (mail templates, record rules).
"""

from odoo import fields, models


class ImLivechatChannel(models.Model):
    _inherit = "im_livechat.channel"

    grove_support_company_id = fields.Many2one(
        "res.company",
        string="Grove Support Company",
        help="Company that owns conversations on this livechat channel. "
        "grove_support files the chat's partner and crm.lead under this company "
        "so separate legal entities are not cross-filed. When unset, "
        "grove_support falls back to the answering operator's company. Not named "
        "company_id so it does not flip the livechat loader's serverUrl to the "
        "company website domain (GOL-3482).",
    )
