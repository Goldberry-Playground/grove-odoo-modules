"""Seed pre-order season resolution (GOL-3257, Train #3).

Pure Python — no Odoo import — so the season logic runs both under Odoo's
``--test-enable`` runner and standalone (``pytest`` / direct exec), exactly
like ``shipping_zones``. ``seed_season`` reads only duck-typed attributes off
the object it is handed (``grove_seed_open``, ``grove_seed_ship_start`` /
``_end``, ``grove_seed_order_by``, ``grove_seed_cap_lb``,
``grove_seed_reserved_lb``), so a test can pass a ``SimpleNamespace`` and the
controller passes a ``product.template`` recordset.

Seeds are a pre-order (spec ``docs/superpowers/specs/2026-10-09-seed-preorders-
design.md`` §3): each seed template carries its own ship window, order-by date
and season cap. After the order-by date **or** once the cap is reached, the
season rolls over to next fall's harvest (option B) and the shopper is told
which harvest they are buying from. No cron: the "current season" is derived
from ``grove_seed_ship_start`` and moves forward when Josh edits the dates.
"""

from __future__ import annotations

import datetime

# Reasons a season has rolled over to next fall. ``order_by_passed`` dominates
# ``cap_reached`` when both are true in the same instant: once the last order
# day is behind us the season is closed on the calendar regardless of weight,
# and the date-based message ("pre-orders have closed") is the clearer one.
REASON_ORDER_BY_PASSED = "order_by_passed"
REASON_CAP_REACHED = "cap_reached"

# Refusal shown when a cart breaks the seed mixing rule (spec §6). Seed
# reservations settle on their own deposit and ship window, so a cart holding
# seeds cannot also hold trees, nor seeds from two different harvest years.
# Reuses the GOL-3246 red-message refusal channel on the storefront.
SEED_MIX_REFUSAL = "Seed reservations check out on their own."


def seed_cart_refusal(tiers, harvest_years) -> str | None:
    """Mixing rule for a seed cart (spec §6), pure so it unit-tests without a DB.

    ``tiers`` is the set (or any iterable) of effective shipping tiers present
    in the cart; ``harvest_years`` is the harvest years across its seed lines
    (ints; 0/None are ignored). Returns :data:`SEED_MIX_REFUSAL` when the cart
    mixes seeds with any non-seed line, or mixes seeds from two harvest years,
    else ``None``. A cart with no seed line is never refused here (the tree
    rules live elsewhere); two seed products sharing ONE harvest year ride a
    single $1 deposit and are allowed.
    """
    tiers = set(tiers)
    if "seed" not in tiers:
        return None
    if tiers - {"seed"}:
        return SEED_MIX_REFUSAL
    if len({y for y in harvest_years if y}) > 1:
        return SEED_MIX_REFUSAL
    return None


def _add_year(d: datetime.date, years: int = 1) -> datetime.date:
    """``d`` shifted by whole years, Feb-29 safe.

    Seed ship windows are Oct/Nov so this never actually hits a leap day, but
    guard it anyway rather than let a future date choice raise ValueError.
    """
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        # 29 Feb -> 28 Feb on a non-leap target year.
        return d.replace(year=d.year + years, day=28)


def seed_season(template, today: datetime.date, adding_lb: float = 0.0) -> dict:
    """Resolve the season a seed pre-order placed ``today`` reserves from.

    Returns a dict mirrored verbatim into the catalog payload's ``seedSeason``
    object (camelCased by the serializer):

        {
          "year":        int,            # harvest/ship year of the resolved season
          "ship_start":  date | None,    # resolved ship window (shifted +1y if rolled over)
          "ship_end":    date | None,
          "order_by":    date | None,
          "rolled_over": bool,
          "reason":      str | None,     # REASON_* when rolled_over, else None
          "open":        bool,           # master switch (grove_seed_open)
        }

    ``adding_lb`` is the pack weight a prospective add would consume against the
    cap; 0 for the plain catalog read, the line weight for the checkout
    re-check. A line that would push ``reserved_lb + adding_lb`` past the cap
    rolls over with ``reason == REASON_CAP_REACHED``.

    When ``grove_seed_open`` is false the product is not purchasable: ``open``
    is False and the current (un-rolled) season dates are returned so the PDP
    can still show the window behind a "Not taking reservations right now"
    state. A template with no ``grove_seed_ship_start`` set yields all-None
    dates and ``year`` of ``today`` — an unconfigured seed product.
    """
    is_open = bool(getattr(template, "grove_seed_open", False))
    ship_start = getattr(template, "grove_seed_ship_start", None) or None
    ship_end = getattr(template, "grove_seed_ship_end", None) or None
    order_by = getattr(template, "grove_seed_order_by", None) or None
    cap_lb = float(getattr(template, "grove_seed_cap_lb", 0.0) or 0.0)
    reserved_lb = float(getattr(template, "grove_seed_reserved_lb", 0.0) or 0.0)

    base_year = ship_start.year if ship_start else today.year

    def _result(years_forward: int, rolled_over: bool, reason: str | None) -> dict:
        return {
            "year": base_year + years_forward,
            "ship_start": _add_year(ship_start, years_forward) if ship_start else None,
            "ship_end": _add_year(ship_end, years_forward) if ship_end else None,
            "order_by": _add_year(order_by, years_forward) if order_by else None,
            "rolled_over": rolled_over,
            "reason": reason,
            "open": is_open,
        }

    # Master switch off, or the template is not configured with a season yet:
    # report the current window, not purchasable, no rollover.
    if not is_open or order_by is None:
        return _result(0, rolled_over=False, reason=None)

    # Current season still available: on/before the order-by date AND the cap
    # (incl. the prospective add) not yet reached.
    if today <= order_by and (reserved_lb + float(adding_lb or 0.0)) <= cap_lb:
        return _result(0, rolled_over=False, reason=None)

    # Rolled over to next fall. Date dominates weight when both apply.
    reason = REASON_ORDER_BY_PASSED if today > order_by else REASON_CAP_REACHED
    return _result(1, rolled_over=True, reason=reason)
