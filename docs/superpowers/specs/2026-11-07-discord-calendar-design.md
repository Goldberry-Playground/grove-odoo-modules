# Odoo calendar to Discord (design)

**Status:** approved in chat with Josh, 2026-11-06/07. Target: phases A, B and C on Release Train #5 (freeze Fri 2026-11-13, QA Mon 11-16, promote Wed 11-18); phase D on Train #6 (freeze Fri 11-27, promote Wed 12-02). Builds on the Discord webhook pattern in `grove_headless` (`controllers/main.py:_notify_discord`, `models/order_rollup.py:_discord_digest`) and `grove_support/models/discord.py`, and on the topic-channel env vars from gom#384 / odoocker#967. Board: GOL-3828 (A, Josh), GOL-3830 (B, Ada), GOL-3831 (C, Terra), GOL-3832 (D, Ada, blocked), GOL-3829 (E, Josh gate).

## Decision (Josh, 2026-11-06)

Odoo is the source of truth for the farm's public events. Google Calendar is a two-way view of that calendar for the people who live in Gmail. Discord is a publish target: a weekly "Coming up" post first, then native Discord Events (the sidebar kind with "Interested" and reminders) once a bot token exists.

Ruled in chat, do not re-open:

- **Odoo in the middle**, not a Google-to-Discord bot such as Sesh. Public events must exist in Odoo so the storefronts, the weekly digest and later the consult plans can read one list.
- **Google sync = Odoo's own `google_calendar` module** (shipped with Odoo 19 CE, currently uninstalled). No custom Google code. It syncs each paired user's **primary** Google calendar, both ways.
- **Publish rule = `privacy == 'public'`.** Odoo's Google sync maps Google's event visibility to `calendar.event.privacy`, so Josh can mark an event public from either Gmail (Visibility: Public) or Odoo (Privacy: Public). Everything else that syncs in from Gmail stays off Discord. A tag was rejected because it cannot be set from Google.
- **Two tiers on the Discord side.** Tier 1 is a weekly digest through a webhook, no new credentials beyond a channel webhook URL. Tier 2 is native Discord Events, which need a bot token with Manage Events; Discord webhooks cannot create events.
- **Bot token is a new credential** and Josh mints it. It joins the December rotation batch with the other Grove credentials.
- **Times are shown in America/New_York** in every Discord post. Odoo stores UTC; the module converts for display, the same way the weekly rollup does.

## Non-goals

- No event sign-up, RSVP, ticketing or payment. The `event` / `website_event` modules stay uninstalled. GOL-1585 (done, CMO) already ruled site-is-source-of-truth and Odoo Events with RSVP for `/visit` in spring 2027; when that lands, `event.event` can feed the same Discord publisher through the shared public-domain helper.
- No Discord-to-Odoo direction. An event edited in Discord is overwritten by Odoo on the next sync.
- No recurring-event rules pushed to Discord. Odoo expands recurrences into single `calendar.event` rows, and each occurrence in the window becomes its own Discord event. Discord's own `recurrence_rule` is not used.
- No storefront events page. A `/grove/api/v1/events` feed for grove-sites is a natural follow-up and is listed under open questions, not built here.
- No change to the orders, sales, support or shipping Discord channels.
- No sync of attendees. Discord events carry name, time, location and description only.

## Phase A: Google Workspace side (Josh, manual, no code)

1. In the goldberrygrove.farm Google Workspace, create or reuse a Google Cloud project, enable the **Google Calendar API**, set the OAuth consent screen to **Internal**, and create an OAuth client of type **Web application** with the redirect URI `https://odoo.gatheringatthegrove.com/google_account/authentication`.
2. Store the client ID and client secret as concealed fields on the `Grove Prod/odoocker` 1Password item (`GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET`). They are also entered by hand in Odoo (step 4), so no compose passthrough is needed.
3. Install **Google Calendar** (`google_calendar`) on prod through Apps, the way `survey` was installed on 2026-10-26. If odoocker keeps a list of hand-installed core modules, add it there (phase C).
4. Settings, Integrations, Google Calendar: paste the client ID and secret. Open the Calendar app and click **Sync with Google**. Odoo's own cron then keeps Josh's primary calendar and `calendar.event` in step.
5. QA never pairs a Google account. QA runs on seeded `calendar.event` fixtures only, so a QA sync can never write into Josh's real calendar.

Open: whether the three other allowed companies ever need their own paired calendars. Phase A pairs Josh only.

## Phase B: `grove_calendar` module, weekly digest (Ada, Train #5)

A new small module, not more code in `grove_headless`: the calendar has nothing to do with checkout, and `grove_headless` is already at version 1.74. Depends on `calendar` only. It must **not** depend on `google_calendar`, so QA and tests install without Google.

### 1. Configuration

Env vars, read with `os.environ.get` like every other Discord var; nothing is stored in the database.

| Env var | Phase | Notes |
|---|---|---|
| `DISCORD_EVENTS_WEBHOOK_URL` | B | Webhook of the channel that gets the weekly digest. Falls back to `DISCORD_OPS_WEBHOOK_URL` so a missing var surfaces in ops instead of dropping the post silently (same stance as the topic channels). |
| `DISCORD_BOT_TOKEN` | D | Bot token with **Manage Events** on the Grove guild. Absent: the events cron logs once at INFO and does nothing. |
| `DISCORD_GUILD_ID` | D | Snowflake of the Grove server. |
| `GROVE_EVENTS_WINDOW_DAYS` | D | Default `60`. Horizon for native events. Discord caps a guild at 100 scheduled events. |
| `GROVE_EVENTS_LOCATION_DEFAULT` | B, D | Default `Goldberry Grove Farm`. Used when an event has no location. |

### 2. Which events are public

A `calendar.event` is published when all of these hold:

- `active = True`
- `privacy = 'public'`
- `stop` is in the future
- `start` is inside the window (14 days for the digest, `GROVE_EVENTS_WINDOW_DAYS` for native events)

A helper `calendar.event._grove_public_domain(window_days)` returns that domain so the digest, the events cron and tests share one definition.

### 3. Weekly digest

`grove.calendar.digest` (AbstractModel, no table) with `_cron_send_weekly_digest()`, scheduled by `data/calendar_digest_cron.xml` on **Monday 12:00 UTC** (07:00 or 08:00 Eastern). Monday 08:00 UTC is what the order rollup uses, but that lands at 03:00 Eastern and the post would sit unread for hours.

Format, Discord markdown, one message, cut at 2,000 characters:

```
📅 **Coming up at the Grove** (Nov 9 – Nov 22)
- **Fri Nov 13** — Young Farmers meeting
- **Fri Nov 27, 6:30 PM** — Co-op alliance contract · Goldberry Grove Farm
```

Rules:

- All-day events show the date only. Timed events show the start time in `h:mm AM/PM`. Multi-day events show `Sat Nov 14 – Sun Nov 15`.
- Location is appended after ` · ` when set; the description is never posted (it may hold notes meant for staff).
- Sorted by `start`. A week with no public events posts nothing.
- `allowed_mentions: {"parse": []}` on every post so an event named `@everyone` cannot ping the server.
- The formatter lives in `models/discord_format.py` with no `odoo` import, like `grove_headless/models/order_digest.py`, so it is stdlib-testable.

### 4. Fields on `calendar.event`

| Field | Type | Notes |
|---|---|---|
| `discord_event_id` | Char, indexed, readonly | Snowflake of the Discord scheduled event. Empty until phase D creates one. |
| `discord_synced_at` | Datetime, readonly | When the Discord event last matched this row. |
| `discord_url` | Char, computed | `https://discord.com/events/{guild}/{discord_event_id}`; shown on the form as a link when set. |

The form view gets a collapsed "Discord" group with those three fields. Nothing is editable there; the publish switch is the existing Privacy field.

## Phase C: odoocker plumbing (Terra, Train #5)

- Compose passthrough for `DISCORD_EVENTS_WEBHOOK_URL`, `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `GROVE_EVENTS_WINDOW_DAYS`, `GROVE_EVENTS_LOCATION_DEFAULT` on prod and QA, after the `DISCORD_SHIPPING_WEBHOOK_URL` block (odoocker#967 pattern). `.env.op` references point at `Grove Prod/odoocker`.
- QA sets `DISCORD_EVENTS_WEBHOOK_URL` to the QA ops channel and leaves `DISCORD_BOT_TOKEN` unset, so QA never creates Discord events on the real server.
- Add `grove_calendar` to the gom module list that the promote script installs, and `google_calendar` to the hand-installed core module list if one exists.
- Prod `.env` is written by Josh (the auto-mode classifier blocks SSH writes to prod), with the same `apply.sh` pattern as the 2026-10-30 topic-channel wiring.

## Phase D: native Discord Events (Ada, Train #6, blocked on the bot token)

`grove.calendar.discord` (AbstractModel) with `_cron_sync_discord_events()`, scheduled every **15 minutes**. Skips with one INFO log when `DISCORD_BOT_TOKEN` or `DISCORD_GUILD_ID` is unset.

### 1. Discord API

Base `https://discord.com/api/v10`, header `Authorization: Bot <token>`, plain `requests`, timeout 10 s, no new Python dependency.

| Call | Use |
|---|---|
| `GET /guilds/{guild}/scheduled-events` | Sweep: list what the bot owns. |
| `POST /guilds/{guild}/scheduled-events` | Create. `entity_type: 3` (EXTERNAL), `privacy_level: 2` (GUILD_ONLY), `entity_metadata.location`, `scheduled_start_time`, `scheduled_end_time` (required for EXTERNAL), `name` (max 100), `description` (max 1000). |
| `PATCH /guilds/{guild}/scheduled-events/{id}` | Update when any pushed field changed. |
| `DELETE /guilds/{guild}/scheduled-events/{id}` | Remove. |

Rate limits: the cron touches a handful of events per run; on HTTP 429 it stops and retries on the next run. HTTP 404 on PATCH or DELETE clears `discord_event_id` so the next run recreates or forgets the event.

### 2. Field mapping

| Discord | Odoo |
|---|---|
| `name` | `name`, cut at 100 |
| `scheduled_start_time` | `start` as UTC ISO 8601; all-day events start at 09:00 America/New_York |
| `scheduled_end_time` | `stop`; all-day events end at 17:00 America/New_York; an event with `stop <= start` gets `start + 1 hour` |
| `entity_metadata.location` | `location`, else `videocall_location`, else `GROVE_EVENTS_LOCATION_DEFAULT` |
| `description` | `description` with HTML stripped, cut at 1000, plus a last line `odoo:calendar.event:{id}` used by the sweep |

### 3. Sync algorithm (per run)

1. Fetch Odoo rows matching `_grove_public_domain(window_days)`.
2. Fetch the guild's scheduled events and keep those whose `creator_id` is the bot and whose description carries the `odoo:calendar.event:` marker.
3. For each Odoo row: no `discord_event_id` means **create** and store id plus `discord_synced_at`; an id whose `write_date > discord_synced_at` means **PATCH**; otherwise nothing.
4. For each bot-owned Discord event whose Odoo row is missing, archived, no longer public, or outside the window and not yet started: **DELETE**, and clear the Odoo field if the row still exists. Events that already started are left alone; Discord ends them itself.
5. Odoo `unlink` of a published event calls DELETE directly as well, so a deleted meeting does not wait 15 minutes.

Steps 3 and 4 each wrap their request in try/except and log at WARNING; one bad event never stops the run.

### 4. Why not a webhook, restated

A Discord webhook posts messages only. Guild scheduled events need a bot user with Manage Events, so phase D needs the token. Phase B ships without it so the weekly post exists on Train #5 either way.

## Tests

- `discord_format.py`: pure tests for the digest line rules (all-day, timed, multi-day, location, 2,000-char cut, empty week returns None), stdlib only.
- `_grove_public_domain`: public, private, archived, past and out-of-window events, inside Odoo's test runner.
- Digest cron: `requests.post` monkeypatched; asserts URL fallback order and `allowed_mentions`.
- Events cron: a fake Discord client recording calls; cases for create, patch after edit, no-op when unchanged, delete on archive, delete on privacy change, recreate after 404, skip when token unset, stop on 429.
- No test touches `google_calendar`.

## Rollout

| Step | Who | When |
|---|---|---|
| Phase A: Google Cloud OAuth client, `google_calendar` install, pair Josh | Josh | Before Train #5 promote |
| Phase B: `grove_calendar` module + digest | Ada | Train #5 freeze Fri 11-13 |
| Phase C: compose passthrough prod + QA, module lists | Terra | Train #5 |
| Events channel webhook minted and stored in 1Password | Josh | Before Train #5 promote |
| Prod `.env` written | Josh | Promote day Wed 11-18 |
| Phase D: native events | Ada | Train #6 freeze Fri 11-27, after the bot token exists |
| Bot created in the Discord Developer Portal, invited with Manage Events, token stored in 1Password | Josh | Before Train #6 freeze |

First live check after promote: Josh marks the Nov 27 co-op meeting Public in Gmail, confirms it appears in Odoo with Privacy: Public, and the Monday 11-23 digest lists it.

## Open questions for Josh

1. ~~Which Discord channel receives the weekly digest?~~ Answered 2026-11-07: a dedicated events channel (Discord channel id 1568803422314959018). Josh minted its webhook; it is stored as `DISCORD_EVENTS_WEBHOOK_URL` on `Grove Prod/odoocker`. The server's guild id is 1315472509319118938, which is `DISCORD_GUILD_ID` for phase D.
2. Should the digest also go out by email to the merchant inbox, as the order rollup does, or Discord only?
3. Should grove-sites get a public events feed (`/grove/api/v1/events`) from the same domain in a later train?
4. Phase D: should each Discord event's description end with a link back to a page on goldberrygrove.farm, which only makes sense if question 3 is a yes?
