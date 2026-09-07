"""Box catalog + per-box packing engine for bareroot-only shipping (v2).

Replaces the one-tree-one-box model: shipping now prices PER PACKED BOX, not
per tree, because under carrier dimensional billing the box drives the cost —
5 dormant bareroots and 1 dormant bareroot in the same box bill nearly the
same. Design: vault wiki/Software/Grove Shipping (Box Engine v2, 2026-07-31;
recalibrated off UPS-only billing to the USPS/UPS least-cost race, GOL-1906;
catalog descoped to two SKUs by CEO directive 2026-09-07).

**Two-SKU catalog (CEO directive, 2026-09-07).** The bulk 12x12 boxes and the
graduated 8x8 length ladder are retired; the near-term catalog is exactly two
boxes, both 24" long, selected by tree COUNT (contiguous, non-overlapping
ranges) rather than by tree height:

* ``small`` — 24x6x4, holds 1-5 trees.
* ``large`` — 24x9x6, holds 6-10 trees.

Because both boxes share one length (24"), the packer no longer walks a length
ladder for a box tall enough; it pools the whole cart and picks the cheapest
box combination for the total tree count. A tree taller than the box (> 24")
has no box in this catalog and fails safe (no shipping line).

Two packing modes, resolved from the ship date (trees are dormant or leafed
out at the nursery — it is a property of the season, not the product). The mode
does not change how many trees fit the descoped boxes (Josh's 1-5 / 6-10 ranges
are season-independent); it only changes the estimated packed WEIGHT the rate
probe declares (leafed foliage is heavier), via ``PER_TREE_LB``.

Pure Python, no Odoo imports — same testability contract as
``shipping_zones.py`` (see ``tests/test_shipping_boxes.py``).
"""

import math
from datetime import date

# ── Packing modes ───────────────────────────────────────────────────────────
MODES: tuple[str, ...] = ("dormant", "leafed")

# Modes the published rate table is allowed to quote at (GOL-1906, Josh
# 2026-09-07). A "mode" (dormant/leafed) is NOT the same axis as a shippability
# "tier" (bareroot/potted, see shipping_zones.SHIPPABLE_TIERS): both modes are
# bareroot. But a bareroot tree only ever gets a SHIPPING LABEL in its dormant
# window — outside it the same stock resolves to peat-and-bagged (potted-
# equivalent) and is farm-pickup only, so no leafed-weight parcel is ever bought.
# ``representative_billable_lb`` must therefore quote the DORMANT parcel only;
# taking max() across all modes let the heavier leafed weight (PER_TREE_LB 2.0)
# drive the table, inflating small to 11 lb / large to 22 lb against a real
# dormant 7 lb / 14 lb — a systematic OVERCHARGE off a parcel that can't be
# ordered. (Josh phrased this as "capacity keys ∩ SHIPPABLE_TIERS"; because
# modes and tiers are different axes that literal intersection is empty, so this
# constant encodes the intent — the shippable/quotable modes — directly.)
QUOTABLE_MODES: tuple[str, ...] = ("dormant",)

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
# current 1-2 yr inventory. Both catalog boxes are 24" long, so every class here
# fits both boxes — length is now only a fit GATE (a tree over 24" has no box),
# never a box-selection key. The tall 3-5 yr classes (32/46) left the near-term
# catalog with their boxes (CEO directive 2026-09-07); a tree still tagged over
# 24" fails safe at packing until a longer box is restocked.
LENGTH_CLASSES: tuple[int, ...] = (16, 20)
DEFAULT_LENGTH = 20

# ── Box catalog ─────────────────────────────────────────────────────────────
# Two SKUs, selected by tree COUNT (CEO directive 2026-09-07). capacity: trees
# per box, by mode — Josh's 1-5 / 6-10 ranges are season-independent, so both
# modes carry the same count. packaging_usd: wholesale box + consumables
# (biodegradable bag, packing paper, corrugate, rubber bands, tape, sticker,
# care card, thank-you note).
#
# Packed weight is modelled as three explicit terms (Josh bench-measurement,
# 2026-09-07): ``tare_lb`` = the empty CARTON alone; ``paper_lb`` = the void-fill
# packing paper (a real, non-trivial term — a full small box carries ~2.5 lb of
# paper, more than the trees themselves); and ``PER_TREE_LB[mode] * count`` for
# the stock. Keeping paper as its own field rather than burying it in tare makes
# the estimate auditable and each box tunable independently as boxes are weighed.
#   small: carton 2.0 + paper 2.5 + 5*0.5 dormant trees = 7.0 lb (Josh measured
#          the small box full at ~7 lb: 2.5 lb seedlings + 2.0 lb carton + paper).
#   large: carton 3.1 + paper 5.0 + 10*0.5 dormant trees = 13.1 lb -> quoted 14.
#          DERIVED by physical scaling from the small box (surface area for the
#          carton, void volume for the paper), NOT yet measured — Josh to weigh a
#          full large box to confirm; over-quote is the safe side (GOL-1906).
BOXES: dict[str, dict] = {
    "small": {
        "length": 24,
        "width": 6,
        "height": 4,
        "capacity": {"dormant": 5, "leafed": 5},  # holds 1-5 trees
        "packaging_usd": 3.50,
        "tare_lb": 2.0,  # empty carton, measured (Josh 2026-09-07)
        "paper_lb": 2.5,  # void-fill packing paper, measured
    },
    "large": {
        "length": 24,
        "width": 9,
        "height": 6,
        "capacity": {"dormant": 10, "leafed": 10},  # holds 6-10 trees
        "packaging_usd": 4.50,
        "tare_lb": 3.1,  # empty carton, DERIVED (scaled by surface area) — weigh to confirm
        "paper_lb": 5.0,  # void-fill packing paper, DERIVED (scaled by void volume)
    },
}

# USPS Ground Advantage hard mailability limits (GOL-1906). Source: Shippo
# (our broker), "USPS Ground Advantage" service guide — max weight 70 lb, max
# combined length + girth 130" (girth = 2*width + 2*height). A box that violates
# either is not mailable at all, so the catalog must clear both; this fails
# loudly at import if a future box is added over-size. USPS is the binding
# constraint in the least-cost race: it has the tighter combined-size limit, so
# a box that clears USPS also clears UPS Ground's own 165" length+girth ceiling.
# Both descoped boxes clear it with room: small = 24 + 2*(6+4) = 44"; large =
# 24 + 2*(9+6) = 54".
#
# NOTE — nonstandard-LENGTH surcharge applies to BOTH boxes. USPS surcharges any
# parcel over 22" on its longest side; both catalog boxes are 24" long, so EVERY
# shipment carries the nonstandard-length fee (~$5-7/parcel at time of writing).
# That is a COST tier priced into the live Shippo quote, not a mailability limit
# — so it gates cost, not shippability, and the rate-checker's live probe
# captures the actual dollar effect. USPS oversize/cost tiers:
#   * length 22"-30"           -> nonstandard length (hits BOTH boxes)
#   * length over 30"          -> higher nonstandard length (no catalog box)
#   * volume over 2 cu ft      -> cubic surcharge (no catalog box; the largest,
#                                 large at 24x9x6 = 1,296 cu in, is under 2 cu ft)
# Length and shape surcharges do not stack (higher applies); the >2 cu ft
# surcharge would stack on top. Because both boxes cross the 22" line, the rate
# table MUST be regenerated from a live probe that includes the fee — carrying
# forward a pre-descope box's numbers would under-quote every order.
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
# USPS bills on actual scale weight alone. Both descoped boxes are under 1 cu ft
# (small = 576 cu in, large = 1,296 cu in), so NEITHER takes a dimensional
# penalty — they bill on actual scale weight. (This differs from UPS, which
# applied DIM to every package regardless of size.)
#
# In the two-carrier race this value is the DECLARED probe/label weight, i.e.
# the USPS billing floor. It never under-declares for UPS: UPS re-derives its own
# every-package DIM (divisor 139) from the declared box dimensions and floors the
# rate to it, so a small box quotes USPS on actual weight while UPS still quotes
# its higher DIM.
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
    """Estimated scale weight of a packed box (what the label declares).

    Three terms: empty carton (``tare_lb``) + void-fill packing paper
    (``paper_lb``) + stock (``PER_TREE_LB[mode] * count``). Paper is a real,
    measured component — ~2.5 lb in a full small box — not rolled into tare.
    """
    b = BOXES[box_id]
    return round(b["tare_lb"] + b["paper_lb"] + PER_TREE_LB[mode] * max(0, count), 1)


def billable_weight_lb(box_id: str, count: int, mode: str) -> float:
    """What USPS bills: max(actual, DIM) — DIM is 0 at/under 1 cu ft."""
    return max(actual_weight_lb(box_id, count, mode), dim_weight_lb(box_id))


def representative_billable_lb(box_id: str) -> int:
    """Worst typical billable weight at full capacity across the QUOTABLE modes
    — the weight the rate-checker declares for each box (never undercharge).

    Only ``QUOTABLE_MODES`` (dormant) count: a bareroot parcel only ships in its
    dormant window, so the leafed weight prices a parcel that is never bought
    (see ``QUOTABLE_MODES``). Falls back to the box's own modes if a future box
    declares none of the quotable modes, so this never silently returns 0.
    """
    b = BOXES[box_id]
    modes = [m for m in QUOTABLE_MODES if m in b["capacity"]] or list(b["capacity"])
    worst = max(billable_weight_lb(box_id, b["capacity"][mode], mode) for mode in modes)
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


def _min_cost_combo(
    n: int, options: list[tuple[str, int, float]], catalog: dict[str, dict] = BOXES
) -> list[str] | None:
    """Cheapest multiset of boxes covering `n` trees.

    options: (box_id, capacity, cost). Exact DP (covering knapsack); ties
    break toward fewer boxes, then smaller total volume, then box id — fully
    deterministic. Returns list of box_ids or None when options is empty.
    ``catalog`` is the box dict the ids resolve against (BOXES for bareroot,
    POTTED_BOXES for the potted engine) — only used for the volume tie-break.
    """
    if n <= 0:
        return []
    if not options:
        return None

    def volume(box_id):
        b = catalog[box_id]
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
    packed plan or None when the cart cannot be packed (a tree taller than any
    box, no usable/rated box, non-positive catalog data) — fail-safe like the
    rest of the engine: None means "add no shipping line", never guess.

    Selection is a straight cost-optimal search over the TOTAL tree count (CEO
    directive 2026-09-07): both catalog boxes share one length (24"), so there
    is no length ladder to walk. The whole cart pools into one count and the DP
    picks the cheapest box combination — which, for a sane monotone rate table,
    is the small box for 1-5 trees and the large box for 6-10, then the cheapest
    mix above 10. The only role length class still plays is the fit gate: every
    box used must be at least as long as the tallest tree in the cart.
    """
    if mode not in MODES:
        return None
    total = 0
    max_length_class = 0
    for length_class, qty in items:
        q = int(qty)
        if q != qty or q < 0:
            return None
        if q:
            total += q
            max_length_class = max(max_length_class, int(length_class))
    if total == 0:
        return []

    # Boxes that can hold the tallest tree in the cart, are used in this mode,
    # and have a configured rate. If none qualifies (e.g. a tree over 24" with
    # no box that long), _min_cost_combo returns None and we fail safe.
    options = []
    for box_id, b in BOXES.items():
        if b["length"] < max_length_class:
            continue
        if mode not in b["capacity"]:
            continue
        cost = cost_of(box_id)
        if cost is None:
            continue
        options.append((box_id, b["capacity"][mode], float(cost)))

    combo = _min_cost_combo(total, options)
    if combo is None:
        return None

    # Distribute the trees into the chosen boxes (largest capacity first so a
    # partial fill lands in one box, leaving clean spare).
    combo.sort(key=lambda bid: BOXES[bid]["capacity"][mode], reverse=True)
    packed: list[PackedBox] = []
    n = total
    for box_id in combo:
        take = min(n, BOXES[box_id]["capacity"][mode])
        packed.append(PackedBox(box_id, take))
        n -= take
    assert n <= 0
    return packed


# ── Potted / peat-and-bagged box catalog + packing (GOL-2031) ────────────────
# Potted (peat-and-bagged) units pack on a DIFFERENT axis than bareroot: by
# UNIT COUNT and ACTUAL packed weight (soil/rootball moisture dominates; DIM
# does not bite at these volumes), NOT by season dormancy mode or tree length
# class. Kept as its own catalog + packer so the bareroot Box Engine v2 above
# is untouched — a mistagged bareroot can never be priced at potted rates and
# vice versa. This module only knows how to PACK and WEIGH potted boxes; the
# shippability flip (SHIPPABLE_TIERS) and the checkout wiring live in
# shipping_zones and stay gated until go-live (still money-flow / CEO gated).
#
# Geometry is Josh's real bench inventory — the boxes physically in hand and
# measured with leafed-out trees flat-packed (2026-09-06, supersedes the
# 2026-09-05 pre-measurement estimate of 24x6x4 / 24x9x6):
#   p24x10x4  24 x 10 x 4 = 960 in³   -> 1-5 seedlings
#   p24x10x6  24 x 10 x 6 = 1,440 in³ -> 5-10 seedlings
# Both are 24" long (> 22"), so USPS charges its nonstandard-length surcharge on
# every potted label. As with the 32"/46" bareroot boxes, that surcharge is NOT
# modelled here — the rate-checker's live Shippo probe quotes each box at its
# real dimensions and captures the actual dollar effect, so the surcharge lands
# in the per-box zone rate. The only requirement is that these boxes reach the
# probe list with their true 24" length (see scripts/rate_check). Both sit under
# 1 cu ft (1,728 in³), so USPS bills actual scale weight and DIM never bites.
#
# POTTED_UNIT_LB = 2.0 is Josh's FIRMED damp peat-and-bagged per-tree increment
# (weigh-in 2026-09-06): a damp potted/peat-bagged unit weighs ~2 lb, so a 5-pack
# runs ~10 lb + tare and a 10-pack ~20 lb + tare — both far under the 70 lb ceiling
# and (at < 1 cu ft) DIM-irrelevant. This supersedes the earlier 1.3 lb planning
# proxy, which was the LEAFED flat-pack figure (bench reads of leafed trees dry-
# packed as a stand-in) and always ran light for the damp root-mass this catalog
# actually ships. Leafed flat-pack stays ~1.3, but that inventory ships on the
# bareroot Box Engine above (PER_TREE_LB), not here — this potted catalog is
# peat-and-bagged only, so 2.0 is the right calibration. representative_billable
# uses ceil(), so pricing stays on the never-undercharge side. The resulting
# 12 lb / 22 lb representative points are regression-locked in test_shipping_boxes.py
# and feed the GOL-1906 rate re-derive once USPS-GA is live on the Shippo token.
POTTED_UNIT_LB = 2.0  # Josh weigh-in 2026-09-06: firmed damp peat-and-bagged lb/tree

POTTED_BOXES: dict[str, dict] = {
    "p24x10x4": {
        "length": 24,
        "width": 10,
        "height": 4,  # 960 in³ (< 1 cu ft -> no DIM)
        "capacity": 5,  # seedlings — single axis, no season mode
        "packaging_usd": 3.50,
        # tare 1.4 lb = Josh's measured box-only tare (2026-09-06; independent of dry
        # vs damp contents). Damp 5-pack -> 1.4 + 5*2.0 = 11.4 lb; ceil -> 12 lb probe.
        "tare_lb": 1.4,
    },
    "p24x10x6": {
        "length": 24,
        "width": 10,
        "height": 6,  # 1,440 in³ (< 1 cu ft -> no DIM)
        "capacity": 10,
        "packaging_usd": 4.50,
        # tare 1.5 lb = box-only tare back-solved from Josh's leafed reads (independent
        # of contents). Damp full 10-pack -> 1.5 + 10*2.0 = 21.5 lb; ceil -> 22 lb probe.
        "tare_lb": 1.5,
    },
}

# Potted boxes obey the same USPS Ground Advantage envelope gates as bareroot:
# 130" length+girth and the 70 lb ceiling at worst-case fill. Fail loudly at
# import if a future potted box or a re-tuned POTTED_UNIT_LB breaks either.
assert all(length_plus_girth_in(b) <= MAX_LENGTH_PLUS_GIRTH_IN for b in POTTED_BOXES.values())


def potted_dim_weight_lb(box_id: str) -> float:
    """USPS dimensional weight of a potted box, or 0.0 at/under 1 cu ft.

    Same rule as ``dim_weight_lb`` (DIM only above DIM_APPLIES_ABOVE_CU_IN);
    both catalog boxes sit under 1 cu ft, so this is 0.0 today and actual scale
    weight governs — kept explicit so a larger future potted box is handled.
    """
    b = POTTED_BOXES[box_id]
    volume = b["length"] * b["width"] * b["height"]
    if volume <= DIM_APPLIES_ABOVE_CU_IN:
        return 0.0
    return round(volume / DIM_DIVISOR, 1)


def potted_actual_weight_lb(box_id: str, count: int) -> float:
    """Estimated scale weight of a packed potted box (what the label declares)."""
    return round(POTTED_BOXES[box_id]["tare_lb"] + POTTED_UNIT_LB * max(0, count), 1)


def potted_billable_weight_lb(box_id: str, count: int) -> float:
    """What USPS bills: max(actual, DIM) — DIM is 0 at/under 1 cu ft."""
    return max(potted_actual_weight_lb(box_id, count), potted_dim_weight_lb(box_id))


def potted_representative_billable_lb(box_id: str) -> int:
    """Worst-case billable weight at full capacity — the weight the
    rate-checker quotes each potted box at (never undercharge)."""
    return math.ceil(potted_billable_weight_lb(box_id, POTTED_BOXES[box_id]["capacity"]))


assert all(potted_representative_billable_lb(box_id) <= MAX_SHIP_WEIGHT_LB for box_id in POTTED_BOXES)


def pack_potted(count, cost_of) -> list[PackedBox] | None:
    """Cheapest combo of potted boxes for ``count`` seedlings.

    ``cost_of(box_id) -> float | None`` supplies the destination-zone rate; a
    box with no configured rate is unusable. Returns the packed plan, ``[]`` for
    zero units, or ``None`` when the units cannot be packed (negative/non-integer
    count, or no rated potted box) — fail-safe like ``pack_order``: None means
    "add no shipping line", never guess.
    """
    if count is None:
        return None
    n = int(count)
    if n != count or n < 0:
        return None
    if n == 0:
        return []
    options = []
    for box_id, b in POTTED_BOXES.items():
        cost = cost_of(box_id)
        if cost is None:
            continue
        options.append((box_id, b["capacity"], float(cost)))
    combo = _min_cost_combo(n, options, POTTED_BOXES)
    if combo is None:
        return None
    # Distribute into the chosen boxes (largest capacity first so a partial fill
    # lands in one box), mirroring pack_order's deterministic layout.
    combo.sort(key=lambda bid: POTTED_BOXES[bid]["capacity"], reverse=True)
    packed: list[PackedBox] = []
    for box_id in combo:
        take = min(n, POTTED_BOXES[box_id]["capacity"])
        packed.append(PackedBox(box_id, take))
        n -= take
    assert n <= 0
    return packed
