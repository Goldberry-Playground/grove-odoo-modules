# CRM, social leads and marketplace cross-posting (brainstorm)

**Status:** brainstorm 2026-09-30; decisions ratified by Josh 2026-10-02 (see
"Decisions" below). Findings come from reading this repo, `grove-sites`, the
Odoo 19.0 source on GitHub, and the public platform docs for Meta, Threads
and Etsy. Where a platform doc could not be read directly, that is flagged
as *verify*.

## Decisions (Josh, 2026-10-02)

1. **One lead per person per company.** A later interest from the same person
   must land on that same lead, never a second card. Rules in §3.
2. **Release follow-up email goes through Odoo** (`mass_mailing`), not Ghost.
3. **Social DMs, comments and replies become leads only after Discord
   approval.** Lead-form submissions (if ads are ever run) auto-create.
4. **Live-plant policy is settled**: we grow everything we sell and Facebook
   accepts nursery plants (Josh). Etsy's cultivated-by-seller rule is met.
   Only the state-shipping exclusions need carrying into each channel (§5).
5. **Etsy buy-vs-build:** comparison in §5; recommendation is build, phased.
6. **Meta lead-form ads vs organic:** explained in §4; organic plus our own
   notify-me form first, lead ads only if paid ads start.

## The four asks

1. Wire the existing storefront chat widget into Odoo.
2. A CRM that collects every "interest" from the frontend and creates
   follow-up tasks when a product is released (e.g. mushroom-inoculated trees).
3. Centralize leads from Instagram, Threads and Facebook with follow-ups tied
   to a specific product.
4. Cross-post products for sale to Instagram/Facebook (Shops + Marketplace)
   and Etsy.

---

## 1. Where we already are (code review)

### Chat widget → Odoo is already done, phase 1

The "existing chat widget" **is Odoo's own `im_livechat` widget**, embedded by
`grove-sites/apps/nursery/app/support-chat.tsx` (loader script, then
`assets_embed.js`, gated on `NEXT_PUBLIC_LIVECHAT_CHANNEL_ID`). Nursery only.

- Channel + chatbot are configured by `scripts/setup_livechat_support.py`:
  greet → free-text question → email → thanks → `forward_operator`.
- `grove_support` (`models/discuss_channel.py`) fires at operator handoff:
  partner match by email (never overwrite), one `crm.lead` tagged `livechat`,
  a recent-orders internal note, a Discord ping. Idempotent via
  `grove_support_lead_id`.

What the lead is missing today, and why it matters for asks 2 and 3:

| Gap | Effect |
| --- | --- |
| Lead name is `Livechat: <partner>`; the visitor's question is only reachable via a transcript link | Pipeline is unreadable; no product context |
| No `source_id` / `medium_id` / `team_id` | No attribution, no per-channel reporting |
| No `mail.activity` scheduled | After-hours chats can silently rot |
| No lead at all unless the bot collected an email | Anonymous chats vanish |
| `im_livechat.channel` has no `company_id` (Odoo 19 Community); `_grove_support_company` falls back to the operator's company | Multi-tenant scoping is by convention, not data. The code already checks `"company_id" in lc._fields`, so adding that field is a one-liner seam |
| Odoo's own `crm_livechat` adds `create_lead` steps; we deliberately don't use them | Fine, but we can borrow its lead naming: first free-input message, capped at 100 chars |

### Interest capture exists, but it lives on the partner, not in CRM

`POST /grove/api/v1/newsletter/subscribe` (bearer) upserts `res.partner` and
adds `res.partner.category` tags: `newsletter`, `brand:<x>`, `interest:<x>`,
`source:<x>`, and `Waitlist: <Department>` for the shop "notify me" capture
(GOL-2744). Attribution goes into a chatter note. Ghost is the list of record
for email (GOL-245, double opt-in). Interests in `@grove/newsletter` are
free-form slugs (`produce`, `nursery`, `woodworking`, `wholesale`, ...) and
waitlists are **per department**, not per product.

There is no `crm.lead`, no state ("has this person been told?"), and no link
from an interest to a `product.template`.

### The release trigger already exists

`grove.publish.event` (`_flush_availability_events`) detects, at commit and
coalesced per template, every storefront-visible availability transition:
`qty_available` crossing 0, `sale_ok` flipping, `website_published` flipping.
Today it only revalidates grove-sites. **This is the hook for "we released X":
the same flush can fan out to CRM.**

The "coming soon" placeholder pattern (`scripts/seed_coming_soon_products.py`:
published, `sale_ok=False`, price 0) means a future product like
mushroom-inoculated trees can exist as a record months before stock, so
interests can attach to a real `product.template` and graduation
(`sale_ok=True` + price + quants) is the release event.

### Social is outbound-only, via Buffer and the Discord bridge

`grove-sites/apps/discord-bridge` does caption/hashtag assist → Discord
approval card → Buffer draft (Threads text, Instagram with media). Buffer org
"Goldberry Grove" has these channels connected: Facebook page, Instagram
business, Threads, YouTube, Google Business. Nothing reads inbound comments,
DMs or lead forms. Buffer's API exposes posts/ideas/metrics, not an inbox, so
Buffer cannot be the lead source.

### What Odoo 19 Community gives us, and what it does not

Available (LGPL, in `odoo/odoo` 19.0): `crm`, `crm_livechat`, `website_crm`,
`im_livechat`, `mass_mailing` (+ `utm`, `link_tracker`), `mail` activities.

Not available (Enterprise only): `marketing_automation`, the Social Marketing
app (`social_facebook`, `social_instagram`, ...). No Meta catalog feed and no
Etsy connector ship with Odoo. Third-party paid apps exist on apps.odoo.com
for both (e.g. `ecom_product_feed`, `facebook_shop`, `sale_etsy` 19.0).

---

## 2. Proposal A: chat lead enrichment (`grove_support` v2)

Small, additive, no new module. All of it rides the existing handoff seam.

1. **Name the lead from the question.** First `free_input_multi` answer,
   100 chars, same as `crm_livechat`. Put the full question in `description`
   above the transcript link.
2. **Add an interest step to the bot** (config in `setup_livechat_support.py`):
   a `question_selection` "What are you interested in?" with options that map
   to departments or headline products (Fruit trees, Nut trees, Mushroom logs
   & inoculated trees, Berries, Wholesale, Order help). Read it back with
   `_chatbot_find_customer_values_in_messages` and create an interest record
   (Proposal B) plus a `crm.tag`.
3. **Attribution:** `source_id` = utm.source "Livechat", `medium_id` = "Chat",
   `team_id` = the tenant's "Online" team (already seeded by
   `scripts/seed_sales_teams.py`).
4. **Schedule a `mail.activity`** ("Reply to chat lead", due today, assigned
   to the forwarded operator or the team lead) so nothing depends on someone
   noticing the Discord ping.
5. **Company on the channel:** add `company_id` to `im_livechat.channel` in
   `grove_support`. `_grove_support_company` already prefers it.
6. **Other tenants:** one channel per tenant, and the same `SupportChat`
   component in the goldberry and ggg apps (grove-sites work).
7. **Page context (needs a spike):** the external embed does not tell Odoo
   which product page the visitor was on. Options: pass the product slug as a
   chatbot pre-answer from the Next.js component, or have the bot ask. Decide
   after checking what `/im_livechat/get_session` accepts in 19.0.

---

## 3. Proposal B: interest ledger + release follow-ups (`grove_crm`)

This is the core of ask 2. New module `grove_crm` (depends `crm`,
`grove_headless`), so `grove_headless` stays a catalog/checkout module.

### Data model

`grove.interest` — one row per (person, thing they want):

| Field | Notes |
| --- | --- |
| `partner_id` | company-scoped partner, same never-overwrite discipline |
| `lead_id` | the person's `crm.lead` for this company (see "one lead per person") |
| `company_id` | tenant |
| `product_tmpl_id` | optional; a live product or a coming-soon placeholder |
| `public_categ_id` | optional; department, for the existing `waitlist:<dept>` capture |
| `keyword` | free text when nothing matches yet ("mushroom inoculated trees") |
| `source` | `livechat` / `newsletter` / `notify_me` / `instagram` / `facebook` / `threads` / `lead_ad` / `etsy` / `pos` / `manual` |
| `source_ref` | channel id, IG comment id, leadgen id, ... for dedupe and deep links |
| `state` | `open` → `notified` → `converted` / `closed`; plus `notified_at`, `notified_count`, `last_release_delivery_id` |

**One lead per person per company, many interests (ratified).** A
`crm.lead` is the pipeline card the team works; `grove.interest` lines hang
off it (one2many). The zero-new-model alternative (tags only) cannot answer
"who did we already tell about release #1" and is the reason to build the
model.

**"It goes back to them": the attach rule.** Every capture path, whatever
the source, runs the same resolver (`grove.interest._attach`):

1. Resolve the person: email → company-scoped partner (existing
   never-overwrite search); no email → social handle + platform (stored on
   the lead as `grove_social_handle` / `grove_social_platform`).
2. Find that person's lead for this company, newest first:
   - an **open** lead → attach the interest, post a chatter line "New
     interest: Mushroom-inoculated trees (via Instagram)", and schedule a
     "New interest" activity if none is pending;
   - the newest lead is **lost** (archived) → restore it
     (`crm.lead.toggle_active`) and attach, so the history stays in one
     chatter;
   - the newest lead is **won** → create a fresh lead on the same partner
     (the partner form already lists every opportunity, so the customer
     view is intact);
   - no lead → create one.
3. Duplicate safety net: a daily cron runs Odoo's own
   `_get_lead_duplicates` per partner email and merges with
   `merge_opportunity`, so a lead created by another path (e.g. a human
   quick-create) folds back into the one card.
4. Handle-to-email join: when a handle-only lead later yields an email
   (chat, checkout, lead form), set `partner_id` and let step 3 merge any
   email-keyed lead that already existed.

### Capture points

- Chat (Proposal A step 2).
- `newsletter/subscribe`: in addition to partner tags, create an interest for
  each `interest:` / `waitlist:` entry. Keep the tags; Ghost labels still
  drive email segments.
- New `POST /grove/api/v1/interests` (bearer, tenant-scoped): product-level
  "Notify me when available" on a product page, body `{email, product_id,
  consent, source, attribution}`. Today the storefront only has the
  department-level waitlist.
- Social inbound (Proposal C).
- Operator quick-add on the partner form for farmers-market and phone leads.

### The release trigger

Hook `grove.publish.event._flush_availability_events` (or subscribe to the
same coalesced set) and, for each template that became purchasable
(`sale_ok` → True while published, or `qty_available` 0 → >0), call
`grove.interest._on_release(template, delivery_id)`:

1. Match open interests: direct `product_tmpl_id`, else the template's
   department, else keyword hits on name/tags.
2. Per matched **lead**, schedule one `mail.activity` ("Follow up: Pawpaw
   now available", due +1 day, note lists every matched interest and the
   storefront URL). One activity per lead, not per interest.
3. Mark interests `notified`, bump `notified_count`, store the release
   `delivery_id` so a replayed event is a no-op and a *second* batch later
   can notify again deliberately.
4. Never raise into the availability flush; log and move on, same stance as
   the webhook emit.

**Email blast on release goes through Odoo (ratified).** `grove_crm` depends
on `mass_mailing` (+ `mass_mailing_crm` so `crm.lead` is a mailing model).
On release the hook creates a **draft** `mailing.mailing` per tenant whose
recipient domain is "leads with an open, consented interest matching this
template", subject prefilled, body from a stored mailing template that pulls
the product's storefront URL, image and `description_ecommerce`. Josh edits
the body in Odoo's mailing editor and sends; nothing mails automatically.

Correction (Josh, 2026-10-02): Odoo already sends through **Mailgun SMTP**
in QA and prod (`ir.mail_server` with the `send.gatheringatthegrove.com`
from-filter; see `grove_headless/models/mail_from.py` and GOL-2180). So
there is no "second sender" to stand up: `mass_mailing` uses the same
outgoing server, SPF/DKIM on that domain already exist, and Mailgun keeps
doing delivery, bounce suppression and complaint handling. What Odoo has to
own is only the data (who gets which product notice) and the content (the
editable email). Remaining practical points:

- Use the existing from resolver (`mail_from_vals`) for the mailing's
  `email_from`, so the branded display name per company rides on the
  from-filter-compliant sender exactly as order mail does today.
- `mass_mailing` adds its own unsubscribe link and `mail.blacklist`; that is
  built in, not extra work, and it runs alongside Mailgun's suppression
  list (both apply).
- Optional: set Odoo's bounce alias and inbound fetch so hard bounces mark
  the lead/interest in Odoo as well as in Mailgun. Not needed for v1.
- Consent: only interests captured with `consent=true` (the newsletter
  endpoint already requires it; chat and social captures must ask) are in
  the mailing domain; store `consent_at` on the interest.
- Ghost stays the newsletter list of record; release mailings are product
  notices to people who asked for that product.
- Activities stay for high-intent sources (chat, DM, lead form): one per
  lead per release, as above. Broadcast interests get the mailing only.

---

## 4. Proposal C: social leads (Instagram, Threads, Facebook)

### Platform facts that shape the design

- **Meta Lead Ads** (if we run lead-form ads): `leadgen` webhook delivers a
  `leadgen_id`; a second Graph call with a *Page* token fetches the fields.
  Permissions `leads_retrieval`, `pages_manage_metadata`, `pages_show_list`,
  `pages_read_engagement`, `ads_management`. **Leads expire after 90 days**,
  so run a daily bulk-read backstop alongside the webhook.
- **Instagram DMs and comments**: Instagram Messaging API for business
  accounts, webhooks for `messages` and `comments`, permissions
  `instagram_business_manage_messages` / `_manage_comments`, Meta **app
  review + business verification** required for production. 24-hour reply
  window (7 days with the human-agent tag). The "Connected tools / allow
  access to messages" toggle must be on in the IG app or webhooks silently
  never arrive.
- **Facebook Page**: Messenger Platform (`pages_messaging`) and page feed
  comments; same app, same review.
- **Threads**: API supports publishing, replies, mentions webhooks, keyword
  search. **No DM API.** Threads leads are replies and mentions only; DMs stay
  manual.
- Odoo's Social Marketing app is Enterprise; none of this exists in Community.

### Shape

`grove_social` (or inside `grove_crm`):

1. **One webhook controller** `/grove/api/v1/social/meta/webhook`: GET
   verify-token handshake, POST with `X-Hub-Signature-256` HMAC check. Same
   patterns we already have (`stripe/webhook`, `grove_publish.verify_signature`).
2. **`grove.social.event` ledger**: platform, kind (`lead_ad`, `dm`,
   `comment`, `mention`, `reply`), external id (unique, dedupe like
   `grove.stripe.event`), author handle, text, permalink, raw payload,
   state (`new` / `lead` / `ignored`). Every inbound is stored; only some
   become leads.
3. **Triage, human in the loop.** Lead-form submissions auto-create a lead
   (explicit opt-in, has email). DMs, comments and replies do **not**
   auto-create: they are posted as a Discord card by the existing bridge
   ("Create lead for @handle? interest: Mushroom trees") and only an approver
   turns them into `crm.lead` + `grove.interest` via a bearer endpoint
   `POST /grove/api/v1/leads`. This matches the repo's guardrails (nothing
   automatic, approvers allowlisted) and keeps spam out of the pipeline.
   Keyword hints ("mushroom", "pawpaw", "price", "ship") pre-fill the product.
4. **Identity without email:** store the handle on the lead
   (`grove_social_handle`, platform) and on the interest `source_ref`. Merge
   into the partner when an email shows up (chat, checkout, lead form).
5. **Reply-window activity:** every social lead gets an activity due in 20
   hours with the permalink; operators reply in the native app. Sending DMs
   from Odoo is a later phase and needs more review scope.
6. **Ship the manual path first.** A `/lead` slash command in the Discord
   bridge and an Odoo quick-create (source = Instagram, product) gives
   centralization in days. The webhook automation lands once Meta app review
   clears, which is the long pole (weeks). Start business verification and
   the app review now; dev mode already works for the app's own admins.

### Lead-form ads vs organic (decision 6, explained)

There are three different ways a lead can come out of Instagram/Facebook,
and they need three different plumbing jobs:

| Path | What the customer does | What we get | What it costs us |
| --- | --- | --- | --- |
| **Meta Lead Ads** (paid) | Taps a sponsored post, fills a native form inside IG/FB ("Tell me when pawpaws are back": name, email, phone pre-filled by Meta) without leaving the app | A structured lead with email, delivered by the `leadgen` webhook within seconds; Meta deletes it after 90 days | Ad spend; the leadgen permissions and a Page token; a daily bulk-read backstop |
| **Organic DM / comment / reply** (free) | Comments "price?" on a post, DMs "do you ship to Ohio", replies on Threads | Text + handle, **no email**, inside a 24-hour reply window | Instagram Messaging API + Meta app review + business verification; the Discord approve-to-lead card; handle-based identity until they give an email |
| **Organic post → our own form** (free) | Taps the link in bio or a "notify me" link we reply with, lands on the storefront product page, submits email | A `grove.interest` with email and consent through `POST /grove/api/v1/interests`, UTM-tagged | Nothing new from Meta at all; the form is ours |

The third path is what the nursery already does for the newsletter, just
pointed at a product. It needs no Meta API, no review, and produces a
consented email, which is also what the Odoo release mailing needs.
Recommendation: build path three first (it is part of `grove_crm` anyway),
path two when app review clears (approve via Discord, as ratified), and
path one only if someone actually starts buying lead ads. The `leadgen`
controller is small, so it is cheap to add later; it is pointless to build
before there is spend.

---

## 5. Proposal D: cross-posting products

### Meta (Instagram + Facebook Shops, Marketplace)

Facts: since mid-2025 Shops checkout is **website checkout**, so buyers land
on our storefront. A business Page lists on Marketplace **through its
Commerce Manager shop catalog**; there is no public Marketplace listing API
(the Commerce Platform API is partner-only). The catalog is fed by a
scheduled fetch of a public CSV/TSV/XML URL (hourly to weekly). Required
fields: `id`, `title`, `description`, `availability`, `condition`, `price`,
`link`, `image_link`, `brand`; HTTPS public product URLs; domain verification
in Business Manager.

*Policy (decision 4, settled 2026-10-02).* We grow all our own plants, and
Josh confirms Facebook accepts nursery plant listings. That settles both
platforms' main rule: Meta bans live animals and endangered or protected
species only, and Etsy's 2025 Creativity Standards require plants to be
personally cultivated by the seller, which we satisfy. Remaining
constraints to carry into the data: US-origin only (true), no USDA noxious
or CITES species (none in the catalog), seller-side phytosanitary and state
rules (already modelled in `plant_compliance.py`), and no reselling generic
gardening supplies on Etsy. No validation gate remains before engineering;
the first live feed is the test.

Design: a feed endpoint in `grove_headless`, per tenant, e.g.
`GET /grove/api/v1/feeds/meta.csv?key=<per-tenant token>` (Meta must fetch
it unauthenticated, so the token in the URL is the auth; store it in
`ir.config_parameter`). Rows come from the same domain as the shop grid
(`build_product_domain`: published + `sale_ok`) further gated by
`grove_listing_complete`. `link` = tenant storefront `/shop/<grove_slug>`,
`image_link` = absolute `/web/image/product.template/<id>/image_1024`,
`availability` from free qty (with the preorder-cap logic mapping to
`preorder`), `google_product_category` for plants, `item_group_id` for
variants, shipping from the zone/tier engine or omitted (website checkout
computes it). No writes to Meta, no tokens beyond the feed key: lowest
effort of the four asks and the first to ship.

Once the catalog exists, Instagram product tagging works in the app, and a
later Buffer/bridge enhancement can tag products in posts.

### Etsy

Facts: Open API v3, seller apps are approved quickly, OAuth 2 with
`listings_r` / `listings_w` (+ `transactions_r`, `shops_r`).
`createDraftListing` needs `quantity`, `title`, `description`, `price`,
`who_made`, `when_made`, `taxonomy_id`, plus `shipping_profile_id` and
`return_policy_id` for physical goods; then `uploadListingImage`,
`updateListingInventory` (SKU + quantity per offering), and a state change to
`active`. Etsy has **no webhooks**: order (receipt) import is polling.
Fees: listing fee per listing per 4 months, transaction and payment
processing percentages. Policy: live plants allowed, US-origin only, no
noxious/invasive/CITES species, seller responsible for phytosanitary
compliance (we already have `plant_compliance.py` for state rules).

This one is **bidirectional** or it double-sells: listings and quantity go
out, receipts must come back in as `sale.order` (team "Etsy", paid, existing
fulfillment flow) and tracking must go back out (`createReceiptShipment`).
That is a real connector.

### Buy vs build (decision 5, explained)

**Buy.** Paid connectors on apps.odoo.com for 19.0, roughly $290 to $500
one-time per version: Teqstars "Etsy Connector" (`sale_etsy`, ~$292), Webkul
"Etsy Odoo Bridge" (~$299), TheNapkinCompany (~$400), Ecosire (~$499). The
Teqstars one, per its docs, covers the full loop: imports Etsy master data
(categories, shipping profiles, return policies, readiness states), creates
and updates listings from Odoo products with variants, exports stock and
price on a schedule, imports orders into `sale.order`, exports tracking.

What buying really costs us:

- **Licence and deployment.** These are OPL-1 (proprietary), delivered as
  a zip. They cannot be committed to this repo if it is public and cannot
  be modified-and-redistributed; they have to live in a separate private
  addons path in odoocker and be upgraded by hand per Odoo version.
- **A second order-creation path.** Our `sale.order` pipeline is not
  vanilla: the headless checkout applies the compliance gate
  (`plant_compliance.py`, which states we may ship to), the shared-pool and
  preorder-cap quantity rules, tier shipping lines, order alerts, Discord
  pings, rollups and the fulfillment mirror. A bought connector creates
  orders its own way, so each of those has to be re-verified against
  Etsy-created orders, and a compliance miss means an order we must cancel
  after Etsy already charged the buyer.
- **Quantity truth.** The connector pushes `qty_available`; we need "free
  qty from the shared pool minus a buffer", and we need it pushed on the
  availability event, not on a 15-minute timer. That is a modification we
  are not licensed to make cleanly.
- **Multi-company.** Three tenants in one database; the connectors are
  built for one shop per company at best. Only the nursery sells on Etsy,
  which helps, but every record rule they add has to be audited.
- **Vendor dependency** for every Odoo major upgrade and every Etsy API
  change (Etsy retired shipping templates for processing profiles in 2025,
  for example).

**Build** `grove_etsy` (LGPL, in this repo, tested in CI like everything
else): OAuth 2 PKCE with token refresh stored in `ir.config_parameter`;
`grove.channel.listing` (product, marketplace, external id, state, payload
hash, last sync, last error); a "Publish to Etsy" button that calls
`createDraftListing` → `uploadListingImage` → `updateListingInventory` →
state `active`; a quantity push wired to the same availability flush as the
storefront webhook; a receipts-import cron that creates orders **through the
same service path as the headless checkout** so compliance, alerts and
rollups just work; a tracking push from the existing label flow. Etsy
master data we need is small (one taxonomy id per department, two shipping
profiles for the two tiers, one return policy). Rough size: two to three
weeks of focused work, half of it the receipts-in side.

**Recommendation: build, in two steps.** Step 1 is listings out plus
quantity sync, which is the part no connector does the way we need and the
part that can oversell. During step 1, Etsy orders are keyed into Odoo by
hand (volume will be low while we test the channel). Step 2 is receipts in
and tracking out. If step 1 shows Etsy is not worth the fees, we stop
without having bought anything. Buying only makes sense if we decide Etsy
orders may bypass our order pipeline entirely, and nothing above suggests
we want that.

Build components, concretely:

- `grove.channel.listing` (product, marketplace, external id, state,
  payload hash, last sync), a "Publish to Etsy" button, a qty-sync hook on
  the same availability flush, a receipts-import cron.

Either way the Etsy shipping profile is a lossy mapping of our zone × tier
engine; simplest is one Etsy profile per `grove_shipping_tier` with flat
rates, reviewed against `data/shipping_rates.json` by the daily rate check.

### Shared pieces

- `grove.channel.listing` serves both Meta (feed row provenance) and Etsy
  (API push), and is where "is this product cross-posted, where, when" lives.
- Sales teams "Etsy" and "Meta Shops" in `seed_sales_teams.py` so revenue by
  channel reports cleanly.
- Quantity for every channel derives from the same shared-pool qty logic
  minus a safety buffer; pushed on availability events, never on a timer
  alone.

---

## 6. Suggested sequencing

| Phase | What | Mostly |
| --- | --- | --- |
| 0 (this week) | Add the interest step to the chatbot; seed Etsy/Meta teams; register the Meta app + business verification; register the Etsy app; create the Commerce Manager catalog and verify the storefront domain | config, accounts |
| 1 | `grove_crm`: interest model, product-level notify-me endpoint, release hook creating activities; `grove_support` v2 (lead name, attribution, activity, channel company); chat on goldberry + ggg | Odoo + small grove-sites |
| 2 | Meta catalog feed endpoint, scheduled fetch, IG product tagging | Odoo |
| 3 | Manual `/lead` path via the Discord bridge; then Meta webhook controller + social ledger + triage cards once app review clears; Threads replies/mentions | Odoo + bridge |
| 4 | Etsy: buy-vs-build decision, then listings out, receipts in, tracking out | Odoo |

---

## 7. Still open after 2026-10-02

1. Which tenants get chat in phase 1 (nursery only today).
2. Whether anyone will run Meta lead-form ads (decides if the `leadgen`
   path is built at all; see §4).
3. Whether to wire Odoo's bounce alias so Mailgun hard bounces mark the
   interest in Odoo (optional, not v1).

## 8. Risks

- Meta app review and business verification are the critical path for all
  automated social ingest; nothing in Odoo shortens it.
- 90-day lead expiry on Meta lead forms needs a backstop cron, not just a webhook.
- `im_livechat` in Community is not company-aware; the `company_id` field is our convention and must be set on every channel.
- Anything that flips `sale_ok`/`website_published` now also fans out to CRM; the release hook must be as storm-guarded and non-raising as the webhook emit it piggybacks on.
- Cross-listing quantity: any channel that is not driven off the shared pool will oversell.
