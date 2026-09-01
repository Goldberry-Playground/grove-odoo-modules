"""Box catalog + per-box packing engine for bareroot-only shipping (v2).

Replaces the one-tree-one-box model: shipping now prices PER PACKED BOX, not
per tree, because under USPS Ground Advantage dimensional billing the box drives
the cost — 50 dormant bareroots and 10 dormant bareroots in the same 32x12x12
bill nearly the same. Design: vault wiki/Software/Grove Shipping (Box Engine v2,
2026-07-31; recalibrated UPS Ground -> USPS Ground Advantage, GOL-1906).

Two packing modes, resolved from the ship date (trees are dormant or leafed
out at the nursery — it is a property of the season, not the product):

* ``dormant`` — bare crowns pack dense: 50+ per 12x12-cross-section box,
  ~15 per 8x8, or a single whip in the 16x6x4.
* ``leafed`` — canopy needs room: up to 4 per 8x8 box (conservative end of
  Josh's 4-8), 12x12 bulk boxes and the whip box are not used.

Every product variant carries a tree length class (the box length in inches
its height requires — 16/20/32/46); a tree may ride in any box at least that
long. Packing is exact min-cost per destination zone (small DP), with a
top-up pass so short trees fill spare capacity in boxes already opened for
tall ones.

Pure Python, no Odoo imports — same testability contract as
``shipping_zones.py`` (see ``tests/test_shipping_boxes.py``).
"""

import math
from datetime import date

# ── Packing modes ───────────────────────────────────────────────────────────
MODES: tuple[str, ...] = ("dormant", "leafed")

# Nursery dormancy window (month, day) inclusive — trees ship as dormant
# bareroot inside it, leafed-out bareroot outside it. Conservative default
# for the Summersville (z6) nursery; Josh + nursery manager own these dates
# (edit via PR, tests assert shape only).
DORMANT_START = (11, 1)
DORMANT_END = (4, 15)


def packing_mode(today: date) -> str:
    """ "dormant" inside the nursery dormancy window (wraps year end), else "leafed"."""
    t = (today.month, today.day)
    return "dormant" if (t >= DORMANT_START or t <= DORMANT_END) else "leafed"


# ── Tree length classes ─────────────────────────────────────────────────────
# The minimum box length (inches) a tree's height requires. Variant field
# grove_tree_length holds one of these as a string; default "20" fits the
# current 1-2 yr inventory. "46" = the 3-5 yr stock (flowering dogwoods,
# jujubes). A tree of class C may ride in any box with length >= C.
LENGTH_CLASSES: tuple[int, ...] = (16, 20, 32, 46)
DEFAULT_LENGTH = 20

# ── Box catalog ─────────────────────────────────────────────────────────────
# capacity: trees per box, by mode. Conservative ends of the observed ranges
# (12x12 fits 50-100 dormant -> 50; 8x8 fits 4-8 leafed -> 4). packaging_usd:
# wholesale box + consumables (biodegradable bag, packing paper, corrugate,
# rubber bands, tape, sticker, care card, thank-you note) — replaces the old
# flat $3.50/tree. tare_lb: empty box + packing material weight.
BOXES: dict[str, dict] = {
    "br16": {
        "length": 16,
        "width": 6,
        "height": 4,
        "capacity": {"dormant": 1},  # single small whip; no leafed use
        "packaging_usd": 3.00,
        "tare_lb": 0.7,
    },
    "s20": {
        "length": 20,
        "width": 8,
        "height": 8,
        "capacity": {"dormant": 15, "leafed": 4},
        "packaging_usd": 4.50,
        "tare_lb": 1.6,
    },
    "s32": {
        "length": 32,
        "width": 8,
        "height": 8,
        "capacity": {"dormant": 15, "leafed": 4},
        "packaging_usd": 5.00,
        "tare_lb": 2.2,
    },
    "s46": {
        "length": 46,
        "width": 8,
        "height": 8,
        "capacity": {"dormant": 15, "leafed": 4},
        "packaging_usd": 5.50,
        "tare_lb": 2.9,
    },
    "b20": {
        "length": 20,
        "width": 12,
        "height": 12,
        "capacity": {"dormant": 50},  # bulk box — dormant only
        "packaging_usd": 6.00,
        "tare_lb": 2.9,
    },
    "b32": {
        "length": 32,
        "width": 12,
        "height": 12,
        "capacity": {"dormant": 50},
        "packaging_usd": 6.50,
        "tare_lb": 4.1,
    },
    # "b46": 46x12x12 deliberately NOT stocked yet — it would carry bulk
    # 3-5 yr stock at ~48 lb DIM. Add here + rates when Josh decides.
}

# USPS Ground Advantage hard mailability limits (GOL-1906). Source: Shippo
# (our broker), "USPS Ground Advantage" service guide — max weight 70 lb, max
# combined length + girth 130" (girth = 2*width + 2*height). A box that violates
# either is not mailable at all, so the catalog must clear both; this fails
# loudly at import if a future box is added over-size.
#
# Note this REPLACES the old UPS additional-handling rule (fired above a 48"
# longest side). USPS has no single-longest-side cutoff; it prices oversize
# through nonstandard SURCHARGES, which are cost tiers priced into the live
# Shippo quote, NOT mailability limits — so they gate cost, not shippability:
#   * length 22"-30"           -> +$4.50   (nonstandard length)
#   * length over 30"          -> +$10.00  (nonstandard length; hits s32/s46/b32)
#   * volume over 2 cu ft      -> +$21.00  (cubic surcharge; hits b32, 4608 cu in)
# Length and shape surcharges do not stack (higher applies); the >2 cu ft
# surcharge stacks on top. These are documented so a new box's cost impact is
# visible; the rate-checker's live probe captures the actual dollar effect.
MAX_SHIP_WEIGHT_LB = 70.0
MAX_LENGTH_PLUS_GIRTH_IN = 130.0

# Back-compat alias: shipping_zones re-exports this and tests pin it. It now
# carries the largest single side any catalog box may have while still clearing
# the 130" length+girth limit at this catalog's cross-sections — an informational
# ceiling, not a USPS rule. The authoritative gate is MAX_LENGTH_PLUS_GIRTH_IN.
MAX_BOX_LONGEST_SIDE_IN = 108.0


def length_plus_girth_in(box: dict) -> float:
    """USPS combined length + girth: longest side + 2*(sum of the other two)."""
    dims = sorted((box["length"], box["width"], box["height"]), reverse=True)
    return dims[0] + 2 * (dims[1] + dims[2])


assert all(length_plus_girth_in(b) <= MAX_LENGTH_PLUS_GIRTH_IN for b in BOXES.values())

# USPS Ground Advantage dimensional-weight rule (GOL-1906). Source: Shippo,
# "USPS Ground Advantage" service guide. Dimensional weight = L*W*H / divisor,
# but ONLY for packages over 1 cubic foot (1,728 cu in); at or below 1 cu ft
# USPS bills on actual scale weight alone. This differs from UPS, which applied
# DIM to every package regardless of size — so br16 (384 cu in) and s20
# (1,280 cu in) now take no dimensional penalty.
#
# The divisor is 139 as of 2026-07-12 (it was 166 before that date). It happens
# to equal the old UPS daily-rates divisor, but the citation and the cubic-foot
# applicability threshold are USPS's, not UPS's — do not conflate them.
DIM_DIVISOR = 139
DIM_APPLIES_ABOVE_CU_IN = 1728  # 1 cubic foot

# Estimated per-tree weight in the box, by mode (root wrap + damp sphagnum;
# leafed adds soil-free rootball moisture + foliage). Open question flagged
# in the vault: weigh real packed boxes in the first season and tune.
PER_TREE_LB = {"dormant": 0.5, "leafed": 2.0}


def dim_weight_lb(box_id: str) -> float:
    """USPS dimensional weight, or 0.0 for boxes at/under 1 cu ft.

    USPS Ground Advantage applies dimensional weight only above 1 cubic foot
    (DIM_APPLIES_ABOVE_CU_IN); smaller boxes bill on actual weight alone. A box
    at or below the threshold returns 0.0 so ``billable_weight_lb`` falls back to
    the actual scale weight.
    """
    b = BOXES[box_id]
    volume = b["length"] * b["width"] * b["height"]
    if volume <= DIM_APPLIES_ABOVE_CU_IN:
        return 0.0
    return round(volume / DIM_DIVISOR, 1)


def actual_weight_lb(box_id: str, count: int, mode: str) -> float:
    """Estimated scale weight of a packed box (what the label declares)."""
    return round(BOXES[box_id]["tare_lb"] + PER_TREE_LB[mode] * max(0, count), 1)


def billable_weight_lb(box_id: str, count: int, mode: str) -> float:
    """What USPS bills: max(actual, DIM) — DIM is 0 at/under 1 cu ft."""
    return max(actual_weight_lb(box_id, count, mode), dim_weight_lb(box_id))


def representative_billable_lb(box_id: str) -> int:
    """Worst typical billable weight across modes at full capacity — the
    weight the rate-checker quotes each box at (never undercharge)."""
    b = BOXES[box_id]
    worst = max(billable_weight_lb(box_id, cap, mode) for mode, cap in b["capacity"].items())
    return math.ceil(worst)


# No catalog box may exceed the 70 lb USPS Ground Advantage ceiling at its
# worst-case fill — fails loudly at import if a future box does (GOL-1906).
assert all(representative_billable_lb(box_id) <= MAX_SHIP_WEIGHT_LB for box_id in BOXES)


def usable_boxes(length_class: int, mode: str) -> list[str]:
    """Box ids that can carry a tree of `length_class` in `mode`."""
    return [box_id for box_id, b in BOXES.items() if b["length"] >= length_class and mode in b["capacity"]]


# ── Packing ─────────────────────────────────────────────────────────────────


class PackedBox:
    """One physical box in a shipment plan."""

    __slots__ = ("box_id", "count")

    def __init__(self, box_id: str, count: int = 0):
        self.box_id = box_id
        self.count = count

    def spare(self, mode: str) -> int:
        return BOXES[self.box_id]["capacity"].get(mode, 0) - self.count

    def __repr__(self):  # pragma: no cover — debugging aid
        return f"PackedBox({self.box_id}, count={self.count})"


def _min_cost_combo(n: int, options: list[tuple[str, int, float]]) -> list[str] | None:
    """Cheapest multiset of boxes covering `n` trees.

    options: (box_id, capacity, cost). Exact DP (covering knapsack); ties
    break toward fewer boxes, then smaller total volume, then box id — fully
    deterministic. Returns list of box_ids or None when options is empty.
    """
    if n <= 0:
        return []
    if not options:
        return None

    def volume(box_id):
        b = BOXES[box_id]
        return b["length"] * b["width"] * b["height"]

    # dp[i] = (cost, n_boxes, total_volume, ids_tuple) best way to cover i trees
    INF = (float("inf"), 0, 0, ())
    dp: list[tuple] = [INF] * (n + 1)
    dp[0] = (0.0, 0, 0, ())
    for i in range(1, n + 1):
        best = INF
        for box_id, cap, cost in options:
            prev = dp[max(0, i - cap)]
            if prev[0] == float("inf"):
                continue
            cand = (
                prev[0] + cost,
                prev[1] + 1,
                prev[2] + volume(box_id),
                tuple(sorted(prev[3] + (box_id,))),
            )
            if cand < best:
                best = cand
        dp[i] = best
    if dp[n][0] == float("inf"):
        return None
    return list(dp[n][3])


def pack_order(items: list[tuple[int, float]], mode: str, cost_of) -> list[PackedBox] | None:
    """Pack (length_class, qty) items into boxes, minimizing total cost.

    ``cost_of(box_id) -> float | None`` supplies the destination-zone rate
    for each box; a box with no configured rate is unusable. Returns the
    packed plan or None when any tree cannot be packed (unknown class, no
    usable/rated box, non-positive catalog data) — fail-safe like the rest
    of the engine: None means "add no shipping line", never guess.

    Tall classes pack first; shorter trees then top up spare capacity in the
    already-opened longer boxes before any new box is considered.
    """
    if mode not in MODES:
        return None
    totals: dict[int, int] = {}
    for length_class, qty in items:
        if length_class not in LENGTH_CLASSES:
            return None
        q = int(qty)
        if q != qty or q < 0:
            return None
        if q:
            totals[length_class] = totals.get(length_class, 0) + q
    if not totals:
        return []

    packed: list[PackedBox] = []
    for cls in sorted(LENGTH_CLASSES, reverse=True):
        n = totals.get(cls, 0)
        if not n:
            continue
        # Top-up: boxes already opened for taller classes have length >= cls.
        for pb in packed:
            take = min(n, pb.spare(mode))
            if take > 0:
                pb.count += take
                n -= take
        if n <= 0:
            continue
        options = []
        for box_id in usable_boxes(cls, mode):
            cost = cost_of(box_id)
            if cost is None:
                continue
            options.append((box_id, BOXES[box_id]["capacity"][mode], float(cost)))
        combo = _min_cost_combo(n, options)
        if combo is None:
            return None
        # Distribute this class's trees into the chosen boxes (largest first
        # so partial fill lands in one box, leaving clean spare for top-up).
        combo.sort(key=lambda bid: BOXES[bid]["capacity"][mode], reverse=True)
        for box_id in combo:
            take = min(n, BOXES[box_id]["capacity"][mode])
            packed.append(PackedBox(box_id, take))
            n -= take
        assert n <= 0
    return packed
