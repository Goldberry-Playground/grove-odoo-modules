"""Pure fallback-chain resolver for the live-ZIP checkout quote (GOL-3927, spec §5).

The checkout price per box follows a fixed chain: **live quote -> ZIP-area table
(card 2) -> code zone table** (the 10-14 hotfix rates). This module owns the pure
decision — "given what each level produced for a box, pick the level and record
why" — so it unit-tests without a database. The controller supplies the actual
level values (a live result or its failure reason, a ZIP-area price, a zone
price) and consumes the :class:`BoxQuote` / :class:`OrderQuote` it returns.

Prices here are carrier cost only. The flat $2.50-per-tree-order handling is added
once at the order level by the caller (spec Decisions; GOL-3257 seed orders carry
neither a quote nor handling), never per box.

No Odoo imports: ``compute_order_shipping`` and the box engine are pure for the
same reason (see ``shipping_zones.py``), and the checkout flag keeps this whole
path dormant (checkout == the zone table byte-for-byte) until it is turned on.
"""

from dataclasses import dataclass, field

# Which pricing level actually set a box's rate. Recorded per box and rolled up
# to one order-level source (the least-live level any box used).
SOURCE_LIVE = "live"
SOURCE_ZIP_AREA = "zip_area"
SOURCE_ZONE = "zone"

# Order-level source = the WORST (least live) level any box fell back to, so an
# order where one box went live and one hit the zone table reads ``zone`` — the
# honest "this order was not fully live" signal the label tool and alert count on.
_SOURCE_RANK = {SOURCE_LIVE: 0, SOURCE_ZIP_AREA: 1, SOURCE_ZONE: 2}


@dataclass(frozen=True)
class BoxQuote:
    """One packed box priced through the chain."""

    box_id: str
    weight_lb: float
    price: float
    source: str
    service: str | None = None  # carrier mail-class, live quotes only
    service_title: str | None = None  # human service label, live quotes only
    reason: str | None = None  # why it fell back from live (None when live)


@dataclass(frozen=True)
class OrderQuote:
    """Every box priced, plus the order-level source and carrier subtotal."""

    boxes: tuple = field(default_factory=tuple)
    carrier_total: float = 0.0  # sum of box prices, NO handling fee
    source: str = SOURCE_ZONE
    fallback_reasons: tuple = field(default_factory=tuple)


def resolve_box(box_id, weight_lb, *, live=None, zip_area=None, zone=None) -> BoxQuote | None:
    """Pick the chain level for one box, or None when no level could price it.

    ``live`` is the live-quote outcome: a mapping with a numeric ``price`` (a
    hit, also carrying ``service``/``service_title``), a mapping with only a
    ``reason`` (a miss — the typed Pirate Ship error, logged as the fallback
    reason), or None (live not attempted: flag off or disabled). ``zip_area`` and
    ``zone`` are the fallback prices (float) or None when that level has no rate.
    """
    if live and live.get("price") is not None:
        return BoxQuote(
            box_id=box_id,
            weight_lb=weight_lb,
            price=round(float(live["price"]), 2),
            source=SOURCE_LIVE,
            service=live.get("service"),
            service_title=live.get("service_title"),
        )
    reason = (live or {}).get("reason")
    if zip_area is not None:
        return BoxQuote(box_id, weight_lb, round(float(zip_area), 2), SOURCE_ZIP_AREA, reason=reason or "no live quote")
    if zone is not None:
        return BoxQuote(box_id, weight_lb, round(float(zone), 2), SOURCE_ZONE, reason=reason or "no live quote")
    return None


def order_source(sources) -> str:
    """The order-level source: the least-live level any box used (``zone`` for an
    empty list — the safe default)."""
    worst = SOURCE_LIVE
    for s in sources:
        if _SOURCE_RANK.get(s, 2) > _SOURCE_RANK.get(worst, 2):
            worst = s
    return worst if sources else SOURCE_ZONE


def quote_boxes(resolved) -> OrderQuote | None:
    """Roll a list of per-box :class:`BoxQuote` (from :func:`resolve_box`) into an
    :class:`OrderQuote`. Any ``None`` box (unpriceable at every level) fails the
    whole order to None — same fail-safe contract as ``compute_order_shipping``:
    a partly priced order must never add a shipping line."""
    boxes = list(resolved)
    if not boxes or any(b is None for b in boxes):
        return None
    total = round(sum(b.price for b in boxes), 2)
    src = order_source([b.source for b in boxes])
    reasons = tuple(dict.fromkeys(b.reason for b in boxes if b.reason))
    return OrderQuote(boxes=tuple(boxes), carrier_total=total, source=src, fallback_reasons=reasons)
