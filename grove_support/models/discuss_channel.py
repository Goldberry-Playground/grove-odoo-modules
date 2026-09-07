"""grove_support glue for livechat conversations (GOL-2022).

Source of truth: vault doc "Software/Grove Support Chat.md" (ratified 2026-09-02).
This module does FOUR jobs and no more, additively — grove_headless is untouched:

  1. Partner match — the chatbot-collected email -> res.partner, company-scoped,
     with the SAME never-overwrite discipline as checkout (grove_headless
     controllers/main.py ~1571): search email + company_id in [company, False];
     reuse as-is or create; NEVER mutate an existing partner from chat input.
  2. Lead creation — one crm.lead per chat, tagged ``livechat``, linked to the
     partner, with a transcript deep-link in the description.
  3. Recent-orders note — a one-line INTERNAL note (mt_note, never relayed to the
     visitor) into the operator's chat thread listing the partner's 3 most recent
     orders. Everything deeper lives on the native partner form, which already
     lists the customer's sale orders — no new order endpoints.
  4. Discord ping — a best-effort ops ping on chat start via the existing
     ``DISCORD_OPS_WEBHOOK_URL`` (grove_headless ``_notify_discord`` pattern).

Trigger seams (Odoo 19 im_livechat):
  * ``create()`` — chat-start Discord ping for ``channel_type == 'livechat'``.
  * ``_forward_human_operator()`` — fired once when the chatbot hands off to a
    human; by then the email has been collected. Jobs 1-3 run here, guarded
    idempotent by ``grove_support_lead_id`` so a re-forward never dupes a lead.

Security note (from the doc): ``X-Grove-Tenant`` is grove_headless's concern and
is never trusted here; livechat is scoped by company on the Odoo side, resolved
defensively in ``_grove_support_company``.
"""

import logging

from markupsafe import Markup, escape
from odoo import api, fields, models
from odoo.tools import email_normalize

from .discord import format_chat_start, notify_discord

_logger = logging.getLogger(__name__)

# The chatbot step_type whose answer holds the visitor's email. im_livechat's
# _chatbot_find_customer_values_in_messages maps step_type -> our field name.
_EMAIL_STEP_MAP = {"question_email": "email_from"}
_RECENT_ORDERS_LIMIT = 3
_LIVECHAT_TAG = "livechat"


class DiscussChannel(models.Model):
    _inherit = "discuss.channel"

    grove_support_lead_id = fields.Many2one(
        "crm.lead",
        string="Grove Support Lead",
        readonly=True,
        copy=False,
        help="The crm.lead grove_support created for this livechat conversation. "
        "Set once at chatbot->operator handoff; its presence makes lead "
        "creation idempotent so a re-forward never duplicates the lead.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        channels = super().create(vals_list)
        for channel in channels:
            if channel.channel_type == "livechat":
                channel._grove_support_notify_chat_start()
        return channels

    def _forward_human_operator(self, *args, **kwargs):
        """When the chatbot hands off to a human, run the partner/lead/orders
        glue (jobs 1-3). Best-effort — the handoff must never fail because the
        glue did."""
        res = super()._forward_human_operator(*args, **kwargs)
        for channel in self:
            if channel.channel_type != "livechat":
                continue
            try:
                channel._grove_support_process_chat()
            except Exception:  # noqa: BLE001 — glue is best-effort
                _logger.warning(
                    "grove_support: chat processing failed for channel %s",
                    channel.id,
                    exc_info=True,
                )
        return res

    # ── Job 4: Discord chat-start ping ───────────────────────────────────

    def _grove_support_notify_chat_start(self):
        """Best-effort ops ping when a livechat conversation begins."""
        self.ensure_one()
        try:
            company = self._grove_support_company()
            notify_discord(
                format_chat_start(
                    brand=company.name or None,
                    visitor=self._grove_support_visitor_label(),
                )
            )
        except Exception:  # noqa: BLE001 — ping must never break channel creation
            _logger.warning(
                "grove_support: chat-start ping failed for channel %s",
                self.id,
                exc_info=True,
            )

    def _grove_support_visitor_label(self):
        """A human label for the visitor. ``anonymous_name`` is the un-logged-in
        visitor's display when the deployment exposes it; fall back to the
        channel name. Optional-field access is guarded so this never raises."""
        self.ensure_one()
        if "anonymous_name" in self._fields and self.anonymous_name:
            return self.anonymous_name
        return self.name or "A visitor"

    # ── Jobs 1-3: partner match + lead + recent-orders note ──────────────

    def _grove_support_process_chat(self):
        """Match the partner, create the lead, and post the recent-orders note.

        Idempotent: once ``grove_support_lead_id`` is set this is a no-op, so a
        second operator handoff on the same conversation never dupes anything."""
        self.ensure_one()
        if self.grove_support_lead_id:
            return
        email = self._grove_support_collected_email()
        if not email:
            # No chatbot-collected email -> nothing to glue (by design). A plain
            # operator-only chat with no email step lands here and is left alone.
            return
        company = self._grove_support_company()
        partner = self._grove_support_match_partner(email, company)
        lead = self._grove_support_create_lead(partner, email, company)
        self.grove_support_lead_id = lead.id
        self._grove_support_post_recent_orders(partner, company)

    def _grove_support_collected_email(self):
        """The chatbot-collected email (normalized), or ``''`` if none/invalid.

        Reads the same answer im_livechat captured via its own extraction seam,
        then validates with Odoo's ``email_normalize`` exactly as the chatbot's
        ``question_email`` step does — a malformed answer yields ``''``."""
        self.ensure_one()
        values = self._chatbot_find_customer_values_in_messages(_EMAIL_STEP_MAP)
        return email_normalize(values.get("email_from") or "") or ""

    def _grove_support_company(self):
        """Company that scopes the partner match / lead / orders.

        Livechat is not natively company-scoped in Odoo community, so resolve
        defensively (never trusting an edge-supplied tenant header): an explicit
        company on the livechat channel if the deployment adds one, else the
        human operator's company, else the environment company. The partner
        search is ``company_id in [company, False]`` regardless, so a shared
        (company-less) partner still matches."""
        self.ensure_one()
        lc = self.livechat_channel_id
        if lc and "company_id" in lc._fields and lc.company_id:
            return lc.company_id
        operator = self.livechat_operator_id
        if operator:
            op_user = operator.user_ids[:1]
            if op_user and op_user.company_id:
                return op_user.company_id
        return self.env.company

    def _grove_support_match_partner(self, email, company):
        """Match ``email`` -> res.partner within ``company`` (or company-less),
        reusing an existing record AS-IS. NEVER overwrites an existing partner
        from chat input — identical stance to checkout (main.py ~1571) and the
        newsletter funnel: anyone can type a customer's email into chat, so a
        match must never let them mutate that customer's record."""
        Partner = self.env["res.partner"].sudo().with_company(company)
        partner = Partner.search(
            [("email", "=", email), ("company_id", "in", [company.id, False])],
            limit=1,
        )
        if partner:
            return partner
        return Partner.create({"name": email, "email": email, "company_id": company.id})

    def _grove_support_create_lead(self, partner, email, company):
        """One crm.lead for this conversation: partner-linked, tagged
        ``livechat``, with a transcript deep-link in the description."""
        tag = self._grove_support_livechat_tag()
        return (
            self.env["crm.lead"]
            .sudo()
            .with_company(company)
            .create(
                {
                    "name": f"Livechat: {partner.display_name}",
                    "type": "lead",
                    "partner_id": partner.id,
                    "contact_name": partner.name,
                    "email_from": email,
                    "company_id": company.id,
                    "tag_ids": [(4, tag.id)],
                    "description": self._grove_support_transcript_link_html(),
                }
            )
        )

    def _grove_support_livechat_tag(self):
        """Get-or-create the global ``livechat`` crm.tag."""
        Tag = self.env["crm.tag"].sudo()
        tag = Tag.search([("name", "=", _LIVECHAT_TAG)], limit=1)
        if not tag:
            tag = Tag.create({"name": _LIVECHAT_TAG})
        return tag

    def _grove_support_transcript_link_html(self):
        """An HTML deep-link back to this conversation for the lead description.
        The URL is interpolated through Markup so the (record-derived) values are
        escaped rather than trusted as markup."""
        self.ensure_one()
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        url = f"{base}/web#id={self.id}&model=discuss.channel&view_type=form"
        return Markup('<p>Livechat transcript: <a href="%s">%s</a></p>') % (url, url)

    def _grove_support_post_recent_orders(self, partner, company):
        """Post a one-line INTERNAL note (mt_note — never relayed to the visitor)
        into this operator thread with the partner's 3 most recent orders. The
        operator opens the native partner form for anything deeper."""
        self.ensure_one()
        orders = (
            self.env["sale.order"]
            .sudo()
            .search(
                [("partner_id", "=", partner.id), ("company_id", "=", company.id)],
                order="date_order desc, id desc",
                limit=_RECENT_ORDERS_LIMIT,
            )
        )
        if orders:
            parts = []
            for order in orders:
                amount = f"{order.currency_id.symbol or '$'}{order.amount_total:,.2f}"
                when = order.date_order.date().isoformat() if order.date_order else "no date"
                parts.append(f"{order.name} ({amount}, {when})")
            body = Markup("<p><b>Recent orders for %s:</b> %s</p>") % (
                partner.display_name,
                Markup("; ").join(escape(part) for part in parts),
            )
        else:
            body = Markup("<p><b>Recent orders for %s:</b> none on file.</p>") % partner.display_name
        self.message_post(body=body, subtype_xmlid="mail.mt_note")
