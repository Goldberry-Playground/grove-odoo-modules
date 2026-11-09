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
