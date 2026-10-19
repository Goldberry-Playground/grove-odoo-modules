# Newsletter list of record → Odoo Email Marketing (design)

**Status:** proposed for Josh to rule on this PR. Ruling that triggered it:
Josh, 2026-10-19, "option C" — Odoo Email Marketing (`mass_mailing`) is the
list of record for every Grove newsletter; Ghost is blog-only. This completes
the 2026-10-15 capture ruling, under which consult/wholesale/farm-visit/
new-state-waitlist requests became `crm.lead` records and back-in-stock alerts
became Odoo stock notifications (gom#359, `hotfix/odoo-capture-leads-restock`).
That PR's `capture_leads.py` still says *"only the newsletter stays in Ghost"* —
this ruling is exactly what supersedes that line. Audit evidence:
https://claude.ai/artifact/4J7M3Uk7478jRDmZZCEW4c

Target: Release Train #4 (freeze Fri 2026-10-30, QA Mon 11-02, promote Wed
11-04; manifest GOL-3303) **if** Josh puts it in the manifest — he decides.
Builds on the newsletter opt-in endpoint (`/grove/api/v1/newsletter/subscribe`,
GOL-221) and the capture-routing hotfix (gom#359, GOL-3405).

## Why now

No live capture form has ever saved an email. The Ghost hub, nursery and farm
newsletters each have 0 members, and Odoo has 0 `crm.lead` records. Nothing
needs migrating — we pick the list of record before there is a list.

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
- gom#359 (leads + restock, bumps `grove_headless` to `19.0.1.79.0`) is approved
  but still **open**. It touches the same module and version line, so **it must
  land first**; Josh merges it. This spec's implementation branches off the
  post-#359 tree and bumps from there.
- Odoo already sends through Mailgun SMTP per company
  (`send.gatheringatthegrove.com` / `mg.<brand>`). **That domain carries
  transactional mail only and must stay transactional** — bulk newsletter sends
  need their own `news.<brand>` server (see §F and the Terra sibling issue).
- The storefront side (hub opt-in box, per-brand checkboxes, confirm/unsubscribe
  pages) is a **separate issue for Iris**, blocked on this one.

## Scope of this spec

Step 1 (this PR) is the design only. Step 2 (a follow-up PR, after Josh approves
this spec) is the Odoo implementation: add `mass_mailing` to `grove_headless`
depends, extend the subscribe endpoint, add the confirm and unsubscribe routes,
seed the lists, write tests. **No prod data is touched by either step.**

Everything under "Proposed answer" below is a recommendation for Josh to accept,
amend, or reject in review. "Decision needed" marks the ones most worth his eye.

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

> **Decision needed.** Confirm three lists and that GGG rides the Digest rather
> than getting its own list.

### 2. Hub opt-in box: one "also Grove news" checkbox, or per-brand checkboxes?

**Proposed answer.** One **primary** opt-in for the brand whose site the form is
on, plus a single secondary **"Also send me the Grove Digest"** checkbox on the
nursery and farm forms. The hub (GGG) form offers the Digest as its primary and
optionally per-brand checkboxes for Nursery/Farm news. So at most two list ticks
from any one form, and **one confirmation email covers every list ticked** (§3).
This avoids a wall of brand checkboxes on a storefront footer while still letting
someone opt into the cross-brand digest.

The endpoint accepts an explicit `lists: ["nursery","digest"]` array (slugs), so
the storefront, not Odoo, decides which boxes to show; Odoo validates the slugs
against the seeded lists and ignores unknowns. If `lists` is absent, the brand of
the calling tenant is the single implied list (back-compat for the current body).

> **Decision needed.** One secondary "Grove Digest" checkbox (proposed) vs. full
> per-brand checkboxes on every form.

### 3. Double opt-in: pending until confirmed — token, expiry, re-signup

**Proposed answer.** Subscription is **pending** (`opt_out=False` but not yet
"confirmed") until the confirmation link is clicked. Mechanics:

- On subscribe, upsert the `res.partner` as today, then for each ticked list
  create/find the `mailing.contact` and a subscription row in **pending** state.
  Send **one** confirmation email listing every pending list, with a single
  confirm link.
- **Token:** a random 32-byte URL-safe token (`secrets.token_urlsafe`) stored on
  a small `grove.newsletter.confirmation` model `(token, partner_id, list_ids,
  company_id, created_at, consumed_at)`. The token, not the partner id, is the
  only thing in the URL — no enumerable ids, no PII in the link.
- **Route:** `GET /grove/api/v1/newsletter/confirm?token=…` (`auth="public"`,
  `website=True`, CSRF-exempt GET). On a valid unconsumed unexpired token: mark
  each listed subscription confirmed, set `consumed_at`, redirect to a
  storefront "you're subscribed" page. On expired/consumed/unknown: redirect to
  a neutral "link expired — subscribe again" page (never reveal whether the
  email exists).
- **Expiry:** 7 days. A daily cron deletes consumed and expired rows.
- **Re-signup:** if an unconfirmed subscription already exists for that
  (partner, list), reissue a fresh token and resend the confirmation (invalidate
  the old token); do not duplicate the contact. If the subscription is already
  **confirmed**, the subscribe call is a no-op success (idempotent) — no second
  confirmation email. If the contact is **blacklisted/opted-out**, re-signup is
  treated as a fresh pending opt-in (a person may re-subscribe), issuing a new
  confirmation; confirming it clears the opt-out for that list.

> **Decision needed.** 7-day token expiry; neutral (non-enumerating) expired-link
> page.

### 4. Unsubscribe: one-click header + preferences page, blacklist handling

**Proposed answer.**

- **One-click:** every bulk mail carries RFC 8058 `List-Unsubscribe` and
  `List-Unsubscribe-Post: List-Unsubscribe=One-Click`. Odoo's `mass_mailing`
  already injects an unsubscribe URL per recipient; we route it to our handler so
  one click removes the recipient from **that list** (the list the mailing was
  sent to), not all brands.
- **Preferences page:** `GET /grove/api/v1/newsletter/preferences?token=…` shows
  the recipient's per-list opt-in state with checkboxes; `POST` saves it. Same
  opaque-token model as confirm (a long-lived per-contact token, not the
  one-shot confirm token). This is where someone opts out of one brand but keeps
  another.
- **Blacklist:** a global "stop all email" uses Odoo's `mail.blacklist` on the
  email address (list-independent). Blacklisted addresses are excluded from every
  send regardless of list membership; re-subscribing requires an explicit new
  double opt-in (§3). Per-list opt-out uses the subscription's `opt_out`, not the
  global blacklist.

### 5. CAN-SPAM footer: postal address + unsubscribe, per brand

**Proposed answer.** Every mailing template ends with a footer carrying the
sending company's **physical postal address** and the one-click unsubscribe +
preferences links. The address comes from the company record per brand (seeded;
Josh supplies the mailing address if the company records don't already carry a
CAN-SPAM-valid one). Footer is part of the seeded mail template, not hand-entered
per send, so no mailing can go out without it.

> **Decision needed.** Confirm the postal mailing address to print (one address
> for all three brands, or a distinct address per company). This is a legal
> requirement for every send.

### 6. Which Mailgun server do bulk sends use?

**Proposed answer.** A **separate** `news.<brand>` Mailgun sending domain /
`ir.mail_server` per brand, distinct from the transactional
`send.gatheringatthegrove.com` / `mg.<brand>` servers. Bulk marketing must not
share the transactional domain's reputation. The outgoing mail server is selected
with a `from_filter` matching the `news.<brand>` envelope sender so Odoo picks the
bulk server for mailings and the transactional server for order/confirm mail.

This is **infra owned by Terra** (DNS for `news.<brand>`, Mailgun domain
creation, SMTP credentials, the `ir.mail_server` + `from_filter` rows). This spec
only states the dependency; the Terra sibling issue provisions it. Step 2
implementation is gated on those servers existing in QA, but the code reads the
server by `from_filter` config, so it does not hard-code credentials.

> **Decision needed / handoff.** Confirm `news.<brand>` naming and that Terra
> provisions the bulk Mailgun domains + `ir.mail_server` rows. The confirmation
> email in §3 is **transactional** (triggered, 1:1) and sends on the existing
> transactional server; only true bulk mailings use `news.<brand>`.

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

---

## Implementation sketch (Step 2 — after this spec is approved)

**Branches off the post-gom#359 tree; bumps `grove_headless` from `19.0.1.79.0`.**

- `__manifest__.py`: add `mass_mailing` to `depends`; version bump (→ `…81.0` or
  the next free minor after #357/#358 land — do not collide with the order-builder
  / consult-line version line; see GOL-3378 / GOL-3381).
- `controllers/main.py`: extend `newsletter_subscribe` to create pending
  `mailing.contact` + subscription rows and send one confirmation; add
  `newsletter_confirm`, `newsletter_unsubscribe` (one-click), `newsletter_preferences`.
- `models/newsletter_confirmation.py`: the token model + daily GC cron.
- `data/mailing_lists.xml`: seed the three lists (idempotent, stable external IDs)
  and the per-brand footer mail template.
- `models/newsletter.py`: pure helpers get list-slug → external-id resolution and
  token helpers, unit-tested standalone (same pattern as `newsletter_tag_names`).
- Tests (`tests/test_newsletter_*.py`, native + pytest): double opt-in happy path,
  token expiry/consume/reissue, re-signup idempotency, per-list unsubscribe,
  blacklist exclusion, and the §7 "lead/restock adds zero mailing.contact" guard.

QA first (install `mass_mailing` in QA, seed lists, drive subscribe→confirm→
unsubscribe against the QA Mailgun `news.<brand>` server once Terra provisions it).
Candidate for Train #4; Josh decides the manifest.

## Non-goals

- No migration — there are no existing members anywhere.
- No change to Ghost beyond demoting it to blog-only (no newsletter members there).
- No change to the lead or restock routes' behaviour (§7 is a guard, not a change).
- No new credential in code — the bulk Mailgun server is read from config by
  `from_filter`; Terra provisions it.
- No automated bulk send from this spec; composing/sending mailings stays a human
  action in Odoo Email Marketing.
- No storefront UI — that is Iris's separate issue, blocked on this one.
