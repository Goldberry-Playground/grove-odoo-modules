# grove_support — the livechat glue module (GOL-2022)

Source of truth: vault doc **Software/Grove Support Chat.md**, ratified by Josh
2026-09-02. This module is additive — `grove_headless` is untouched.

## The four jobs (no more)

When the Grove support chatbot hands a conversation to a human operator,
`grove_support` does exactly four things:

1. **Partner match.** The chatbot-collected email → `res.partner`,
   company-scoped, with the **same never-overwrite discipline as checkout**
   (`grove_headless/controllers/main.py` ~1571): search `email` +
   `company_id in [company, False]`; reuse as-is or create; **never mutate an
   existing partner from chat input.**
2. **Lead creation.** One `crm.lead` per chat, tagged `livechat`, linked to the
   partner, with a transcript deep-link in the description.
3. **Recent-orders note.** A one-line **internal note** (`mail.mt_note`, never
   relayed to the visitor) into the operator's chat thread with the partner's
   three most recent orders. The operator uses the native partner form for
   anything deeper (it already lists sale orders) — no new order endpoints.
4. **Discord ping.** A best-effort ops ping on chat start via the existing
   `DISCORD_OPS_WEBHOOK_URL` (mirrors `grove_headless` `_notify_discord`).

## Non-goals (ratified)

No customer-facing order-status lookup in chat, no AI operator, no Chatwoot
bridge, no other brands. Newsletter capture (GOL-245) is untouched. The GOL-1896
publish-webhook prod env gap is tracked separately and not bundled here.

## Trigger seams (Odoo 19 `im_livechat`)

| Job | Seam | Notes |
| --- | --- | --- |
| 4 (Discord) | `discuss.channel.create()` for `channel_type == 'livechat'` | fires on chat start |
| 1–3 | `discuss.channel._forward_human_operator()` | fires once at chatbot→operator handoff |

Jobs 1–3 read the collected email through im_livechat's own
`_chatbot_find_customer_values_in_messages({'question_email': 'email_from'})`
and validate it with Odoo's `email_normalize`. Processing is **idempotent**:
`discuss.channel.grove_support_lead_id` is set once, so a re-forward never
duplicates the lead or the note.

## Deployment note — avoid double leads

Odoo's own `crm_livechat` adds `create_lead` / `create_lead_and_forward` chatbot
step types that also create `crm.lead` records. To keep `grove_support` the sole
lead owner, the Grove chatbot script should hand off with a plain
`forward_operator` step, **not** `create_lead_and_forward`. `grove_support`'s
idempotency guard protects against re-processing but does not deduplicate a lead
created by a different mechanism.

## Company scoping / security

`X-Grove-Tenant` is `grove_headless`'s concern and is never trusted here.
Livechat is not natively company-scoped in Odoo community, so the company is
resolved defensively in `_grove_support_company`: an explicit company on the
livechat channel if the deployment adds one, else the human operator's company,
else the environment company. The partner search is `company_id in [company,
False]` regardless, so a shared (company-less) partner still matches.
