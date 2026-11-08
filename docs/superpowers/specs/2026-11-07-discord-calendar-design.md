# Odoo calendar to Discord (design)

**Status:** approved in chat with Josh, 2026-11-06/07; phase D redesigned in a brainstorm with Josh on 2026-11-08 (series-aware, voice rooms). Target: phases A, B and C on Release Train #5 (freeze Fri 2026-11-13, QA Mon 11-16, promote Wed 11-18); phases D and F on Train #6 (freeze Fri 11-27, promote Wed 12-02). Builds on the Discord webhook pattern in `grove_headless` (`controllers/main.py:_notify_discord`, `models/order_rollup.py:_discord_digest`) and `grove_support/models/discord.py`, and on the topic-channel env vars from gom#384 / odoocker#967. Board: GOL-3828 (A, Josh), GOL-3830 (B, Ada), GOL-3831 (C, Terra), GOL-3832 (D, Ada, blocked by B), GOL-3829 (E, Josh gate, done 11-08), GOL-3842 (F, Ada, blocked by B).

## Decision (Josh, 2026-11-06, revised 11-08)

Odoo is the source of truth for the farm team's calendar. Google Calendar is a two-way view of it for the people who live in Gmail. Discord is where the team meets, so the calendar shows up there as native Discord Events (the sidebar kind with a Join button and a start-of-meeting ping), plus a Monday post in the events channel saying what is on this week and where.

Ruled in chat, do not re-open:

- **Odoo in the middle**, not a Google-to-Discord bot such as Sesh. Public events must exist in Odoo so the storefronts, the weekly digest and later the consult plans can read one list.
- **Google sync = Odoo's own `google_calendar` module** (shipped with Odoo 19 CE; installed on prod 2026-11-07 and paired with Josh's account). No custom Google code. It syncs each paired user's **primary** Google calendar, both ways.
- **Publish rule = `privacy == 'public'`.** Odoo's Google sync maps Google's event visibility to `calendar.event.privacy`, so Josh can mark an event public from either Gmail (Visibility: Public) or Odoo (Privacy: Public). Everything else that syncs in from Gmail stays off Discord. A tag was rejected because it cannot be set from Google. Verified end to end 2026-11-08.
- **Three audience tiers, 2026-11-07.** Not public = team, in Odoo and Gmail only; Google's *default* visibility syncs in as an unset `privacy` (observed 2026-11-07: 1,687 of 1,692 synced rows unset, 5 private), so the team tier is "anything but public", never `privacy == 'private'`. Public = the Discord server. Public plus the **Website** tag = the public site feed as well. The website calendar is a tag on the one Odoo calendar, not a dedicated events account; Odoo has one calendar per user, and the tag is the cheapest way to carve out a separate public calendar. Rejected: a Workspace events account with its own Google calendar (a seat, a second OAuth pairing, one more account to mind).
- **Discord Events are the product, 2026-11-08.** The audience is the farm team, not a public community. They open Discord to see when their next meeting is and what is being discussed, and most meetings happen in a Discord voice room. Josh creates those Discord Events by hand today and wants to stop. The sync therefore creates what he creates by hand: one Discord Event per calendar entry.
- **One Discord event per series, never per occurrence, 2026-11-08.** Odoo expands a recurring meeting into up to 720 rows; Discord must not. A recurring Gmail entry becomes one Discord event carrying Discord's own `recurrence_rule`. A one-off becomes a one-off.
- **The room comes from the Location field, 2026-11-08.** A Location equal to the name of a voice channel (`grove-chat`, `General`, `Events`, `Game-time` today) makes a voice-channel event in that room. Any other text makes an in-person event showing that text. Blank means the default room, `grove-chat`. All of it is settable from Gmail.
- **Only Josh's events, 2026-11-08.** A per-user switch on `res.users`, `grove_discord_publisher` (default off), gates everything: an event publishes only when it is Public **and** its organizer (`user_id`) has the switch on. On for Josh only at launch. Other users' events, including CRM meetings and consult bookings that name customers, stay team-only even if someone marks one Public, until Josh turns their switch on.
- **A skipped or moved occurrence does not change the Discord series, 2026-11-08.** Discord gives applications no way to cancel or move one occurrence of a repeating event. When one occurrence of a public series is cancelled or moved in Gmail, the Discord event stays as it is and the sync posts one alert to the events channel telling people. Rejected: temporarily splitting the series into per-occurrence Discord events.
- **Keep the Monday post, 2026-11-08.** Even with the Events tab, a Monday "here is the week" message in the events channel is wanted as the conversation anchor.
- **Bot token is a new credential** and Josh minted it (bot user `GroveCal`, 2026-11-08). It joins the December rotation batch with the other Grove credentials.
- **Times are shown in America/New_York** in every Discord post. Odoo stores UTC; the module converts for display, the same way the weekly rollup does.

## Non-goals

- No event sign-up, RSVP, ticketing or payment. The `event` / `website_event` modules stay uninstalled. GOL-1585 (done, CMO) already ruled site-is-source-of-truth and Odoo Events with RSVP for `/visit` in spring 2027; when that lands, `event.event` can feed the same Discord publisher through the shared public-domain helper.
- No Discord-to-Odoo direction. An event edited in Discord is overwritten by Odoo on the next sync.
- No per-occurrence Discord events for a recurring meeting. See phase D.
- Hand-made Discord events (no Odoo marker in their description) are never modified or deleted by the sync.
- No forum posts or sign-up threads in Discord. Josh's hand-made Farm Meeting carries a link to a sign-up post; if that is wanted, it goes in the Gmail event's description and flows through as text.
- No storefront events page. Phase F ships the public feed; the grove-sites page that renders it stays with the spring 2027 visit-page work (GOL-1585).
- No change to the orders, sales, support or shipping Discord channels.
- No sync of attendees. Discord events carry name, time, room or place, and description only.

## Phase A: Google Workspace side (Josh, manual, no code) — done 2026-11-07

1. In the goldberrygrove.farm Google Workspace, a Google Cloud project with the **Google Calendar API** enabled, an **Internal** OAuth consent screen, and an OAuth client of type **Web application** with the redirect URI `https://odoo.gatheringatthegrove.com/google_account/authentication`.
2. Client ID and secret stored as concealed fields on the `Grove Prod/odoocker` 1Password item (`GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET`). They are entered by hand in Odoo (step 4), so no compose passthrough is needed.
3. **Google Calendar** (`google_calendar`) installed on prod through Apps, the way `survey` was installed on 2026-10-26. Hand-installed core modules are recorded by phase C.
4. Settings, Integrations, Google Calendar: client ID and secret pasted. Calendar app, **Sync with Google**. Odoo's own cron (`Google Calendar: synchronization`, every 12 hours) and every load of the Calendar app keep Josh's primary calendar and `calendar.event` in step. First sync pulled 1,692 rows.
5. QA never pairs a Google account. QA runs on seeded `calendar.event` fixtures only, so a QA sync can never write into Josh's real calendar.

Open: whether the three other allowed companies ever need their own paired calendars. Phase A pairs Josh only.

## Phase B: `grove_calendar` module, weekly digest (Ada, Train #5)

A new small module, not more code in `grove_headless`: the calendar has nothing to do with checkout, and `grove_headless` is already at version 1.74. Depends on `calendar` only. It must **not** depend on `google_calendar`, so QA and tests install without Google.

### 1. Configuration

Env vars, read with `os.environ.get` like every other Discord var; nothing is stored in the database.

| Env var | Phase | Notes |
|---|---|---|
| `DISCORD_EVENTS_WEBHOOK_URL` | B | Webhook of the events channel (channel id 1568803422314959018) that gets the weekly digest. Falls back to `DISCORD_OPS_WEBHOOK_URL` so a missing var surfaces in ops instead of dropping the post silently (same stance as the topic channels). |
| `DISCORD_BOT_TOKEN` | D | Token of bot user `GroveCal`. Its managed role carries View Channels, Connect, Manage Events and Create Events, with per-channel overrides on the private rooms (done 11-08). Absent: the events cron logs once at INFO and does nothing. |
| `DISCORD_GUILD_ID` | D | `1315472509319118938` (Gather at the Grove). |
| `GROVE_EVENTS_WINDOW_DAYS` | D | Default `60`. A single event or a series' next occurrence must start inside it to be pushed. Discord caps a guild at 100 scheduled events. |
| `GROVE_EVENTS_DEFAULT_ROOM` | D | Default `grove-chat`. Voice channel used when a public event has no Location. |

### 2. Which events are public

A `calendar.event` is published when all of these hold:

- `active = True`
- `privacy = 'public'`
- `user_id.grove_discord_publisher = True` (the organizer's switch, section 5)
- `stop` is in the future
- `start` is inside the window (the coming Monday to Sunday for the digest, `GROVE_EVENTS_WINDOW_DAYS` for native events)

A helper `calendar.event._grove_public_domain(window_days)` returns that domain so the digest, the events cron and tests share one definition.

### 3. Weekly digest

`grove.calendar.digest` (AbstractModel, no table) with `_cron_send_weekly_digest()`, scheduled by `data/calendar_digest_cron.xml` on **Monday 12:00 UTC** (07:00 or 08:00 Eastern). Monday 08:00 UTC is what the order rollup uses, but that lands at 03:00 Eastern and the post would sit unread for hours.

Covers the seven days from that Monday through Sunday. Format, Discord markdown, one message, cut at 2,000 characters. The digest lists occurrences, so each weekly meeting appears once:

```
📅 **This week at the Grove** (Nov 9 – Nov 15)
- **Tue Nov 10, 6:00 PM** — Farm Meeting · 🔊 grove-chat
- **Wed Nov 11, 9:00 AM** — Meeting with Wes · 2291 Armstrong Rd, Summersville
- **Fri Nov 13, 6:00 PM** — Future Farmer Grant Review · 🔊 grove-chat
```

Rules:

- All-day events show the date only. Timed events show the start time in `h:mm AM/PM`. Multi-day events show `Sat Nov 14 – Sun Nov 15`.
- The place is appended after ` · `: a voice room as `🔊 <room>` (same matching rule as phase D), otherwise the Location text as typed; nothing when blank. The description is never posted.
- Sorted by `start`. A week with no public events posts nothing.
- `allowed_mentions: {"parse": []}` on every post so an event named `@everyone` cannot ping the server.
- The formatter lives in `models/discord_format.py` with no `odoo` import, like `grove_headless/models/order_digest.py`, so it is stdlib-testable. The room-matching helper lives beside it for the same reason.

### 4. Fields

The same three fields on both `calendar.event` (one-off events and fallback occurrences) and `calendar.recurrence` (series):

| Field | Type | Notes |
|---|---|---|
| `discord_event_id` | Char, indexed, readonly | Snowflake of the Discord scheduled event. Empty until phase D creates one. |
| `discord_payload_hash` | Char, readonly | SHA-256 of the last payload pushed to Discord. Compared instead of `write_date`, because the Google sync rewrites every row on each run (observed 2026-11-08: all 2,348 rows got a new `write_date` with no content change). |
| `discord_url` | Char, computed | `https://discord.com/events/{guild}/{discord_event_id}`; shown on the form as a link when set. |

The event form gets a collapsed "Discord" group showing those fields, or the series' values when the event belongs to a recurrence. Nothing is editable there; the publish switch is the existing Privacy field.

### 5. Who may publish

`res.users.grove_discord_publisher` (Boolean, default False, shown under the user's Preferences for admins only). `_grove_public_domain` includes `("user_id.grove_discord_publisher", "=", True)`, so the digest, the events cron and the phase F feed all respect it. Josh turns it on for his own user after the Train #5 promote (rollout table); nothing is seeded, because user records have no stable xmlid on prod. The Discord group on the event form shows "Organizer does not publish to Discord" when the switch is off, so a Public event that stays off Discord is explained in place.

## Phase C: odoocker plumbing (Terra, Train #5)

- Compose passthrough for `DISCORD_EVENTS_WEBHOOK_URL`, `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `GROVE_EVENTS_WINDOW_DAYS`, `GROVE_EVENTS_DEFAULT_ROOM` on prod and QA, after the `DISCORD_SHIPPING_WEBHOOK_URL` block (odoocker#967 pattern). `.env.op` references point at `Grove Prod/odoocker`, where all three Discord values already sit.
- QA sets `DISCORD_EVENTS_WEBHOOK_URL` to the QA ops channel and leaves `DISCORD_BOT_TOKEN` unset, so QA never creates Discord events on the real server.
- Add `grove_calendar` to the gom module list that the promote script installs, and `google_calendar` to the hand-installed core module list if one exists (else record where it is noted).
- Prod `.env` is written by Josh (the auto-mode classifier blocks SSH writes to prod), with the same `apply.sh` pattern as the 2026-10-30 topic-channel wiring.

## Phase D: native Discord Events, one per series (Ada, Train #6, blocked on phase B)

`grove.calendar.discord` (AbstractModel) with `_cron_sync_discord_events()`, scheduled every **15 minutes**. Skips with one INFO log when `DISCORD_BOT_TOKEN` or `DISCORD_GUILD_ID` is unset.

### 1. Discord API

Base `https://discord.com/api/v10`, header `Authorization: Bot <token>`, plain `requests`, timeout 10 s, no new Python dependency.

| Call | Use |
|---|---|
| `GET /guilds/{guild}/channels` | Once per run: voice (type 2) and stage (type 13) channels, for room matching. |
| `GET /guilds/{guild}/scheduled-events` | Once per run: what exists. The bot sees only events in channels it can view. |
| `POST /guilds/{guild}/scheduled-events` | Create. `privacy_level: 2` (GUILD_ONLY), `name` (max 100), `description` (max 1000), `scheduled_start_time`, and either `entity_type: 2` (VOICE) with `channel_id`, or `entity_type: 3` (EXTERNAL) with `entity_metadata.location` (max 100) and `scheduled_end_time` (required for EXTERNAL; sent for both). Plus `recurrence_rule` for a series. |
| `PATCH /guilds/{guild}/scheduled-events/{id}` | Update when the payload hash changed, including `recurrence_rule`. |
| `DELETE /guilds/{guild}/scheduled-events/{id}` | Remove. |

Rate limits: the cron touches a handful of events per run; on HTTP 429 it stops and retries on the next run. HTTP 404 on PATCH or DELETE clears `discord_event_id` so the next run recreates or forgets the event.

Discord constraints the mapping must respect (Discord API docs, "Guild Scheduled Event Recurrence Rule"): `frequency` is YEARLY 0, MONTHLY 1, WEEKLY 2, DAILY 3. `interval` may be 2 only for WEEKLY, otherwise 1. WEEKLY takes exactly one `by_weekday` (Monday 0 to Sunday 6). DAILY takes a `by_weekday` set, and only certain sets (every day, weekdays, weekends). MONTHLY takes exactly one `by_n_weekday` `{n: 1..5, day}`. YEARLY takes one `by_month` and one `by_month_day`. `recurrence_rule.start` must equal `scheduled_start_time`. Applications cannot set `count` or `end`.

### 2. Unit of sync

| Odoo side | Discord side | Marker (last line of description) |
|---|---|---|
| `calendar.recurrence` with at least one future public occurrence whose `start` is inside the window | one Discord event with `recurrence_rule` | `odoo:calendar.recurrence:{id}` |
| `calendar.event` with no `recurrence_id`, matching `_grove_public_domain(window)` | one Discord event | `odoo:calendar.event:{id}` |
| fallback occurrence (section 3) | one Discord event for that occurrence only | `odoo:calendar.event:{id}` |

For a series, the **representative** row is its next occurrence not yet started: name, start, stop, location and description come from it. Gmail applies visibility, location and description to the whole series, so the occurrences agree; if they ever disagree, the representative wins.

### 3. Recurrence mapping

Odoo's `calendar.recurrence` holds `rrule_type`, `interval`, the seven weekday booleans, `month_by`, `byday`, `day`, `end_type`, `count`, `until`.

| Odoo rule | Discord `recurrence_rule` |
|---|---|
| weekly, interval 1 or 2, exactly one weekday set | `frequency 2, interval, by_weekday [d]` |
| daily, interval 1 | `frequency 3, by_weekday [0..6]` |
| monthly, interval 1, `month_by = 'day'`, `byday` in 1..4 | `frequency 1, by_n_weekday [{n, day}]` |
| yearly, interval 1 | `frequency 0, by_month [m], by_month_day [d]` from the representative |
| anything else: weekly with several weekdays or interval 3+, monthly by date, monthly last-weekday (`byday -1`), any interval above 1 outside weekly, daily on a subset other than weekdays | **fallback**: no rule; one Discord event for the representative occurrence only, re-created for the next one once it ends; one WARNING line to `DISCORD_OPS_WEBHOOK_URL` per series per day: "`<name>` repeats in a way Discord cannot express; posting one occurrence at a time." |

Series end: Discord will not let an application set `count` or `end`, so a repeating Discord event runs until deleted. When an Odoo series has no future occurrence left (count reached, `until` passed, or all remaining occurrences made private), the sweep deletes its Discord event.

Daylight saving: Discord's rule repeats at a fixed UTC time; Google repeats at a fixed local time. Twice a year the UTC start of the next Odoo occurrence shifts by an hour, the payload hash changes, and the run PATCHes `scheduled_start_time` together with `recurrence_rule.start`. Josh's hand-made Farm Meeting shows the failure mode this avoids: anchored 2026-08-04 22:00 UTC in summer, it now sits at 5 PM Eastern while the Gmail entry says 6 PM.

### 4. Room from Location

`discord_format.match_room(location, channels)`: strip, lowercase, drop a leading `#`, compare with each voice or stage channel name lowercased. Channels come from the API each run, not a hardcoded list, so renaming or adding a room needs no deploy.

| Location | Result |
|---|---|
| matches a channel name | `entity_type 2`, `channel_id` of that room |
| any other non-empty text | `entity_type 3`, `entity_metadata.location` = the text, cut at 100 |
| empty | the `GROVE_EVENTS_DEFAULT_ROOM` channel; if no channel has that name, skip the event and log one WARNING per run |

Today's public calendar, as Josh set it on 2026-11-08:

| Gmail entry | Location | Discord result |
|---|---|---|
| Grove Team Meeting, Tuesdays 6 PM | grove-chat | one weekly voice event in grove-chat |
| Eldritch Counsel, Fridays 7 PM, every two weeks | Game-time | one biweekly voice event in Game-time |
| Future Farmer Grant Review, Nov 13 | grove-chat | one voice event |
| Meeting with Wes, Wednesdays 9 AM | 2291 Armstrong Rd, Summersville, WV 26651 | one weekly in-person event |
| Friendsgiving, Nov 28, all day | 2291 Armstrong Rd, Summersville, WV 26651 | one in-person event |

### 5. Field mapping

| Discord | Odoo (representative row) |
|---|---|
| `name` | `name`, cut at 100 |
| `scheduled_start_time` | `start` as UTC ISO 8601; all-day events start at 09:00 America/New_York |
| `scheduled_end_time` | `stop`; all-day events end at 17:00 America/New_York; `stop <= start` gets `start + 1 hour` |
| `channel_id` or `entity_metadata.location` | section 4 |
| `description` | `description` with HTML stripped, cut so that description plus marker fit in 1000, marker as the last line |
| `recurrence_rule` | section 3, absent for one-offs and fallbacks |

### 6. Sync algorithm (per run)

1. Fetch channels and the guild's scheduled events. Keep as **owned** those whose description ends with an `odoo:` marker; everything else is hand-made and untouched.
2. Build the unit list: public series (section 2), public one-offs, and fallback occurrences.
3. For each unit, build the payload and its hash. No `discord_event_id`: **POST**, store id and hash. Id present and hash differs: **PATCH**, store hash. Same hash: nothing.
4. For each owned Discord event whose marker points at a unit not in the list (row gone, archived, no longer public, series ended, or one-off outside the window and not yet started): **DELETE** and clear the fields on the Odoo row if it still exists. A live event (status ACTIVE) is left alone until it ends.
5. `unlink` on `calendar.event` and `calendar.recurrence` calls DELETE directly for rows carrying a `discord_event_id`, so a deleted meeting does not wait 15 minutes.

Steps 3 and 4 wrap each request in try/except and log at WARNING; one bad unit never stops the run.

### 7. Skipped or moved occurrences

Discord's repeating event cannot carry exceptions, and the series stays as one Discord event (ruled 2026-11-08). The sync detects exceptions and tells people instead:

- An **exception** is an expected occurrence of a public series, inside the next `GROVE_EVENTS_WINDOW_DAYS`, that is missing from Odoo (cancelled in Gmail: the row is archived or gone) or whose `start` differs from the rule's date and time (moved in Gmail; Odoo keeps the row linked to the recurrence).
- Expected occurrences come from the Odoo recurrence rule applied to the representative's start time, not from Discord.
- For each new exception the run posts one message to `DISCORD_EVENTS_WEBHOOK_URL`, for example `⚠️ Farm Meeting on Tue Nov 17 is cancelled. The Discord event still shows it.` or `⚠️ Farm Meeting moves from Tue Nov 17, 6:00 PM to Wed Nov 18, 6:00 PM for that week only.` Same `allowed_mentions` stance as the digest.
- Each exception is alerted once. `calendar.recurrence.discord_alerted_exceptions` (Char, JSON list of ISO dates of the original occurrences already announced) is the dedupe; entries older than today are pruned on each run.
- A cancelled occurrence that is later restored, or a moved one moved back, posts a second message saying so, then its date is removed from the list.
- The Monday digest already reflects the truth for that week, because it lists actual occurrence rows.

### 8. Why not a webhook, restated

A Discord webhook posts messages only. Guild scheduled events need a bot user, so phase D needs the token. Phase B ships without it so the weekly post exists on Train #5 either way.

## Phase F: Website tag and public events feed (Ada, Train #6, blocked on phase B)

- Seed a `calendar.event.type` record **Website** (xmlid `grove_calendar.tag_website`, `noupdate="1"`). Set in Odoo only; Google has no tags.
- `calendar.event._grove_website_domain(window_days)` = `_grove_public_domain(window_days)` AND `categ_ids` contains the Website tag.
- Public JSON route `GET /grove/api/v1/events` (`auth="public"`, `csrf=False`, `Cache-Control: public, max-age=300`): events from `_grove_website_domain(90)`, `?days=` capped at 180. Fields: `id`, `name`, `start`, `stop` (UTC ISO 8601), `allday`, `location`, `description_text` (HTML stripped), plus a top-level `tz: "America/New_York"`. Never attendees, organizer, or untagged events. A voice-room Location is still returned as typed; the site decides how to show it.
- Calendar search view gets a "Website" filter so the tagged subset reads as its own calendar in Odoo.

## Tests

- `discord_format.py`: digest line rules (all-day, timed, multi-day, room vs place, 2,000-char cut, empty week returns None) and `match_room` (case, leading `#`, no match, empty), stdlib only.
- `_grove_public_domain`: public, private, unset, archived, past and out-of-window events, inside Odoo's test runner.
- Digest cron: `requests.post` monkeypatched; asserts URL fallback order and `allowed_mentions`.
- Recurrence mapping: one test per row of the section 3 table, plus each fallback trigger, plus the "no future occurrence left" deletion.
- Publisher switch: the same public event with the organizer's switch on and off, for the digest, the events cron and the phase F feed.
- Exceptions: cancelled occurrence alerts once, moved occurrence alerts once with both times, restored occurrence alerts the reversal, pruning of past dates, no alert for a series whose rows all match the rule.
- Events cron with a fake Discord client recording calls: create one-off, create series with rule, patch after a name change, no-op when only `write_date` changed, DST re-anchor patches start and rule start, delete on archive, delete on privacy change, hand-made event without marker untouched, recreate after 404, skip when token unset, skip when default room missing, stop on 429, fallback rolls to the next occurrence after the current one ends.
- Phase F: tagged public, tagged private and untagged public events against `_grove_website_domain`; route shape, cache header, days cap, and no leakage of untagged events.
- No test touches `google_calendar`.

## Rollout

| Step | Who | When |
|---|---|---|
| Phase A: Google Cloud OAuth client, `google_calendar` install, pair Josh | Josh | done 2026-11-07 |
| Events channel webhook, bot `GroveCal` with View Channels + Connect + Manage Events + Create Events, overrides on grove-chat and Game-time, all three values in 1Password | Josh | done 2026-11-08 |
| Phase B: `grove_calendar` module + digest | Ada | Train #5 freeze Fri 11-13 |
| Phase C: compose passthrough prod + QA, module lists | Terra | Train #5 |
| Prod `.env` written | Josh | Promote day Wed 11-18 |
| Turn on "publishes to Discord" on Josh's user | Josh | Promote day Wed 11-18 |
| Phase D: native events, one per series | Ada | Train #6 freeze Fri 11-27 |
| Phase F: Website tag + `/grove/api/v1/events` | Ada | Train #6 |
| Delete the hand-made Farm Meeting event in Discord once the synced Grove Team Meeting appears beside it | Josh | after Train #6 promote |

First live check after Train #5 promote: the Monday 11-23 digest lists the Tuesday Farm Meeting in grove-chat, the Wednesday meeting with Wes at Armstrong Rd, and Friendsgiving. After Train #6 promote: five Discord events exist, each with the room or address above, and the Grove Team Meeting one repeats weekly.

## Open questions for Josh

1. ~~Which Discord channel receives the weekly digest?~~ Answered 2026-11-07: a dedicated events channel (Discord channel id 1568803422314959018). Webhook stored as `DISCORD_EVENTS_WEBHOOK_URL` on `Grove Prod/odoocker`. Guild id 1315472509319118938.
2. ~~Should the digest also go out by email to the merchant inbox?~~ Answered 2026-11-07: **no**, Discord only.
3. ~~Should grove-sites get a public events feed?~~ Answered 2026-11-07: **yes, as a separate public calendar**, done with the Website tag (phase F). The page that shows it waits for the visit-page work.
4. ~~Phase D: should each Discord event's description link back to goldberrygrove.farm?~~ Answered 2026-11-07: **no**.
5. ~~One Discord event per occurrence, or per series?~~ Answered 2026-11-08: **per series**, using Discord's recurrence rule.
6. ~~Is the Monday post still wanted once native events exist?~~ Answered 2026-11-08: **yes**.
7. ~~Whose events can reach Discord?~~ Answered 2026-11-08: **only Josh's for now**, via the per-user switch; he can turn it on for others later. Only what he marks Public goes.
8. ~~Skipped or moved occurrence of a series?~~ Answered 2026-11-08: **keep the Discord series as is and alert people** in the events channel.
9. Digest span: settled by the author as the coming Monday through Sunday (seven days), matching the "This week" heading.
