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
3. **Recent-orders note.** A one-line **internal note** (`mail.mt_note`) on the
   **lead's chatter** with the partner's three most recent orders. Never in the
   chat channel itself: the visitor is a member of that channel and sees every
   message in it, `mt_note` included, and the email that picked the partner is
   unverified (2026-10-28 leak). The operator uses the native partner form for
   anything deeper (it already lists sale orders) — no new order endpoints.
4. **Discord ping.** A best-effort ping on chat start via
   `DISCORD_SUPPORT_WEBHOOK_URL`, falling back to `DISCORD_OPS_WEBHOOK_URL`
   (mirrors `grove_headless` `_notify_discord`).

## Non-goals (ratified)

No customer-facing order-status lookup in chat, no AI operator, no Chatwoot
bridge, no other brands. Newsletter capture (GOL-245) is untouched. The GOL-1896
publish-webhook prod env gap is tracked separately and not bundled here.

## Trigger seams (Odoo 19 `im_livechat`)

| Job | Seam | Notes |
| --- | --- | --- |
| 4 (Discord) | `discuss.channel.create()` for `channel_type == 'livechat'` | fires on chat start |
| 1–3 | `discuss.channel.write()` of `chatbot_current_step_id` | MAIN seam (GOL-3459): fires when the chatbot advances past the email step, operator online or not (the offline path is the common nursery case) |
| 1–3 | `discuss.channel._forward_human_operator()` | secondary seam at chatbot→operator handoff; usually a no-op because the step write already ran |

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

## Widget patch — one conversation per chat

`static/src/embed/livechat_service_patch.js` (in `im_livechat.assets_embed_core`)
patches Odoo 19's `LivechatService.persist()`. Upstream clears its in-flight guard
as soon as the server channel exists, before the temporary thread is swapped
out, so a second send from the stale composer opened a **second** channel: two
chat windows, the bot asking twice, leads split (seen on prod 2026-10-28/29).
The patch returns the same saved thread for the same temporary thread. Drop it
if a later Odoo 19 build fixes `persist()` upstream.

`static/src/embed/composer_patch.js` (same bundle) empties the text box after
each send. After the swap, typing no longer re-renders the composer, so OWL's
`t-model` never sees the text change and `clear()` left the sent text in the
box; the next message was typed onto the end of it (prod, 2026-10-29). The
patch writes the cleared value into the textarea when the DOM disagrees.

## Company scoping / security

`X-Grove-Tenant` is `grove_headless`'s concern and is never trusted here.
Livechat is not natively company-scoped in Odoo community, so the company is
resolved defensively in `_grove_support_company`: an explicit company on the
livechat channel if the deployment adds one, else the human operator's company,
else the environment company. The partner search is `company_id in [company,
False]` regardless, so a shared (company-less) partner still matches.
