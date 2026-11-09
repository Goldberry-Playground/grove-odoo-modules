"""Odoo ``calendar.recurrence`` → Discord ``recurrence_rule`` mapping (GOL-3832, phase D).

Pure Python / stdlib so the mapping unit-tests without an Odoo DB, mirroring
``discord_format.py``. No ``odoo`` import: the Odoo-side sync
(``calendar_discord.py``) normalises each ``calendar.recurrence`` to the plain
dict :func:`build_recurrence_rule` expects and attaches the result to the Discord
scheduled-event payload.

Discord's recurrence rule (API docs, "Guild Scheduled Event Recurrence Rule")
only expresses a narrow set of patterns, and an application may not set ``count``
or ``end``:

    frequency : YEARLY 0, MONTHLY 1, WEEKLY 2, DAILY 3
    interval  : 2 only for WEEKLY, otherwise 1
    WEEKLY    : exactly one ``by_weekday`` (Monday 0 .. Sunday 6)
    DAILY     : a ``by_weekday`` set, only every-day / weekdays / weekends
    MONTHLY   : exactly one ``by_n_weekday`` {n: 1..5, day}
    YEARLY    : one ``by_month`` and one ``by_month_day``

Anything Odoo can express that Discord cannot (weekly on several weekdays,
monthly by date, monthly last-weekday, any interval above 1 outside weekly,
daily every N>1 days) returns ``None`` so the caller falls back to posting one
occurrence at a time (spec section 3).

The returned dict omits ``start``; the caller sets ``recurrence_rule.start`` to
the Discord ``scheduled_start_time`` (they must be equal).

Recurrence dict shape (built by the sync from a ``calendar.recurrence`` row):
    rrule_type : "daily" | "weekly" | "monthly" | "yearly"
    interval   : int
    mon..sun   : bool          (the seven weekday flags, weekly)
    month_by   : "date" | "day"  (monthly)
    byday      : str | False    ("1".."4", "-1": nth weekday, monthly month_by='day')
    weekday    : str | False    ("mon".."sun" or "MON".."SUN": which weekday, monthly)
"""

from calendar import monthrange
from datetime import timedelta

# Odoo weekday flag name -> Discord weekday int (Monday 0 .. Sunday 6).
_WEEKDAY_FLAGS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_INDEX = {name: i for i, name in enumerate(_WEEKDAY_FLAGS)}

# Discord frequency codes.
FREQ_YEARLY = 0
FREQ_MONTHLY = 1
FREQ_WEEKLY = 2
FREQ_DAILY = 3

# Every weekday Monday..Sunday, for a plain daily rule.
_EVERY_DAY = [0, 1, 2, 3, 4, 5, 6]


def _weekday_to_index(value):
    """Map an Odoo ``weekday`` selection value ('MON'.. or 'mon'..) to 0..6, or None."""
    if not value:
        return None
    return _WEEKDAY_INDEX.get(str(value).strip().lower())


def _set_weekdays(rec):
    """The Discord weekday ints for the weekly flags set true on *rec*."""
    return [i for i, name in enumerate(_WEEKDAY_FLAGS) if rec.get(name)]


def build_recurrence_rule(rec, start_dt):
    """Return a Discord ``recurrence_rule`` dict for the Odoo recurrence *rec*, or None.

    *start_dt* is the representative occurrence's start (a ``datetime``); it feeds
    the ``by_month`` / ``by_month_day`` of a yearly rule. ``None`` means Discord
    cannot express the pattern and the caller must fall back to one occurrence at
    a time.
    """
    rrule_type = rec.get("rrule_type")
    # Odoo stores interval as a positive int; guard against 0/None.
    interval = int(rec.get("interval") or 1)

    if rrule_type == "weekly":
        days = _set_weekdays(rec)
        # Discord WEEKLY: exactly one weekday, interval 1 or 2.
        if len(days) == 1 and interval in (1, 2):
            return {"frequency": FREQ_WEEKLY, "interval": interval, "by_weekday": days}
        return None

    if rrule_type == "daily":
        # Odoo daily is "every N days"; Discord DAILY repeats every day. Only
        # interval 1 maps cleanly (every-day set); every-N>1 has no Discord form.
        if interval == 1:
            return {"frequency": FREQ_DAILY, "interval": 1, "by_weekday": list(_EVERY_DAY)}
        return None

    if rrule_type == "monthly":
        # Discord MONTHLY: the nth weekday of the month ({n, day}). Odoo expresses
        # that with month_by='day', byday in 1..4 (first..fourth) and a weekday.
        # Monthly-by-date and last-weekday (byday '-1') have no Discord form.
        if interval != 1 or rec.get("month_by") != "day":
            return None
        try:
            n = int(rec.get("byday"))
        except (TypeError, ValueError):
            return None
        day = _weekday_to_index(rec.get("weekday"))
        if day is None or n < 1 or n > 5:
            return None
        return {
            "frequency": FREQ_MONTHLY,
            "interval": 1,
            "by_n_weekday": [{"n": n, "day": day}],
        }

    if rrule_type == "yearly":
        # Discord YEARLY: one by_month + one by_month_day, taken from the
        # representative occurrence. Odoo yearly is interval-based on the event's
        # own month/day, so interval must be 1.
        if interval != 1 or start_dt is None:
            return None
        return {
            "frequency": FREQ_YEARLY,
            "interval": 1,
            "by_month": [start_dt.month],
            "by_month_day": [start_dt.day],
        }

    return None


def _nth_weekday_of_month(year, month, weekday, n):
    """The date of the *n*-th *weekday* (Mon 0..Sun 6) in *year*/*month*, or None.

    *n* is 1..5 (first..fifth); None when the month has no such occurrence.
    """
    first_weekday, days_in_month = monthrange(year, month)  # first_weekday: Mon==0
    offset = (weekday - first_weekday) % 7
    day = 1 + offset + (n - 1) * 7
    if day > days_in_month:
        return None
    return day


def expected_occurrences(rec, rep_start, horizon):
    """Local wall-clock datetimes the recurrence *rec* is expected to fire at.

    Exception detection compares these against the actual Odoo occurrence rows
    (spec D.7): a predicted slot with no row is a cancellation, a row off the grid
    is a move. Everything is naive **local wall-clock** time — *rep_start* and
    *horizon* are the representative's start and the window end already converted
    to the event's timezone and made naive — so DST never enters the comparison
    (Google repeats at a fixed local time; stepping naive wall-clock preserves
    that). Only patterns Discord can express (the ones :func:`build_recurrence_rule`
    maps) are expanded; anything else returns ``[]`` (it is published as
    fallbacks, which carry no series exceptions).

    The list starts at *rep_start* (inclusive) and runs through *horizon*
    (inclusive).
    """
    rrule_type = rec.get("rrule_type")
    interval = int(rec.get("interval") or 1)
    out = []

    if rrule_type == "weekly":
        days = _set_weekdays(rec)
        if len(days) != 1 or interval not in (1, 2):
            return []
        dt = rep_start
        step = timedelta(weeks=interval)
        while dt <= horizon:
            out.append(dt)
            dt += step
        return out

    if rrule_type == "daily":
        if interval != 1:
            return []
        dt = rep_start
        while dt <= horizon:
            out.append(dt)
            dt += timedelta(days=1)
        return out

    if rrule_type == "monthly":
        if interval != 1 or rec.get("month_by") != "day":
            return []
        try:
            n = int(rec.get("byday"))
        except (TypeError, ValueError):
            return []
        weekday = _weekday_to_index(rec.get("weekday"))
        if weekday is None or n < 1 or n > 5:
            return []
        year, month = rep_start.year, rep_start.month
        while True:
            day = _nth_weekday_of_month(year, month, weekday, n)
            if day is not None:
                dt = rep_start.replace(year=year, month=month, day=day)
                if dt > horizon:
                    break
                if dt >= rep_start:
                    out.append(dt)
            month += 1
            if month > 12:
                month, year = 1, year + 1
            # Guard: the horizon bounds a sane number of months.
            if year > horizon.year + 1:
                break
        return out

    if rrule_type == "yearly":
        if interval != 1:
            return []
        dt = rep_start
        while dt <= horizon:
            out.append(dt)
            dt = dt.replace(year=dt.year + 1)
        return out

    return []
