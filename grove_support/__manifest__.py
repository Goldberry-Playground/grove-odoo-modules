{
    "name": "Grove Support Chat",
    "version": "19.0.1.1.2",
    "category": "Website/Live Chat",
    "summary": "Livechat glue: partner match, crm.lead, recent-orders note, Discord ping",
    "description": """
        The `grove_support` glue module (GOL-2022). Source of truth: vault doc
        "Software/Grove Support Chat.md", ratified 2026-09-02.

        Additive only — grove_headless is untouched. When the Grove support
        chatbot hands a conversation to a human operator, this module does FOUR
        jobs and no more:

          1. Partner match — the chatbot-collected email → res.partner,
             company-scoped, with the SAME never-overwrite discipline as
             checkout (grove_headless controllers/main.py ~1571).
          2. Lead creation — one crm.lead per chat, tagged `livechat`, linked to
             the partner, with a transcript deep-link.
          3. Recent-orders note — a one-line internal note on the lead's
             chatter (never in the chat the visitor sees) with the partner's 3
             most recent orders.
          4. Discord ping — a best-effort ops ping on chat start via the existing
             DISCORD_OPS_WEBHOOK_URL.

        Non-goals (ratified): no customer-facing order-status lookup, no AI
        operator, no Chatwoot bridge, no other brands, newsletter capture
        (GOL-245) untouched.
    """,
    "author": "Gathering at the Grove",
    "website": "https://goldberrygrove.farm",
    "license": "LGPL-3",
    # im_livechat: discuss.channel livechat + chatbot answer extraction seam.
    # crm: crm.lead / crm.tag. sale: the recent-orders note reads sale.order.
    "depends": [
        "im_livechat",
        "crm",
        "sale",
    ],
    # We only add fields to / extend behaviour on existing models
    # (discuss.channel, im_livechat.channel), so no new model and no ACLs. The
    # one data file is a form-view inherit exposing the livechat channel's
    # grove_support_company_id (GOL-3459; renamed off company_id in GOL-3482 so
    # it does not flip the loader's serverUrl) so operators can file a channel's
    # chats under the right legal entity.
    "data": [
        "views/im_livechat_channel_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
