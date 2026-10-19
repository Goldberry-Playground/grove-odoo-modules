"""grove_support: map a livechat channel to its owning company (GOL-3459).

``im_livechat.channel`` has no ``company_id`` natively, but Goldberry Grove runs
several legal entities behind one Odoo (company 1 Goldberry Grove farm, company 9
At The Grove Nursery, ...). A nursery chat must file its partner + crm.lead under
the nursery — NOT under whichever company the answering operator's user happens
to belong to. Before this, ``_grove_support_company`` fell back to the operator's
company, so a nursery lead handled by a farm-company operator landed in the farm
(GOL-3459 defect 2: lead 1 / partner 509 created in company 1, not 9).

This adds an explicit, optional company pointer on the livechat channel.
``discuss.channel._grove_support_company`` already prefers it (it guards on
``"company_id" in lc._fields``), so setting it on the nursery channel is what
makes nursery leads land in company 9. Left unset, the operator-company fallback
still applies, so this is additive and safe for channels that don't set it.
"""

from odoo import fields, models


class ImLivechatChannel(models.Model):
    _inherit = "im_livechat.channel"

    company_id = fields.Many2one(
        "res.company",
        string="Company",
        help="Company that owns conversations on this livechat channel. "
        "grove_support files the chat's partner and crm.lead under this company "
        "so separate legal entities are not cross-filed. When unset, "
        "grove_support falls back to the answering operator's company.",
    )
