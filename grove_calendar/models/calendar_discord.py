"""Native Discord scheduled-events sync (GOL-3832, phase D).

``grove.calendar.discord`` is an ``ir.cron`` (AbstractModel, no table) that every
15 minutes reconciles the farm's public calendar with Discord's *guild scheduled
events*: one Discord event per Gmail **series** (using Discord's
``recurrence_rule``, never one per occurrence — ruled 2026-11-08), one per public
one-off, and one per fallback occurrence for a series Discord's rule cannot
express.

Source of truth: docs/superpowers/specs/2026-11-07-discord-calendar-design.md,
phase D (sections D.1–D.8). The recurrence mapping and the digest copy live in the
pure, stdlib-testable ``discord_recurrence.py`` / ``discord_format.py``; this model
is the Odoo seam (query + timezone + network).

Credentials are environment variables set by odoocker (phase C); the cron logs one
INFO line and returns when ``DISCORD_BOT_TOKEN`` or ``DISCORD_GUILD_ID`` is unset,
so QA and the test runner install the module without them.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from odoo import api, fields, models
from odoo.tools import html2plaintext

from . import discord_format as dfmt
from . import discord_recurrence as drec

_logger = logging.getLogger(__name__)

_EASTERN = ZoneInfo("America/New_York")

_API_BASE = "https://discord.com/api/v10"
_TIMEOUT = 10

# Discord entity types.
_ENTITY_VOICE = 2
_ENTITY_EXTERNAL = 3
# Discord voice (2) and stage (13) channel types — the rooms an event can live in.
_ROOM_CHANNEL_TYPES = (2, 13)
# Discord scheduled-event status ACTIVE: a running event is left alone until it ends.
_EVENT_STATUS_ACTIVE = 2

_NAME_LIMIT = 100
_DESC_LIMIT = 1000
_LOCATION_LIMIT = 100

_MARKER_PREFIX = "odoo:"


class DiscordRateLimited(Exception):
    """HTTP 429 — stop this run and retry on the next (spec D.1)."""


class DiscordNotFound(Exception):
    """HTTP 404 on PATCH/DELETE — the event is gone; clear the id and recreate."""


class DiscordClient:
    """Thin ``requests`` wrapper over the Discord guild scheduled-events API.

    One instance per run. Isolated behind ``grove.calendar.discord._discord_client``
    so the cron tests can inject a fake that records calls (spec test list).
    """

    def __init__(self, token, guild, base=_API_BASE):
        self.token = token
        self.guild = guild
        self.base = base
        self._session = requests

    def _headers(self):
        return {"Authorization": f"Bot {self.token}", "Content-Type": "application/json"}

    def _request(self, method, path, payload=None):
        resp = self._session.request(
            method,
            f"{self.base}{path}",
            headers=self._headers(),
            data=json.dumps(payload) if payload is not None else None,
            timeout=_TIMEOUT,
        )
        if resp.status_code == 429:
            raise DiscordRateLimited(path)
        if resp.status_code == 404:
            raise DiscordNotFound(path)
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def list_channels(self):
        return self._request("GET", f"/guilds/{self.guild}/channels") or []

    def list_events(self):
        return self._request("GET", f"/guilds/{self.guild}/scheduled-events") or []

    def create_event(self, payload):
        return self._request("POST", f"/guilds/{self.guild}/scheduled-events", payload)

    def patch_event(self, event_id, payload):
        return self._request("PATCH", f"/guilds/{self.guild}/scheduled-events/{event_id}", payload)

    def delete_event(self, event_id):
        return self._request("DELETE", f"/guilds/{self.guild}/scheduled-events/{event_id}")


class GroveCalendarDiscord(models.AbstractModel):
    """Container for the ir.cron method; no table of its own."""

    _name = "grove.calendar.discord"
    _description = "Native Discord scheduled-events sync"

    # ------------------------------------------------------------------ cron
    @api.model
    def _cron_sync_discord_events(self):
        """ir.cron entry (every 15 min). Reconciles public events with Discord."""
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        guild = os.environ.get("DISCORD_GUILD_ID", "").strip()
        if not token or not guild:
            _logger.info("grove.calendar.discord: DISCORD_BOT_TOKEN/DISCORD_GUILD_ID unset — skipping run")
            return

        client = self._discord_client(token, guild)
        try:
            channels = client.list_channels()
            existing = client.list_events()
        except DiscordRateLimited:
            _logger.warning("grove.calendar.discord: rate limited fetching state — retry next run")
            return
        except Exception:  # noqa: BLE001 — a failed fetch should not raise out of the cron
            _logger.warning("grove.calendar.discord: failed to fetch guild state", exc_info=True)
            return

        rooms = [c for c in channels if c.get("type") in _ROOM_CHANNEL_TYPES]
        units = self._build_units(rooms)

        try:
            self._push_units(client, units)
            self._sweep_orphans(client, existing, units)
            self._detect_exceptions(units)
        except DiscordRateLimited:
            _logger.warning("grove.calendar.discord: rate limited mid-run — retry next run")
            return

    def _discord_client(self, token, guild):
        """Factory, patched by the cron tests to inject a recording fake."""
        return DiscordClient(token, guild)

    @api.model
    def _delete_discord_event(self, event_id):
        """Best-effort DELETE for an ``unlink`` hook (spec D.6 step 5).

        A deleted meeting removes its Discord event at once rather than waiting
        for the next 15-minute run. Swallows everything — a failed delete must not
        block the Odoo ``unlink``; the sweep catches the orphan next run.
        """
        if not event_id:
            return
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        guild = os.environ.get("DISCORD_GUILD_ID", "").strip()
        if not token or not guild:
            return
        try:
            self._discord_client(token, guild).delete_event(event_id)
        except Exception:  # noqa: BLE001 — unlink must not raise on a Discord error
            _logger.warning("grove.calendar.discord: unlink delete of event %s failed", event_id, exc_info=True)

    # ----------------------------------------------------------- unit building
    @api.model
    def _window_days(self):
        try:
            return int(os.environ.get("GROVE_EVENTS_WINDOW_DAYS", "30"))
        except ValueError:
            return 30

    def _build_units(self, rooms):
        """Build the list of sync units: public series, public one-offs, fallbacks.

        A *unit* is a dict::

            {record, marker, payload, hash}

        where ``record`` is the Odoo row carrying the Discord bookkeeping
        (``calendar.recurrence`` for a mapped series, ``calendar.event`` for a
        one-off or a fallback occurrence). ``payload`` is ``None`` when the event
        must be skipped (e.g. no default room); such units are dropped.
        """
        CalendarEvent = self.env["calendar.event"].sudo()
        window = self._window_days()
        public = CalendarEvent.search(CalendarEvent._grove_public_domain(window), order="start asc")

        units = []
        seen_recurrences = self.env["calendar.recurrence"].sudo()
        now = fields.Datetime.now()
        horizon = now + timedelta(days=window)

        for event in public:
            recurrence = event.recurrence_id
            if not recurrence:
                unit = self._event_unit(event, rooms)
                if unit:
                    units.append(unit)
                continue
            if recurrence in seen_recurrences:
                continue
            seen_recurrences |= recurrence
            unit = self._series_unit(recurrence, rooms, now, horizon)
            if unit:
                units.append(unit)
        return units

    def _representative(self, recurrence, now, horizon):
        """The series' next occurrence not yet started and still public/in-window."""
        occurrences = recurrence.calendar_event_ids.filtered(
            lambda e: (
                e.active
                and e.privacy == "public"
                and e.user_id.grove_discord_publisher
                and e.start
                and now < e.start <= horizon
            )
        )
        return occurrences.sorted("start")[:1]

    def _series_unit(self, recurrence, rooms, now, horizon):
        representative = self._representative(recurrence, now, horizon)
        if not representative:
            return None
        rule = drec.build_recurrence_rule(self._recurrence_dict(recurrence), representative.start)
        if rule is None:
            # Discord cannot express this pattern: fall back to one occurrence at a
            # time, keyed on the representative occurrence event, and warn ops once.
            self._warn_fallback(recurrence)
            return self._event_unit(representative, rooms)
        marker = f"{_MARKER_PREFIX}calendar.recurrence:{recurrence.id}"
        payload = self._build_payload(representative, rooms, marker, recurrence_rule=rule)
        if payload is None:
            return None
        return {
            "record": recurrence,
            "marker": marker,
            "payload": payload,
            "hash": self._payload_hash(payload),
            # Carried for exception detection (spec D.7); absent on one-offs and
            # fallbacks, which have no series exceptions.
            "series": {"recurrence": recurrence, "representative": representative},
        }

    def _event_unit(self, event, rooms):
        marker = f"{_MARKER_PREFIX}calendar.event:{event.id}"
        payload = self._build_payload(event, rooms, marker)
        if payload is None:
            return None
        return {
            "record": event,
            "marker": marker,
            "payload": payload,
            "hash": self._payload_hash(payload),
        }

    def _recurrence_dict(self, recurrence):
        """Normalise a ``calendar.recurrence`` to the plain dict the mapper reads."""
        return {
            "rrule_type": recurrence.rrule_type,
            "interval": recurrence.interval,
            "mon": recurrence.mon,
            "tue": recurrence.tue,
            "wed": recurrence.wed,
            "thu": recurrence.thu,
            "fri": recurrence.fri,
            "sat": recurrence.sat,
            "sun": recurrence.sun,
            "month_by": recurrence.month_by,
            "byday": recurrence.byday,
            "weekday": recurrence.weekday,
        }

    # --------------------------------------------------------- payload / hash
    def _event_times(self, event):
        """Return (start_dt, stop_dt) as tz-aware UTC datetimes (spec D.5).

        Timed events carry naive-UTC datetimes; all-day events run 09:00–17:00
        America/New_York. A non-positive duration is widened to one hour.
        """
        if event.allday:
            start_local = datetime.combine(event.start_date, time(9, 0), tzinfo=_EASTERN)
            stop_local = datetime.combine(event.stop_date, time(17, 0), tzinfo=_EASTERN)
            start = start_local.astimezone(timezone.utc)
            stop = stop_local.astimezone(timezone.utc)
        else:
            start = event.start.replace(tzinfo=timezone.utc)
            stop = (event.stop or event.start).replace(tzinfo=timezone.utc)
        if stop <= start:
            stop = start + timedelta(hours=1)
        return start, stop

    def _description_with_marker(self, event, marker):
        """Plain-text description, cut so description + marker fit 1000, marker last."""
        raw = html2plaintext(event.description or "").strip() if event.description else ""
        budget = _DESC_LIMIT - len(marker) - 1  # one newline before the marker
        body = raw[:budget].rstrip()
        return f"{body}\n{marker}" if body else marker

    def _build_payload(self, event, rooms, marker, recurrence_rule=None):
        """Assemble the Discord scheduled-event payload, or None to skip the event."""
        start, stop = self._event_times(event)
        payload = {
            "name": (event.name or "").strip()[:_NAME_LIMIT] or "(untitled)",
            "privacy_level": 2,  # GUILD_ONLY
            "scheduled_start_time": start.isoformat(),
            "scheduled_end_time": stop.isoformat(),
            "description": self._description_with_marker(event, marker),
        }

        location = (event.location or "").strip()
        room = dfmt.match_room(location, rooms) if location else None
        if room is not None:
            payload["entity_type"] = _ENTITY_VOICE
            payload["channel_id"] = str(room.get("id"))
        elif location:
            payload["entity_type"] = _ENTITY_EXTERNAL
            payload["entity_metadata"] = {"location": location[:_LOCATION_LIMIT]}
        else:
            default_name = os.environ.get("GROVE_EVENTS_DEFAULT_ROOM", "grove-chat").strip()
            default_room = dfmt.match_room(default_name, rooms)
            if default_room is None:
                _logger.warning(
                    "grove.calendar.discord: no '%s' channel for blank-Location event %r — skipped",
                    default_name,
                    event.name,
                )
                return None
            payload["entity_type"] = _ENTITY_VOICE
            payload["channel_id"] = str(default_room.get("id"))

        if recurrence_rule is not None:
            rule = dict(recurrence_rule)
            rule["start"] = payload["scheduled_start_time"]  # must equal start
            payload["recurrence_rule"] = rule
        return payload

    def _payload_hash(self, payload):
        """Stable SHA-256 over the semantically significant payload fields.

        Compared instead of ``write_date`` because Google sync rewrites every
        row's ``write_date`` on each pull (spec: hash, not write_date).
        """
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    # ------------------------------------------------------------ push / sweep
    def _push_units(self, client, units):
        """Create, patch, or skip each unit by comparing stored id and hash."""
        for unit in units:
            record = unit["record"]
            try:
                if not record.discord_event_id:
                    created = client.create_event(unit["payload"])
                    record.write(
                        {
                            "discord_event_id": str(created.get("id")),
                            "discord_payload_hash": unit["hash"],
                        }
                    )
                elif record.discord_payload_hash != unit["hash"]:
                    client.patch_event(record.discord_event_id, unit["payload"])
                    record.discord_payload_hash = unit["hash"]
                # same hash: nothing to do.
            except DiscordRateLimited:
                raise
            except DiscordNotFound:
                # The event vanished on Discord: forget it so the next run recreates.
                record.write({"discord_event_id": False, "discord_payload_hash": False})
            except Exception:  # noqa: BLE001 — one bad unit never stops the run
                _logger.warning("grove.calendar.discord: failed to push %s", unit["marker"], exc_info=True)

    def _sweep_orphans(self, client, existing, units):
        """DELETE owned Discord events whose unit is gone, clearing the Odoo row."""
        live_markers = {u["marker"] for u in units}
        for event in existing:
            marker = self._marker_of(event.get("description") or "")
            if not marker:
                continue  # hand-made event, no odoo: marker — never touched.
            if marker in live_markers:
                continue
            if event.get("status") == _EVENT_STATUS_ACTIVE:
                continue  # a running event is left alone until it ends.
            event_id = event.get("id")
            try:
                client.delete_event(event_id)
            except DiscordRateLimited:
                raise
            except DiscordNotFound:
                pass  # already gone.
            except Exception:  # noqa: BLE001
                _logger.warning("grove.calendar.discord: failed to delete orphan %s", marker, exc_info=True)
                continue
            self._clear_marker_row(marker)

    def _marker_of(self, description):
        """Return the ``odoo:...`` marker on the last line of *description*, or ''."""
        line = description.strip().splitlines()[-1].strip() if description.strip() else ""
        return line if line.startswith(_MARKER_PREFIX) else ""

    def _clear_marker_row(self, marker):
        """Clear the Discord bookkeeping on the Odoo row a marker points at."""
        try:
            _, model, rec_id = marker.split(":")
            record = self.env[model].sudo().browse(int(rec_id)).exists()
        except (ValueError, KeyError):
            return
        if record and record.discord_event_id:
            record.write({"discord_event_id": False, "discord_payload_hash": False})

    def _warn_fallback(self, recurrence):
        """One WARNING to the ops webhook per fallback series per day (spec D.3)."""
        today = fields.Date.context_today(self)
        stamp = today.isoformat()
        if recurrence.discord_fallback_warned == stamp:
            return  # already warned today for this series.
        url = os.environ.get("DISCORD_OPS_WEBHOOK_URL", "").strip()
        name = recurrence.name or "A recurring event"
        message = f"⚠️ `{name}` repeats in a way Discord cannot express; posting one occurrence at a time."
        if url:
            try:
                requests.post(
                    url,
                    json={"content": message[:2000], "allowed_mentions": {"parse": []}},
                    timeout=_TIMEOUT,
                )
            except Exception:  # noqa: BLE001 — ops warning is best-effort
                _logger.warning("grove.calendar.discord: ops webhook post failed", exc_info=True)
        recurrence.discord_fallback_warned = stamp

    # ------------------------------------------------------------- exceptions
    _MOVE_PAIR_DAYS = 7  # a missing slot pairs with an off-grid row within a week.

    def _to_local_naive(self, dt, tz):
        """A naive-UTC Odoo datetime → naive local wall-clock in *tz*."""
        return dt.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)

    def _fmt_when(self, local_dt):
        """'Tue Nov 17, 6:00 PM' from a naive local datetime."""
        weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        months = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        hour = local_dt.hour % 12 or 12
        meridiem = "AM" if local_dt.hour < 12 else "PM"
        return (
            f"{weekdays[local_dt.weekday()]} {months[local_dt.month]} "
            f"{local_dt.day}, {hour}:{local_dt.minute:02d} {meridiem}"
        )

    def _post_events_webhook(self, message):
        """Post an exception alert to the events channel (best-effort).

        Same URL fallback and disarmed ``allowed_mentions`` as the weekly digest."""
        url = os.environ.get("DISCORD_EVENTS_WEBHOOK_URL", "") or os.environ.get("DISCORD_OPS_WEBHOOK_URL", "")
        if not url:
            _logger.info("grove.calendar.discord: no events/ops webhook — exception alert dropped")
            return
        try:
            requests.post(
                url,
                json={"content": message[:2000], "allowed_mentions": {"parse": []}},
                timeout=_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 — alert is best-effort
            _logger.warning("grove.calendar.discord: exception alert post failed", exc_info=True)

    def _nearest_extra(self, expected_dt, extras):
        """The off-grid actual datetime closest to *expected_dt* within the pair
        window, or None — used to tell a *moved* occurrence from a *cancelled* one."""
        best, best_gap = None, None
        for dt in extras:
            gap = abs((dt.date() - expected_dt.date()).days)
            if gap <= self._MOVE_PAIR_DAYS and (best_gap is None or gap < best_gap):
                best, best_gap = dt, gap
        return best

    def _detect_exceptions(self, units):
        """Alert the events channel about skipped/moved occurrences (spec D.7).

        Discord's repeating event carries no exceptions, so the series stays one
        Discord event and people are told instead. For each mapped series this
        compares the rule's expected occurrences (local wall-clock, DST-stable)
        with the live Odoo occurrence rows: an expected slot with no row is a
        cancellation; an off-grid row near a missing slot is a move; a previously
        alerted slot that is well again posts a reversal. ``discord_alerted_exceptions``
        dedupes to one alert per original occurrence and is pruned of past dates.
        """
        now = fields.Datetime.now()
        horizon = now + timedelta(days=self._window_days())
        today = fields.Date.context_today(self)

        for unit in units:
            series = unit.get("series")
            if not series:
                continue
            try:
                self._detect_series_exceptions(series, now, horizon, today)
            except Exception:  # noqa: BLE001 — one series never stops the run
                _logger.warning("grove.calendar.discord: exception scan failed for %s", unit["marker"], exc_info=True)

    def _detect_series_exceptions(self, series, now, horizon, today):
        recurrence = series["recurrence"]
        representative = series["representative"]
        tz = ZoneInfo(representative.event_tz) if representative.event_tz else _EASTERN

        rep_local = self._to_local_naive(representative.start, tz)
        horizon_local = self._to_local_naive(horizon, tz)
        expected = drec.expected_occurrences(self._recurrence_dict(recurrence), rep_local, horizon_local)
        if not expected:
            return

        actual = recurrence.calendar_event_ids.filtered(
            lambda e: e.active and e.privacy == "public" and e.start and e.start <= horizon and e.stop > now
        )
        actual_by_date = {}
        for event in actual:
            local = self._to_local_naive(event.start, tz)
            actual_by_date[local.date()] = local

        expected_by_date = {dt.date(): dt for dt in expected}
        extras = [local for d, local in actual_by_date.items() if d not in expected_by_date]

        try:
            alerted = json.loads(recurrence.discord_alerted_exceptions or "[]")
        except ValueError:
            alerted = []
        # Prune dates already past; keep the rest as the live dedupe set.
        active = {d for d in alerted if fields.Date.to_date(d) and fields.Date.to_date(d) >= today}

        name = recurrence.name or "A recurring event"
        for ed, expected_dt in sorted(expected_by_date.items()):
            iso = ed.isoformat()
            actual_dt = actual_by_date.get(ed)
            if actual_dt is not None and actual_dt == expected_dt:
                # On the grid and on time: a prior alert for this slot is now stale.
                if iso in active:
                    self._post_events_webhook(f"✅ {name} on {self._fmt_when(expected_dt)} is back on as scheduled.")
                    active.discard(iso)
                continue
            if actual_dt is not None:
                # Same day, different time — a move within the day.
                if iso not in active:
                    self._post_events_webhook(
                        f"⚠️ {name} moves from {self._fmt_when(expected_dt)} "
                        f"to {self._fmt_when(actual_dt)} for that week only."
                    )
                    active.add(iso)
                continue
            # No row on this expected date: moved to another day, or cancelled.
            moved_to = self._nearest_extra(expected_dt, extras)
            if moved_to is not None:
                extras.remove(moved_to)
                if iso not in active:
                    self._post_events_webhook(
                        f"⚠️ {name} moves from {self._fmt_when(expected_dt)} "
                        f"to {self._fmt_when(moved_to)} for that week only."
                    )
                    active.add(iso)
            elif iso not in active:
                self._post_events_webhook(
                    f"⚠️ {name} on {self._fmt_when(expected_dt)} is cancelled. The Discord event still shows it."
                )
                active.add(iso)

        recurrence.discord_alerted_exceptions = json.dumps(sorted(active))
