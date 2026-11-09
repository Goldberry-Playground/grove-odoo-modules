"""Public events feed ``GET /grove/api/v1/events`` (GOL-3842, phase F).

A separate public calendar for the website, decided by Josh 2026-11-07: the
site gets the Public events that also carry the Website tag, not a dedicated
Google account. The feed reuses ``calendar.event._grove_website_domain`` so the
"which events are public on the website" rule has exactly one definition shared
with the Odoo "Website" calendar filter.

Only the whitelisted fields below ever leave the server: never attendees, never
the organizer, never an untagged or non-public event. The grove-sites page that
renders this stays with the spring 2027 visit-page work (GOL-1585).
"""

import json

from odoo import http, tools
from odoo.http import Response, request

# Matches the planner catalog feed (GOL-3379): cache at the edge for 5 minutes.
EVENTS_FEED_CACHE_CONTROL = "public, max-age=300"
# The site renders roughly a season ahead; a caller may widen with ?days=,
# capped so the feed can never be turned into a full-calendar dump.
EVENTS_FEED_DEFAULT_DAYS = 90
EVENTS_FEED_MAX_DAYS = 180
# Times in the body are UTC ISO 8601; this top-level hint tells the site which
# wall-clock zone to render them in (the farm's zone), without per-row offsets.
EVENTS_FEED_TZ = "America/New_York"


def _coerce_days(raw):
    """Parse the ``?days=`` query param: default 90, hard cap 180.

    Anything missing, non-numeric or below 1 falls back to the default rather
    than erroring, so a malformed query still returns the normal feed.
    """
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return EVENTS_FEED_DEFAULT_DAYS
    if days < 1:
        return EVENTS_FEED_DEFAULT_DAYS
    return min(days, EVENTS_FEED_MAX_DAYS)


def _utc_iso(value):
    """``calendar.event`` start/stop are naive UTC datetimes; emit ISO 8601 Z."""
    if not value:
        return None
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _description_text(markup):
    """Flatten the event's HTML description to plain text, or ''."""
    if not markup:
        return ""
    return tools.html2plaintext(markup).strip()


def _serialize_event(event):
    return {
        "id": event.id,
        "name": event.name or "",
        "start": _utc_iso(event.start),
        "stop": _utc_iso(event.stop),
        "allday": bool(event.allday),
        # A voice-room Location is returned as typed; the site decides how to show it.
        "location": event.location or "",
        "description_text": _description_text(event.description),
    }


class GroveCalendarEventsController(http.Controller):
    @http.route(
        "/grove/api/v1/events",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def grove_events_feed(self, days=None, **_kwargs):
        window_days = _coerce_days(days)
        # sudo: the public user cannot read calendar.event, but the domain has
        # already narrowed the set to tagged, published, public events and only
        # whitelisted fields are serialized, so nothing private escapes.
        Event = request.env["calendar.event"].sudo()
        events = Event.search(Event._grove_website_domain(window_days), order="start asc")
        body = {
            "tz": EVENTS_FEED_TZ,
            "events": [_serialize_event(event) for event in events],
        }
        return Response(
            json.dumps(body),
            status=200,
            content_type="application/json",
            headers={"Cache-Control": EVENTS_FEED_CACHE_CONTROL},
        )
