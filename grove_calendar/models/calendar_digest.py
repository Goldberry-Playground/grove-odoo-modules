"""Weekly 'Coming up' Discord digest cron (GOL-3830, phase B).

Scheduled via ``data/calendar_digest_cron.xml`` (ir.cron, Monday 12:00 UTC).
Queries ``calendar.event`` for the public events in the next two weeks and posts
one Discord message. The copy/formatting lives in ``discord_format.py``
(pure/stdlib-testable); this model is the Odoo seam (query + timezone + network).

Mirrors the Discord-webhook contract used across the stack
(``grove_headless`` order rollup, ``grove_support`` chat ping): best-effort POST,
``DISCORD_EVENTS_WEBHOOK_URL`` with a fallback to ``DISCORD_OPS_WEBHOOK_URL`` so a
missing var surfaces in ops instead of dropping the post silently, and
``allowed_mentions: {"parse": []}`` so an event named ``@everyone`` can never ping
the server.
"""

import logging
import os
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from odoo import api, fields, models

from . import discord_format as df

_logger = logging.getLogger(__name__)

# Two-week horizon for the weekly digest (phase D's native-events sync uses the
# wider GROVE_EVENTS_WINDOW_DAYS instead).
_DIGEST_WINDOW_DAYS = 14

_EASTERN = ZoneInfo("America/New_York")


class GroveCalendarDigest(models.AbstractModel):
    """Container for the ir.cron method; no table of its own."""

    _name = "grove.calendar.digest"
    _description = "Weekly 'Coming up' calendar digest"

    @api.model
    def _cron_send_weekly_digest(self):
        """Called weekly by ir.cron. Builds the 'Coming up' message from the
        public events in the window and posts it; an empty week posts nothing."""
        CalendarEvent = self.env["calendar.event"].sudo()
        events = CalendarEvent.search(
            CalendarEvent._grove_public_domain(_DIGEST_WINDOW_DAYS),
            order="start asc",
        )

        now = fields.Datetime.now()
        window_start = now.replace(tzinfo=timezone.utc).astimezone(_EASTERN).date()
        window_end = (
            (now + timedelta(days=_DIGEST_WINDOW_DAYS)).replace(tzinfo=timezone.utc).astimezone(_EASTERN).date()
        )

        message = df.build_digest_message(
            [self._event_payload(e) for e in events],
            window_start,
            window_end,
        )
        if not message:
            _logger.info("grove.calendar.digest: no public events in the window — nothing posted")
            return

        self._post_discord(message)
        _logger.info("grove.calendar.digest: posted 'Coming up' digest — %d event(s)", len(events))

    def _event_payload(self, event):
        """Normalise a ``calendar.event`` to the plain dict ``discord_format``
        expects. All-day events carry their inclusive start/stop *dates*; timed
        events carry naive-UTC datetimes that the formatter renders in Eastern."""
        if event.allday:
            return {
                "name": event.name or "",
                "allday": True,
                "location": event.location or "",
                "start": event.start_date,
                "stop": event.stop_date,
            }
        return {
            "name": event.name or "",
            "allday": False,
            "location": event.location or "",
            "start": event.start,
            "stop": event.stop,
        }

    def _post_discord(self, message):
        """POST the digest to the events channel (best-effort).

        ``DISCORD_EVENTS_WEBHOOK_URL`` is the weekly-digest channel; it falls
        back to ``DISCORD_OPS_WEBHOOK_URL`` so an unset events webhook surfaces
        visibly in ops rather than dropping the post. A missing URL or a failed
        request is logged and swallowed — the digest never raises."""
        url = os.environ.get("DISCORD_EVENTS_WEBHOOK_URL", "") or os.environ.get("DISCORD_OPS_WEBHOOK_URL", "")
        if not url:
            _logger.info("grove.calendar.digest: no DISCORD_EVENTS/OPS webhook configured — skipping post")
            return
        try:
            requests.post(
                url,
                json={"content": message[:2000], "allowed_mentions": {"parse": []}},
                timeout=10,
            )
        except Exception:  # noqa: BLE001 — digest post is best-effort
            _logger.warning("grove.calendar.digest: Discord post failed", exc_info=True)
