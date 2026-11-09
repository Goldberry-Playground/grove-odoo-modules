"""Discord formatting for the weekly 'This week at the Grove' digest (GOL-3830).

Pure Python / stdlib so the digest copy unit-tests without an Odoo DB, mirroring
the ``grove_headless/models/order_digest.py`` convention. No ``odoo`` import: the
Odoo-side cron (``calendar_digest.py``) queries ``calendar.event``, normalises
each record to the plain-dict shape expected here, and POSTs the result to a
Discord webhook.

Times are rendered in America/New_York (``zoneinfo``, stdlib). Odoo stores timed
events as naive UTC datetimes; this module converts for display, the same way the
weekly order rollup does.

Event dict shape (built by the cron):
    name:     str
    allday:   bool
    location: str                       ("" when unset)
    start:    datetime.date   (allday)  | datetime.datetime (timed, naive UTC)
    stop:     datetime.date   (allday)  | datetime.datetime (timed, naive UTC)

The ``–`` (en dash) in a date range and the ``—`` (em dash) between the date and
the event title are taken verbatim from the approved design
(docs/superpowers/specs/2026-11-07-discord-calendar-design.md); the house
"no em dash in prose" rule covers customer/merchant prose, not this structured
bullet list.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

_EASTERN = ZoneInfo("America/New_York")

# date.weekday(): Monday == 0. Month index 1..12. Table-driven so the output is
# locale- and platform-independent (no %-d / %a, which are glibc-only — see
# order_digest._fmt_date).
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Discord rejects a message over 2,000 characters outright, so the digest is
# capped. In practice a week never approaches this; the cap is a safety net.
_DISCORD_CONTENT_LIMIT = 2000

# The voice channels the digest knows about in phase B, which has no Discord bot
# token and so cannot list the guild's channels. A Location equal to one of these
# names renders as a voice room (🔊 <room>); any other text renders verbatim.
# Phase D (Train #6) fetches the live channel list from the Discord API and feeds
# it to the same ``match_room`` helper, at which point this stand-in is unused.
# Kept in sync by hand with the server's voice rooms as Josh set them 2026-11-08.
DEFAULT_VOICE_ROOMS = ("grove-chat", "General", "Events", "Game-time")


def _normalize_room(name: str) -> str:
    """Casefold a room/location for comparison: trim, drop a leading ``#``, lower."""
    return (name or "").strip().lstrip("#").strip().lower()


def match_room(location, channels):
    """Return the channel from *channels* whose name matches *location*, else None.

    Each entry in *channels* is either a plain name string (phase B, where the
    digest passes :data:`DEFAULT_VOICE_ROOMS`) or a dict carrying a ``"name"`` key
    (phase D, where the sync passes the Discord API's channel objects). The same
    matching rule — trim, strip a leading ``#``, case-insensitive — serves both,
    so the digest and the native-events sync agree on what counts as a room.
    The matched entry is returned verbatim so a caller can read its ``id`` too.
    """
    needle = _normalize_room(location)
    if not needle:
        return None
    for ch in channels:
        name = ch if isinstance(ch, str) else (ch.get("name") or "")
        if _normalize_room(name) == needle:
            return ch
    return None


def _to_eastern(dt: datetime) -> datetime:
    """Return *dt* in America/New_York. A naive datetime is read as UTC (Odoo's
    storage convention)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_EASTERN)


def _fmt_md(d: date) -> str:
    """'Nov 9' — month + day, used in the window header."""
    return f"{_MONTHS[d.month]} {d.day}"


def _fmt_wmd(d: date) -> str:
    """'Fri Nov 13' — weekday + month + day, used on each event line."""
    return f"{_WEEKDAYS[d.weekday()]} {_MONTHS[d.month]} {d.day}"


def _fmt_time(dt: datetime) -> str:
    """'6:30 PM' — 12-hour clock, no leading zero on the hour."""
    hour = dt.hour % 12 or 12
    meridiem = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {meridiem}"


def _event_dates(event: dict) -> tuple[date, date, datetime | None]:
    """Resolve an event's display start/stop *dates* (in Eastern for timed
    events) plus the Eastern start *datetime* when the event is timed.

    Returns ``(start_date, stop_date, start_dt_or_None)``.
    """
    if event.get("allday"):
        start = event["start"]
        stop = event.get("stop") or start
        # Odoo stores an all-day event's start_date/stop_date as inclusive dates.
        return start, stop, None
    start_dt = _to_eastern(event["start"])
    stop_src = event.get("stop") or event["start"]
    stop_dt = _to_eastern(stop_src)
    return start_dt.date(), stop_dt.date(), start_dt


def format_event_line(event: dict, voice_rooms=()) -> str:
    """One Discord bullet for *event*.

    - All-day, single day:  ``- **Fri Nov 13** — Young Farmers meeting``
    - Timed, single day:    ``- **Fri Nov 27, 6:30 PM** — Co-op alliance contract``
    - Multi-day (either):   ``- **Sat Nov 14 – Sun Nov 15** — Harvest weekend``
    - The place is appended after `` · ``: a Location matching one of
      *voice_rooms* renders as ``🔊 <room>``, any other text renders verbatim,
      and a blank Location adds nothing. The description is never shown (it may
      hold staff notes).
    """
    start_date, stop_date, start_dt = _event_dates(event)

    if stop_date > start_date:
        # Multi-day events show a date range only, no time (per spec).
        when = f"{_fmt_wmd(start_date)} – {_fmt_wmd(stop_date)}"
    elif start_dt is not None:
        # Single-day timed event: date + start time.
        when = f"{_fmt_wmd(start_date)}, {_fmt_time(start_dt)}"
    else:
        # Single-day all-day event: date only.
        when = _fmt_wmd(start_date)

    name = (event.get("name") or "").strip() or "(untitled)"
    line = f"- **{when}** — {name}"
    location = (event.get("location") or "").strip()
    if location:
        room = match_room(location, voice_rooms)
        if room is not None:
            room_name = room if isinstance(room, str) else (room.get("name") or location)
            line += f" · 🔊 {room_name}"
        else:
            line += f" · {location}"
    return line


def build_digest_message(
    events: list[dict],
    window_start: date,
    window_end: date,
    voice_rooms=(),
    limit: int = _DISCORD_CONTENT_LIMIT,
) -> str | None:
    """Render the full 'This week at the Grove' message, or ``None`` for an empty week.

    *events* must already be ordered by start (the cron queries
    ``order="start asc"``). The message is held to *limit* characters; when the
    list is too long it is cut on a line boundary with a trailing
    ``…and N more`` note so the markdown is never sliced mid-token.
    """
    if not events:
        return None

    header = f"📅 **This week at the Grove** ({_fmt_md(window_start)} – {_fmt_md(window_end)})"
    lines = [format_event_line(e, voice_rooms) for e in events]

    parts = [header]
    length = len(header)
    for i, line in enumerate(lines):
        addition = len(line) + 1  # the joining newline
        remaining = len(lines) - i  # this line plus everything after it
        note = f"\n…and {remaining} more"
        # Reserve room for the truncation note so the final string stays within
        # the limit even if we have to stop here.
        if length + addition + len(note) > limit:
            parts.append(note)
            break
        parts.append("\n" + line)
        length += addition

    message = "".join(parts)
    return message[:limit]  # belt-and-braces hard cap
