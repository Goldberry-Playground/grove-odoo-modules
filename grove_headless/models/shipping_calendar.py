"""USDA-zone shipping calendar for Grove checkout (design: vault
wiki/Software/Grove Shipping). Pure Python, stdlib only — mirrors the
shipping_zones.py testability contract.

Everything here keys off the DESTINATION USDA hardiness zone (int 2-10),
resolved from the shipping ZIP via the vendored PHZM matrix — never off
state (states span multiple USDA zones; WV alone runs 5a-7a).
"""

import csv
import os
from datetime import date, timedelta
from functools import lru_cache

_MATRIX_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "zip_usda_zone.csv")


@lru_cache(maxsize=1)
def _zip_matrix() -> dict[str, int]:
    try:
        with open(_MATRIX_PATH, newline="", encoding="utf-8") as fh:
            return {row["zip"]: int(row["zone"]) for row in csv.DictReader(fh)}
    except (OSError, ValueError, KeyError, csv.Error):
        return {}


def usda_zone_for_zip(zip_code) -> int | None:
    """Integer USDA zone (2-10) for a 5-digit ZIP, or None if unknown."""
    if not zip_code or not isinstance(zip_code, str):
        return None
    raw = zip_code.strip()
    if not (len(raw) == 5 or (len(raw) == 10 and raw[5] == "-")):
        return None
    zip5 = raw[:5]
    if not zip5.isdigit():
        return None
    return _zip_matrix().get(zip5)


# ── Calendar data (Josh, 2026-07-02; vault wiki/Software/Grove Shipping) ────
# (month, day) tuples; year resolved at query time.
WAVE_SCHEDULE: dict[int, dict] = {
    2: {
        "fall": {"ship_start": (11, 2), "ship_end": (11, 13), "order_by": (11, 12)},
        "spring": {"ship_start": (4, 19), "ship_end": (6, 6), "order_by": (5, 31)},
    },
    3: {
        "fall": {"ship_start": (11, 2), "ship_end": (11, 13), "order_by": (11, 12)},
        "spring": {"ship_start": (4, 19), "ship_end": (6, 6), "order_by": (5, 31)},
    },
    4: {
        "fall": {"ship_start": (11, 2), "ship_end": (11, 19), "order_by": (11, 16)},
        "spring": {"ship_start": (4, 19), "ship_end": (6, 6), "order_by": (5, 31)},
    },
    5: {
        "fall": {"ship_start": (11, 2), "ship_end": (11, 19), "order_by": (11, 16)},
        "spring": {"ship_start": (4, 12), "ship_end": (6, 6), "order_by": (5, 31)},
    },
    6: {
        "fall": {"ship_start": (11, 9), "ship_end": (11, 26), "order_by": (11, 21)},
        "spring": {"ship_start": (4, 5), "ship_end": (6, 6), "order_by": (5, 31)},
    },
    7: {
        "fall": {"ship_start": (11, 9), "ship_end": (11, 26), "order_by": (11, 21)},
        "spring": {"ship_start": (3, 16), "ship_end": (5, 24), "order_by": (5, 17)},
    },
    8: {
        "fall": {"ship_start": (11, 9), "ship_end": (12, 12), "order_by": (11, 21)},
        "spring": {"ship_start": (3, 1), "ship_end": (4, 30), "order_by": (4, 16)},
    },
    9: {
        "fall": {"ship_start": (11, 9), "ship_end": (12, 12), "order_by": (11, 21)},
        "spring": {"ship_start": (3, 1), "ship_end": (4, 30), "order_by": (4, 16)},
    },
    10: {
        "fall": {"ship_start": (11, 9), "ship_end": (12, 12), "order_by": (11, 21)},
        "spring": {"ship_start": (3, 1), "ship_end": (4, 30), "order_by": (4, 16)},
    },
}

# On-demand no-ship ranges (conservative launch defaults — loosen with
# nursery-manager experience via PR). Jan+Feb is the global floor regardless.
NO_SHIP_MONTHS = (1, 2)
FREEZE_WINDOWS: dict[int, tuple] = {
    2: ((12, 1), (3, 15)),
    3: ((12, 1), (3, 15)),
    4: ((12, 1), (3, 15)),
    5: ((12, 1), (3, 15)),
    6: ((12, 15), (3, 1)),
    7: ((12, 15), (3, 1)),
    8: ((1, 1), (2, 28)),
    9: ((1, 1), (2, 28)),
    10: ((1, 1), (2, 28)),
}


def _in_md_window(today: date, start_md, end_md) -> bool:
    """Is `today` inside a (month, day) window that may wrap the year end?"""
    t = (today.month, today.day)
    if start_md <= end_md:
        return start_md <= t <= end_md
    return t >= start_md or t <= end_md  # wraps Dec -> Mar


def _next_occurrence(md, today: date) -> date:
    """The next date with (month, day) == md on or after today."""
    m, d = md
    candidate = date(today.year, m, min(d, 28) if (m, d) == (2, 29) else d)
    return candidate if candidate >= today else date(today.year + 1, m, d)


def _next_wave(zone: int, today: date) -> dict | None:
    """Return the next bareroot wave (fall or spring) with the earliest ship_start."""
    waves = WAVE_SCHEDULE.get(zone)
    if not waves:
        return None
    candidates = []
    for season, w in waves.items():
        order_by = _next_occurrence(w["order_by"], today)
        # Wave ship dates share the same year as their order_by deadline.
        ship_start = date(order_by.year, *w["ship_start"])
        ship_end = date(order_by.year, *w["ship_end"])
        candidates.append(
            {
                "season": season,
                "ship_start": ship_start,
                "ship_end": ship_end,
                "order_by": order_by,
            }
        )
    return min(candidates, key=lambda c: c["ship_start"])


def _freeze_end(zone: int, today: date) -> date:
    end_md = FREEZE_WINDOWS[zone][1]
    return _next_occurrence(end_md, today) + timedelta(days=1)


def ship_options(zip_code, tier: str, today: date) -> dict:
    """Can this order ship now, and if not, when? See vault spec.

    Conservative on unknowns: unrecognized ZIP -> ships_now False.
    """
    zone = usda_zone_for_zip(zip_code)
    result = {"usda_zone": zone, "ships_now": False, "next_wave": None, "defer_to": None}
    if zone is None:
        return result
    frozen = today.month in NO_SHIP_MONTHS or _in_md_window(today, *FREEZE_WINDOWS[zone])
    result["ships_now"] = not frozen
    if tier == "bareroot":
        result["next_wave"] = _next_wave(zone, today)
        if frozen:
            result["defer_to"] = result["next_wave"]["ship_start"] if result["next_wave"] else None
    elif frozen:
        result["defer_to"] = _freeze_end(zone, today)
    return result


def serialize_ship_options(result: dict) -> dict:
    """Convert ship_options result dict dates to ISO strings for JSON serialization."""
    out = dict(result)
    if out.get("defer_to"):
        out["defer_to"] = out["defer_to"].isoformat()
    if out.get("next_wave"):
        nw = dict(out["next_wave"])
        for k in ("ship_start", "ship_end", "order_by"):
            nw[k] = nw[k].isoformat()
        out["next_wave"] = nw
    return out


# ── Annual shipping calendar (Box Engine fulfillment model rev 2, GOL-1172) ──
# One calendar shared by EVERY species, keyed to the destination USDA plant
# hardiness zone (int 2-10). Two dormant-bareroot mailing seasons per year
# (fall + spring), each staggered per zone, plus a leafed "peat & bagged"
# remainder that ships on the normal 5-10 business-day policy.
#
# This is ANNUAL CONFIG, not a code constant. The defaults below are the real
# per-zone Arbor Day windows (GOL-1177) — cold zones ship earliest in fall /
# latest in spring, warm zones the reverse — overridable at runtime WITHOUT A
# DEPLOY via the `grove_headless.shipping_calendar` system parameter (a JSON
# blob deep-merged over these defaults by `merge_calendar_override`). Zones
# drift over time, so ops can restate a zone's window (and its order deadline)
# once a year through that parameter, no code change required.
#
# WARNING: the USDA hardiness zone keyed here is NOT the UPS distance "shipping
# zone" (zone_1..zone_5) that keys the rate table in shipping_zones.py. Same
# word, different axis.

# Global preorder-open switch dates (month, day). Fall preorder opens Aug 15;
# spring preorder opens Nov 1.
PREORDER_OPEN: dict[str, tuple[int, int]] = {"fall": (8, 15), "spring": (11, 1)}

# Leafed / peat & bagged remainder — trees are leafed out at the nursery and
# ship potted-in-peat on the normal 5-10 business-day policy. Informational for
# the frontend label; the resolver treats every date outside a dormant window
# or preorder as this "ships now" mode (see resolve_fulfillment).
LEAFED_WINDOW: tuple[tuple[int, int], tuple[int, int]] = ((5, 6), (8, 14))

# Normal processing SLA (business days) for leafed / peat & bagged and for the
# shipped-past-your-zone fallback.
FULFILLMENT_DAYS: tuple[int, int] = (5, 10)

# Windows are APPROXIMATE — "weather permitting." Per Josh, a hard-frost snap
# can push a shipment, so every displayed window carries a weather-permitting
# qualifier (`approximate`) and, when a known delay is in effect, an
# admin-editable advisory (`weather_hold_note`) that the frontend renders as a
# banner. Both are overridable at runtime via the system parameter (GOL-1177).
APPROXIMATE_DEFAULT: bool = True
WEATHER_HOLD_NOTE_DEFAULT: str | None = None

# Per-USDA-zone dormant ship windows + order deadlines — the REAL Arbor Day
# chart (GOL-1177; Josh via arborday.org/nursery-shipping, bareroot columns
# only — the potted column is deliberately NOT imported: potted is farm pickup
# only). These are the same windows the legacy ``ship_options`` path already
# serves from ``WAVE_SCHEDULE`` above, so the resolver calendar is sourced from
# it — one authority, the two can never drift. Cold zones ship earliest in fall
# / latest in spring; warm zones the reverse. Each window is non-wrapping
# within its season. Narrow further per zone via the system parameter (no code
# edit) as agronomy data refines.
ZONE_SHIP_WINDOWS: dict[int, dict[str, tuple]] = {
    z: {
        "fall": (w["fall"]["ship_start"], w["fall"]["ship_end"]),
        "spring": (w["spring"]["ship_start"], w["spring"]["ship_end"]),
    }
    for z, w in WAVE_SCHEDULE.items()
}
ZONE_ORDER_DEADLINES: dict[int, dict[str, tuple]] = {
    z: {"fall": w["fall"]["order_by"], "spring": w["spring"]["order_by"]}
    for z, w in WAVE_SCHEDULE.items()
}
# Fallback windows for a zone an override introduces that the real chart does
# not cover (defensive only — the chart already spans every USDA zone 2-10).
_FALL_DEFAULT: tuple[tuple[int, int], tuple[int, int]] = ((11, 2), (11, 26))
_SPRING_DEFAULT: tuple[tuple[int, int], tuple[int, int]] = ((3, 1), (6, 6))

# The three shippable modes the frontend (GOL-1114) resolves to, plus the
# fallback. Kept here so the contract has one authority.
MODE_PREORDER = "bareroot-preorder"
MODE_IN_WINDOW = "bareroot-in-window"
MODE_PEAT = "peat-and-bagged"


def _md(pair) -> tuple[int, int]:
    """Coerce a [m, d] list (from JSON) or (m, d) tuple to a comparable tuple.

    Comparisons like ``(month, day) <= start`` raise TypeError if one side is a
    list and the other a tuple, so every month-day pair is normalized to a
    tuple before it enters the calendar.
    """
    return (int(pair[0]), int(pair[1]))


def default_calendar() -> dict:
    """A fresh, normalized copy of the launch calendar (tuples, int zone keys)."""
    return {
        "preorder_open": {season: _md(md) for season, md in PREORDER_OPEN.items()},
        "leafed_window": (_md(LEAFED_WINDOW[0]), _md(LEAFED_WINDOW[1])),
        "fulfillment_days": (int(FULFILLMENT_DAYS[0]), int(FULFILLMENT_DAYS[1])),
        "approximate": APPROXIMATE_DEFAULT,
        "weather_hold_note": WEATHER_HOLD_NOTE_DEFAULT,
        "zones": {
            z: {
                "fall": (_md(w["fall"][0]), _md(w["fall"][1])),
                "spring": (_md(w["spring"][0]), _md(w["spring"][1])),
                "fall_order_deadline": _md(ZONE_ORDER_DEADLINES[z]["fall"]),
                "spring_order_deadline": _md(ZONE_ORDER_DEADLINES[z]["spring"]),
            }
            for z, w in ZONE_SHIP_WINDOWS.items()
        },
    }


def _season_override(val) -> tuple:
    """Parse one override season entry -> (window | None, order_deadline | None).

    Accepts either a bare ``[[m, d], [m, d]]`` ship-window pair (the shape the
    feed emits) OR Josh's proposed ``{"ship": [[m, d], [m, d]], "order_deadline":
    [m, d]}`` object (GOL-1114 comment e8b071f9). Any piece the override omits
    comes back None so the caller keeps that default.
    """
    if isinstance(val, dict):
        window = None
        ship = val.get("ship")
        if isinstance(ship, (list, tuple)) and len(ship) == 2:
            window = (_md(ship[0]), _md(ship[1]))
        deadline = _md(val["order_deadline"]) if val.get("order_deadline") else None
        return window, deadline
    if isinstance(val, (list, tuple)) and len(val) == 2:
        return (_md(val[0]), _md(val[1])), None
    return None, None


def merge_calendar_override(override) -> dict:
    """Deep-merge an admin override (parsed JSON) over the default calendar.

    The override is partial: any key it omits keeps the default, and ``zones``
    is merged per zone so an admin can narrow one zone's window without
    re-stating all nine. Returns a normalized calendar. A non-dict override
    (or None) yields the plain defaults — the feed must never break because a
    system parameter holds garbage.

    A zone's season entry may be either a bare ``[[m, d], [m, d]]`` window or
    the ``{"ship": ..., "order_deadline": ...}`` object form (see
    ``_season_override``); the flat ``"<season>_order_deadline"`` key is also
    honored. ``approximate`` and ``weather_hold_note`` let ops flag a known
    frost delay without a deploy (GOL-1177).
    """
    cal = default_calendar()
    if not isinstance(override, dict):
        return cal
    po = override.get("preorder_open")
    if isinstance(po, dict):
        cal["preorder_open"] = {**cal["preorder_open"], **{s: _md(md) for s, md in po.items()}}
    if "leafed_window" in override:
        lw = override["leafed_window"]
        cal["leafed_window"] = (_md(lw[0]), _md(lw[1]))
    if "fulfillment_days" in override:
        fd = override["fulfillment_days"]
        cal["fulfillment_days"] = (int(fd[0]), int(fd[1]))
    if "approximate" in override:
        cal["approximate"] = bool(override["approximate"])
    if "weather_hold_note" in override:
        whn = override["weather_hold_note"]
        cal["weather_hold_note"] = str(whn) if whn is not None else None
    zones = override.get("zones")
    if isinstance(zones, dict):
        for zk, zv in zones.items():
            z = int(zk)  # JSON object keys arrive as strings
            cur = dict(
                cal["zones"].get(
                    z,
                    {
                        "fall": _FALL_DEFAULT,
                        "spring": _SPRING_DEFAULT,
                        "fall_order_deadline": None,
                        "spring_order_deadline": None,
                    },
                )
            )
            if isinstance(zv, dict):
                for season in ("fall", "spring"):
                    if season in zv:
                        window, deadline = _season_override(zv[season])
                        if window is not None:
                            cur[season] = window
                        if deadline is not None:
                            cur[f"{season}_order_deadline"] = deadline
                    flat_deadline = f"{season}_order_deadline"
                    if flat_deadline in zv and zv[flat_deadline] is not None:
                        cur[flat_deadline] = _md(zv[flat_deadline])
            cal["zones"][z] = cur
    return cal


def _window_dates(start_md, end_md, today: date, upcoming: bool) -> tuple[date, date]:
    """Concrete [start, end] dates for a non-wrapping season window.

    ``upcoming`` (a preorder wave): anchor to the next occurrence of the start
    on/after today. Otherwise (today is inside the window): anchor to today's
    calendar year.
    """
    if upcoming:
        start = _next_occurrence(start_md, today)
    else:
        start = date(today.year, *start_md)
    return start, date(start.year, *end_md)


def resolve_fulfillment(zone, today: date, calendar: dict | None = None) -> dict:
    """Resolve exactly ONE shippable mode for a (zone, date), per the rev-2 model.

    Returns ``mode`` = one of ``bareroot-preorder`` | ``bareroot-in-window`` |
    ``peat-and-bagged`` (or None for an unknown/unconfigured zone), plus the
    ship window and a ship-timing string the frontend (GOL-1114) can surface
    verbatim or reformat.

    Phase order matters — an order placed AFTER the shopper's zone window has
    already shipped falls through every dormant branch to ``peat-and-bagged``
    (ships now on the 5-10 business-day policy), and is NOT held as a preorder
    for the next season. That shipped-past-your-zone fallback is deliberately
    the same "ships now" path used for the leafed / peat & bagged season, so it
    applies to dormant and leafed alike (Josh, box-fulfillment-model rev 2).
    """
    cal = calendar or default_calendar()
    fulfillment = list(cal["fulfillment_days"])
    result = {
        "usda_zone": zone,
        "mode": None,
        "season": None,
        "ship_window": None,
        "order_deadline": None,
        "fulfillment_days": None,
        "ship_timing": None,
        # Advisory (GOL-1177): windows are estimates, "weather permitting".
        "approximate": bool(cal.get("approximate", APPROXIMATE_DEFAULT)),
        "weather_hold_note": cal.get("weather_hold_note", WEATHER_HOLD_NOTE_DEFAULT),
    }
    zwin = cal["zones"].get(zone)
    if zwin is None:
        return result

    fall_s, fall_e = zwin["fall"]
    spring_s, spring_e = zwin["spring"]
    fall_open = cal["preorder_open"]["fall"]
    spring_open = cal["preorder_open"]["spring"]

    def _deadline_iso(season, year):
        dl = zwin.get(f"{season}_order_deadline")
        return date(year, dl[0], dl[1]).isoformat() if dl else None

    def _preorder(season, start_md, end_md):
        s, e = _window_dates(start_md, end_md, today, upcoming=True)
        result.update(
            mode=MODE_PREORDER,
            season=season,
            ship_window=[s.isoformat(), e.isoformat()],
            order_deadline=_deadline_iso(season, s.year),
            ship_timing=f"Reserve now — ships {_fmt(s)}–{_fmt(e)}",
        )

    def _in_window(season, start_md, end_md):
        s, e = _window_dates(start_md, end_md, today, upcoming=False)
        result.update(
            mode=MODE_IN_WINDOW,
            season=season,
            ship_window=[s.isoformat(), e.isoformat()],
            order_deadline=_deadline_iso(season, s.year),
            ship_timing=f"Ships now in your {season} window (by {_fmt(e)})",
        )

    def _peat():
        result.update(
            mode=MODE_PEAT,
            fulfillment_days=fulfillment,
            ship_timing=f"Ships in {fulfillment[0]}–{fulfillment[1]} business days",
        )

    # 1) Inside a dormant ship window -> ships promptly within that wave.
    if _in_md_window(today, spring_s, spring_e):
        _in_window("spring", spring_s, spring_e)
    elif _in_md_window(today, fall_s, fall_e):
        _in_window("fall", fall_s, fall_e)
    # 2) Preorder open for the upcoming wave (open date -> ship start).
    elif _in_md_window(today, fall_open, _prev_day(fall_s)):
        _preorder("fall", fall_s, fall_e)
    elif _in_md_window(today, spring_open, _prev_day(spring_s)):
        _preorder("spring", spring_s, spring_e)
    # 3) Everything else (leafed season + shipped-past-your-zone) ships now.
    else:
        _peat()
    return result


def _prev_day(md: tuple[int, int]) -> tuple[int, int]:
    """The (month, day) one day before ``md`` — the inclusive end of a preorder
    window that runs up to (but not into) a ship_start."""
    y = 2001  # non-leap anchor; only month/day are read back
    d = date(y, md[0], md[1]) - timedelta(days=1)
    return (d.month, d.day)


_MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _fmt(d: date) -> str:
    """ "Sep 15" — locale-free, no %-d portability trap."""
    return f"{_MONTHS[d.month]} {d.day}"


def serialize_calendar(calendar: dict | None = None) -> dict:
    """JSON-safe annual calendar for the rate feed's top-level ``calendar`` key.

    Month-day pairs become ``[m, d]`` lists and USDA zone keys become strings
    ("2".."10"), the shape the frontend indexes with String(usdaZone). The
    per-zone ``fall``/``spring`` window pairs are unchanged; ``*_order_deadline``
    (nullable), ``approximate`` and ``weather_hold_note`` are additive
    (GOL-1177) so an existing consumer that only reads windows is unaffected.
    """
    cal = calendar or default_calendar()

    def _deadline(w, season):
        md = w.get(f"{season}_order_deadline")
        return list(md) if md else None

    return {
        "preorder_open": {season: list(md) for season, md in cal["preorder_open"].items()},
        "leafed_window": [list(cal["leafed_window"][0]), list(cal["leafed_window"][1])],
        "fulfillment_days": list(cal["fulfillment_days"]),
        "approximate": bool(cal.get("approximate", APPROXIMATE_DEFAULT)),
        "weather_hold_note": cal.get("weather_hold_note", WEATHER_HOLD_NOTE_DEFAULT),
        "zones": {
            str(z): {
                "fall": [list(w["fall"][0]), list(w["fall"][1])],
                "spring": [list(w["spring"][0]), list(w["spring"][1])],
                "fall_order_deadline": _deadline(w, "fall"),
                "spring_order_deadline": _deadline(w, "spring"),
            }
            for z, w in sorted(cal["zones"].items())
        },
    }
