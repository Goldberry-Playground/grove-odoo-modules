# Newsletter list of record → Odoo Email Marketing (design)

**Status:** **APPROVED by Josh on this PR, 2026-10-20** — Step 2 (implementation)
is greenlit. This revision folds in Josh's two changes to the proposals below
(single opt-in, one cross-brand checkbox) plus the confirmed CAN-SPAM address,
the Mailgun handoff, and a legacy-import path; see the **Ruled** notes inline.
Original ruling that triggered the work: Josh, 2026-10-19, "option C" — Odoo
Email Marketing (`mass_mailing`) is the list of record for every Grove
newsletter; Ghost is blog-only. This completes the 2026-10-15 capture ruling,
under which consult/wholesale/farm-visit/new-state-waitlist requests became
`crm.lead` records and back-in-stock alerts became Odoo stock notifications
(gom#359, `hotfix/odoo-capture-leads-restock`). That PR's `capture_leads.py`
still says *"only the newsletter stays in Ghost"* — this ruling is exactly what
supersedes that line. Audit evidence:
https://claude.ai/artifact/4J7M3Uk7478jRDmZZCEW4c

Target: **Release Train #4** (freeze Fri 2026-10-30, QA Mon 11-02, promote Wed
11-04; manifest GOL-3303) — Josh confirmed this issue, GOL-3455 and GOL-3456 are
in Train #4. Builds on the newsletter opt-in endpoint
(`/grove/api/v1/newsletter/subscribe`, GOL-221) and the capture-routing hotfix
(gom#359, GOL-3405), which **merged as `c58fa05a`** — Step 2 branches off `main`.

## Why now

No live capture form has ever saved an email. The Ghost hub, nursery and farm
newsletters each have 0 members, and Odoo has 0 `crm.lead` records. We pick the
list of record before there is a list. **Exception (Josh, 2026-10-20):** a
**legacy Ghost droplet** may still hold a handful of farm-blog subscribers;
those are the one thing that migrates — see §9.

## Facts on prod (2026-10-19)

- `mass_mailing` and `website_mass_mailing` are **uninstalled**. `grove_headless`
  is `19.0.1.71.1` on prod.
- `POST /grove/api/v1/newsletter/subscribe` is live (bearer auth, `website=True`,
  tenant-routed) and upserts a `res.partner` tagged `newsletter`, `brand:<x>`,
  `interest:<x>`, `source:<x>`, with UTM attribution written to chatter. It does
  **not** currently create any subscription row. We extend this route; we do not
  add a second one.
- A `waitlist:<dept-slug>` interest already becomes a readable
  `Waitlist: <Department>` contact tag (GOL-2744) on this same route.
- gom#359 (leads + restock, bumped `grove_headless` to `19.0.1.79.0`) **merged
  as `c58fa05a`** (2026-10-20). Step 2's implementation branches off `main` and
  bumps from there.
- Odoo already sends through Mailgun SMTP per company
  (`send.gatheringatthegrove.com` / `mg.<brand>`). **That domain carries
  transactional mail only and must stay transactional** — bulk newsletter sends
  need their own `news.<brand>` server (see §F and the Terra sibling issue).
- The storefront side (opt-in box with the single "other Grove brands"
  checkbox, unsubscribe / preferences pages) is a **separate issue for Iris**,
  blocked on this one.

## Scope of this spec

Step 1 (this PR) is the design only. Step 2 (a follow-up PR, after Josh approves
this spec) is the Odoo implementation: add `mass_mailing` to `grove_headless`
depends, extend the subscribe endpoint (single opt-in + welcome email), add the
unsubscribe + preferences routes, seed the lists, add the one-time legacy import
path, write tests. **No prod data is touched except the Josh-gated one-time
legacy import (§9), which is dry-run-first and out of the module install.**

Each question below carries its original "Proposed answer" plus a **Ruled
(Josh, 2026-10-20)** note recording his decision — approved as proposed, or the
change he made. The ruling notes are authoritative where they differ from the
proposal; Step 2 implements the ruled design.

---

## The eight questions

### 1. Lists: how many `mailing.list`, per-company or global?

**Proposed answer.** Three brand lists, created as records in each brand's
company so Mailgun routing, CAN-SPAM footer, and sender identity resolve per
company the way transactional mail already does:

| List | Company | Audience |
|---|---|---|
| `Nursery News` | At The Grove Nursery, LLC | nursery subscribers |
| `Farm News` | Goldberry Grove Farm | farm/produce subscribers |
| `Grove Digest` | Gathering at the Grove | cross-brand hub digest; **GGG rides the Digest**, no separate GGG list |

Rationale for **per-company, not global**: a `mailing.list` is a container of
`mailing.contact` rows and a send is scoped to a company for the `from`/reply-to,
footer address, and Mailgun server. Three lists in three companies keeps the
unsubscribe/blacklist and CAN-SPAM footer unambiguous per brand. The subscribe
endpoint already resolves the tenant company from website routing
(`request.website.company_id`), so the brand→list mapping is a lookup, not a new
input. Lists are seeded idempotently by XML data with stable external IDs
(`grove_headless.mailing_list_nursery`, `…_farm`, `…_digest`).

> **Ruled (Josh, 2026-10-20).** Approved as proposed — three lists (Nursery
> News, Farm News, Grove Digest); GGG rides the Digest.

### 2. Hub opt-in box: one "also Grove news" checkbox, or per-brand checkboxes?

> **Ruled (Josh, 2026-10-20) — CHANGED from the original proposal.** Every form,
> **the hub included**, gets one **primary** opt-in for the brand whose site the
> form is on, plus **one unticked** secondary checkbox worded **"Also send me
> news from the other Grove brands"**. Ticked, it adds **every** other brand
> list (not just the Digest). No wall of per-brand checkboxes anywhere.

Mechanics. The endpoint accepts an explicit `lists: ["nursery","farm","digest"]`
array (slugs), so the storefront decides what the two controls map to; Odoo
validates the slugs against the seeded lists and ignores unknowns. The
storefront builds the array as: primary brand list always, plus — when the "other
Grove brands" box is ticked — every seeded list the primary is not. If `lists`
is absent, the brand of the calling tenant is the single implied list
(back-compat for the current body). The hub's (GGG) primary list is the Grove
Digest; its "other brands" box adds Nursery News and Farm News.

### 3. Opt-in model: single opt-in + welcome email

> **Ruled (Josh, 2026-10-20) — CHANGED to single opt-in.** No confirmation
> click gate. Add the subscriber to the chosen lists **on submit**; send one
> **welcome email** right away that names the lists and carries one-click
> unsubscribe. Clicking is **not** required to stay subscribed. The 32-byte
> token / 7-day expiry / `/newsletter/confirm` route are **dropped**.

Mechanics:

- On subscribe, upsert the `res.partner` as today, then for each ticked list
  create/find the `mailing.contact` and add an **active** subscription
  (`mailing.contact.subscription`, `opt_out=False`) immediately. No pending
  state, no token model, no confirm route, no GC cron.
- Send **one** welcome email naming every list the person was just added to,
  with RFC 8058 one-click unsubscribe + a link to the per-list preferences page
  (§4). This is a **transactional** 1:1 send (triggered by the subscribe), so it
  goes on the existing transactional Mailgun server, not `news.<brand>` (§6).
  Welcome-email wording and the consent copy are drafted on **GOL-3456** — reuse
  that text.
- **Re-signup is idempotent.** If the (partner, list) subscription already
  exists and is active, the subscribe call is a no-op success — **no duplicate
  contact and no second welcome** within a short de-dupe window (e.g. the
  welcome is suppressed if one was sent to that address within the last 24h). If
  the contact previously **opted out / was blacklisted**, a fresh submit is a
  legitimate new opt-in: clear the opt-out for the ticked list(s) and send the
  welcome again.

Consent (`consent: true`) stays required on the route (§7); single opt-in means
the submit itself is the recorded consent, so we capture consent source + UTM to
chatter at subscribe time rather than at a later confirm.

### 4. Unsubscribe: one-click header + preferences page, blacklist handling

**Proposed answer.**

- **One-click:** every bulk mail carries RFC 8058 `List-Unsubscribe` and
  `List-Unsubscribe-Post: List-Unsubscribe=One-Click`. Odoo's `mass_mailing`
  already injects an unsubscribe URL per recipient; we route it to our handler so
  one click removes the recipient from **that list** (the list the mailing was
  sent to), not all brands.
- **Preferences page:** `GET /grove/api/v1/newsletter/preferences?token=…` shows
  the recipient's per-list opt-in state with checkboxes; `POST` saves it. The
  token is a long-lived per-contact opaque token (`secrets.token_urlsafe`, no
  enumerable id or PII in the URL), reused across every mailing's unsubscribe /
  preferences links for that contact. This is where someone opts out of one
  brand but keeps another. (With single opt-in there is no longer a one-shot
  confirm token — this per-contact token is the only token the design keeps.)
- **Blacklist:** a global "stop all email" uses Odoo's `mail.blacklist` on the
  email address (list-independent). Blacklisted addresses are excluded from every
  send regardless of list membership; re-subscribing requires an explicit new
  opt-in submit (§3), which clears the blacklist for that address. Per-list
  opt-out uses the subscription's `opt_out`, not the global blacklist.

### 5. CAN-SPAM footer: postal address + unsubscribe, per brand

**Proposed answer.** Every mailing template ends with a footer carrying the
sending company's **physical postal address** and the one-click unsubscribe +
preferences links. Footer is part of the seeded mail template, not hand-entered
per send, so no mailing can go out without it.

> **Ruled (Josh, 2026-10-20).** One address for every brand:
> **2291 Armstrong Road, Summersville, WV 26651.** It already matches the
> `res.company` street/city/zip on companies 1, 2 and 3, so the footer
> **renders the address from the company record** (`company_id` of the mailing)
> rather than hard-coding it — one QWeb footer, correct per brand, and it stays
> correct if the registered address ever changes.

### 6. Which Mailgun server do bulk sends use?

**Proposed answer.** A **separate** `news.<brand>` Mailgun sending domain /
`ir.mail_server` per brand, distinct from the transactional
`send.gatheringatthegrove.com` / `mg.<brand>` servers. Bulk marketing must not
share the transactional domain's reputation. The outgoing mail server is selected
with a `from_filter` matching the `news.<brand>` envelope sender so Odoo picks the
bulk server for mailings and the transactional server for order/confirm mail.

This is **infra owned by Terra** (DNS for `news.<brand>`, Mailgun domain
creation, SMTP credentials, the `ir.mail_server` + `from_filter` rows). This spec
only states the dependency; GOL-3455 provisions it. Step 2 implementation is
gated on those servers existing in QA, but the code reads the server by
`from_filter` config, so it does not hard-code credentials.

> **Ruled / handoff (Josh, 2026-10-20).** Approved. Terra provisioned
> `news.<brand>` on all four domains (GOL-3455). Terra's handoff for this item is
> **GOL-3455 §4**: one `ir.mail_server` per company, `from_filter=news.<domain>`,
> From `<name>@news.<domain>`, Reply-To the brand `hello@`. **SMTP creds are on
> Josh.** The welcome email in §3 is **transactional** (triggered, 1:1) and sends
> on the existing transactional server; only true bulk mailings use
> `news.<brand>`.

### 7. Leads and restock contacts are never added to marketing lists unless they opted in

**Proposed answer.** Enforced structurally. The capture-lead route (gom#359) and
the restock/stock-notification route create `crm.lead` / stock notifications and
their `res.partner`, and **never** touch `mailing.list` / `mailing.contact`. The
only path that adds a `mailing.contact` is `newsletter/subscribe` with
`consent: true` (already required today). A lead/restock form that also wants
newsletter opt-in must send an explicit `newsletter: true` (or a `lists` array)
in its payload, which routes through the same consent + double-opt-in flow — it
does not piggyback on the lead. A test asserts that a lead/restock capture
produces zero `mailing.contact` rows.

### 8. Where does the department "coming soon" waitlist go?

**Proposed answer.** It stays a **restock-style request, not a newsletter.** The
`waitlist:<dept-slug>` interest continues to tag the `res.partner` with
`Waitlist: <Department>` (GOL-2744) so Josh can pull the launch list, and it is
**not** added to any `mailing.list`. When the department launches, Josh sends the
one-time "we're open" announcement to that tag as a one-off; it is not a
recurring subscription and carries no standing opt-in. (Distinct from gom#359's
`state_waitlist` **lead**, which is a CRM relationship, not a department
waitlist.) If Josh later wants the launch announcement to flow through
`mass_mailing` for the footer/unsubscribe compliance, we convert the tag to a
one-shot `mailing.list` at launch — out of scope here.

### 9. Legacy Ghost subscribers: one-time import

> **Ruled (Josh, 2026-10-20) — NEW.** Any members found on the **legacy Ghost
> droplet** are migrated into the matching Odoo list. Terra's GOL-3455 check
> finds them; that box is the farm blog, so they go to **Farm News**.

A **one-time import path**, separate from the live subscribe route:

- **Source:** the member export Terra pulls from the legacy droplet (GOL-3455).
- **Destination:** the `Farm News` list (company: Goldberry Grove Farm).
- **Status:** **subscribed only** — import nobody who was unsubscribed/complained
  on the legacy side.
- **Provenance:** tag each imported `res.partner` / `mailing.contact`
  `source:legacy-ghost-migrated` so these are distinguishable from form opt-ins.
- **No welcome email** to imported people — the single-opt-in welcome (§3) fires
  only on a live form submit, never on import. The importer must suppress it.
- **Idempotent + QA-first:** safe to re-run (matches on email, no duplicates);
  dry-run against QA before any prod run. Prod run is a deliberate, Josh-gated
  one-off, not part of the module install.

Shape: a guarded admin action (`ir.actions.server` or a short CLI script reading
a CSV export), not a public route. It is **out of the request path** entirely.

---

## Implementation sketch (Step 2 — after this spec is approved)

**Branches off `main` (gom#359 merged as `c58fa05a`); bumps `grove_headless`
from the current main version. Check the live manifest and take the next free
minor — do not collide with the order-builder / consult-line version line
(GOL-3378 / GOL-3381).**

- `__manifest__.py`: add `mass_mailing` to `depends`; version bump.
- `controllers/main.py`: extend `newsletter_subscribe` to create **active**
  `mailing.contact` + subscription rows (single opt-in, §3) and send one welcome
  email; add `newsletter_unsubscribe` (RFC 8058 one-click) and
  `newsletter_preferences` (GET/POST). **No `newsletter_confirm` route.**
- `models/newsletter_token.py`: the long-lived **per-contact** preferences/
  unsubscribe token (no one-shot confirm token, no GC cron).
- `data/mailing_lists.xml`: seed the three lists (idempotent, stable external
  IDs) and the per-brand footer mail template whose address renders from
  `company_id` (§5). Welcome-email template text per GOL-3456.
- `models/newsletter.py`: pure helpers for list-slug → external-id resolution
  and the "other brands" fan-out (§2), unit-tested standalone (same pattern as
  `newsletter_tag_names`).
- Legacy import (§9): a guarded admin action / CLI reading the GOL-3455 export →
  Farm News, `source:legacy-ghost-migrated`, subscribed-only, welcome suppressed,
  idempotent. Not a route.
- Tests (`tests/test_newsletter_*.py`, native + pytest): single-opt-in happy
  path (contact active + one welcome), re-signup idempotency (no dup, no second
  welcome in-window), "other brands" fan-out, per-list unsubscribe, blacklist
  exclusion + re-opt-in clears it, legacy import (subscribed-only, no welcome),
  and the §7 "lead/restock adds zero mailing.contact" guard.

QA first (install `mass_mailing` in QA, seed lists, drive subscribe→welcome→
unsubscribe against the QA Mailgun `news.<brand>` server Terra provisioned).
Train #4 (GOL-3303), freeze Fri 2026-10-30.

## Non-goals

- No migration except the one-time legacy-Ghost-droplet import (§9); the live
  Ghost instances (hub, nursery, farm) have 0 members.
- No change to Ghost beyond demoting it to blog-only.
- No change to the lead or restock routes' behaviour (§7 is a guard, not a change).
- No new credential in code — the bulk Mailgun server is read from config by
  `from_filter`; Terra provisions it.
- No automated bulk send from this spec; composing/sending mailings stays a human
  action in Odoo Email Marketing.
- No storefront UI — that is Iris's separate issue, blocked on this one.
