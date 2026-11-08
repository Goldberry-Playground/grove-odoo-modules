#!/usr/bin/env python3
"""One-time parser that fills the planner facts from the existing prose facts.

Context (GOL-3378, order-builder spec 2026-10-14 section A): the order-builder
planner scores on a typed subset of the growing facts — years to first crop,
pollination partners, harvest months, site tolerances and a wildlife rating —
that the display Char fields (grove_years_to_fruit, grove_pollination,
grove_harvest_season, grove_soil, grove_watering, grove_sun, grove_wildlife) hold
only as prose. This script walks the real plant templates and derives the typed
fields from that prose with the small rule-set below, then writes them through
the normal ``write()`` path (so chatter shows the change) and clears the
``grove_facts_reviewed`` sign-off on every template it touches — the existing
"machine writes clear the sign-off" rule, so the review pass (spec section E)
catches them.

Same shape as ``backfill_listing_content.py``: XML-RPC, ``DRY_RUN=1`` by default,
QA first. In dry-run it prints a before/after table for Josh to confirm; only
``DRY_RUN=0`` actually writes.

The parse rules (spec section A) — each is a pure function below so the test
suite can unit-case it:

- ``grove_years_to_fruit_min``  = first integer in ``grove_years_to_fruit``.
- ``grove_pollination_min``     = 3 on three / 3+; 2 on partner / second / two /
  2+ / "2 or more" / "at least two" / cross / self-incompatible; 1 on a bare
  self-fertile / self-pollinating / parthenocarpic claim. An explicit multi-tree
  requirement wins over a self-* mention — "can self-pollinate, but plant 2+" is
  2, not 1 (GOL-3836 defect 2).
- ``grove_harvest_months``      explicit month numbers win; then named months and
  ranges ("August to September" = 8,9; "September-October" = 9,10; GOL-3836
  defect 3); then a small season table (Summer = 7,8; Summer–Fall = 8,9,10;
  Fall = 9,10,11; Late fall = 10,11,12; Spring = 4,5,6).
- ``grove_tolerates_wet``       true if ``grove_soil`` names wet / moist /
  bottomland / flood in a clause with no leading negation ("will not tolerate
  wet", "avoid wet", "intolerant of wet" do NOT count; GOL-3836 defect 1), or
  ``grove_watering == "high"``.
- ``grove_tolerates_poor_soil`` true if ``grove_soil`` names rocky / clay / thin /
  poor / sandy / drought, again un-negated.
- ``grove_tolerates_shade``     from ``grove_sun`` (partial / shade). The model
  computes this too; the script sets it only to make the backfill explicit.
- ``grove_wildlife_value``      high if ``grove_wildlife`` mentions deer or
  turkey; medium on birds or squirrels; else unset.

A field is written only when its parse yields a *set* value (integer > 0,
non-blank month list, a True tolerance, a non-blank wildlife rating) AND that
value differs from what the template already holds AND the field is not held by a
human (a non-machine ``grove_facts_provenance`` source — the GOL-2543 rule). So a
re-run over the hand-set prod facts writes nothing; a template with nothing new
is left untouched and keeps its sign-off.

Scope: every gated planner plant (``grove_layer`` set, ``type == consu``, not
gate-exempt), not just the ``categ_plants`` tree, so the bareroot fruit trees in
Apple / Plum / Fig / Mulberry / Serviceberry / Goods are included (GOL-3836
defect 4).

Sign-off: a write clears ``grove_facts_reviewed`` on that template, which drops
``grove_listing_complete`` and ``grove_planner_ready`` to False until a human
re-reviews — the planner does not go live the instant this runs (GOL-3836 6).

Usage
-----
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com ODOO_DB=odoo \\
    ODOO_USER=<login> ODOO_PASSWORD=<key> \\
    DRY_RUN=1 python3 scripts/backfill_planner_facts.py   # plan only (default)
    # DRY_RUN=0 -> actually writes.

QA first; prod runs only with Josh's explicit go after he confirms the table.
Exit codes: 0 ok, 1 auth or setup failure.
"""

from __future__ import annotations

import os
import re
import sys
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
# DRY_RUN defaults to ON: only DRY_RUN=0 actually writes.
DRY_RUN = os.getenv("DRY_RUN", "1") != "0"

_HISTORICAL_MARKER = "[in-store historical]"

# Source prose fields read per template, plus the planner fields read for the
# before/after table and grove_facts_provenance for the human-value guard.
_SOURCE_FIELDS = [
    "name",
    "grove_years_to_fruit",
    "grove_pollination",
    "grove_harvest_season",
    "grove_soil",
    "grove_watering",
    "grove_sun",
    "grove_wildlife",
    "grove_facts_provenance",
]
_PLANNER_FIELDS = [
    "grove_years_to_fruit_min",
    "grove_pollination_min",
    "grove_harvest_months",
    "grove_tolerates_wet",
    "grove_tolerates_poor_soil",
    "grove_tolerates_shade",
    "grove_wildlife_value",
]

_WET_WORDS = ("wet", "moist", "bottomland", "flood")
_POOR_WORDS = ("rocky", "clay", "thin", "poor", "sandy", "drought")

# Scope: every gated planner plant, keyed on grove_layer rather than the
# categ_plants tree so the bareroot fruit trees in Apple / Plum / Fig / Mulberry /
# Serviceberry / Goods are included (GOL-3836 defect 4).
_PLANT_DOMAIN = [
    ("grove_layer", "!=", False),
    ("type", "=", "consu"),
    ("grove_gate_exempt", "=", False),
    ("name", "not ilike", _HISTORICAL_MARKER),
]

# A negation cue anywhere in the clause *before* a soil keyword flips the match
# off — "will not tolerate wet feet", "avoid wet sites", "intolerant of wet or
# heavy clay soils" must not read as tolerates_wet / tolerates_poor_soil
# (GOL-3836 defect 1). Clauses are split on sentence/clause punctuation so a
# negation in one clause never reaches a keyword in the next.
_NEGATION = re.compile(r"\b(?:not|no|never|avoid\w*|intolerant|won't|cannot|can't|dislikes?|without)\b")
_CLAUSE_BREAKS = (".", ";", ":", "\n", "!", "?")

# Month name / abbreviation -> number, for harvest prose that names months
# ("August to September", "September-October") instead of a season word
# (GOL-3836 defect 3). Longest keys first so "sept" wins over "sep".
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sept": 9,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH_TOKEN = re.compile(r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b")
# A range joiner between two month tokens ("August to October", "Sept-Nov")
# expands to the inclusive span; a list separator ("and", comma, "&") keeps the
# months as named points.
_RANGE_JOINER = re.compile(r"\b(?:to|through|thru|until|till)\b|[-–—]")

# Provenance sources this backfill is allowed to seed over. A field already
# carrying a human (or any non-machine) provenance stamp is protected — the
# GOL-2543 "never clobber a human" rule — so a re-run over the hand-set prod
# facts changes nothing (GOL-3836 defect 6).
_MACHINE_SOURCES = ("usda", "perenual", "agent", "backfill")


# ── Pure parse rules (unit-tested in test_planner_facts.py) ──────────────────
def parse_years_to_fruit_min(text: str | None) -> int:
    """First integer in the Years-to-Fruit prose; 0 when there is none."""
    match = re.search(r"\d+", text or "")
    return int(match.group()) if match else 0


_POLL_THREE = re.compile(r"\bthree\b|\b3\s*\+|\b3\s+or\s+more|at\s+least\s+three")
_POLL_PARTNER = re.compile(
    r"\bpartner"
    r"|\bsecond\b"
    r"|\btwo\b"
    r"|\b2\s*\+"
    r"|\b2\s+or\s+more"
    r"|\b2\s+(?:trees?|varieties|cultivars|plants|others)\b"
    r"|plant\s+2\b"
    r"|at\s+least\s+(?:two|2)"
    r"|\bcross"
    r"|self[\s-]?incompatible"
    r"|another\s+(?:tree|variety|cultivar|pollinizer|pollinator)"
)
_POLL_SELF = re.compile(r"self[\s-]?fertile|self[\s-]?pollinat|parthenocarp")
# A negation right before a partner cue ("no partner needed") turns it off. Kept
# to a short lookback so a far-off "not" on a self-clause ("self-pollination has
# not produced seed, so plant 2+") does not reach the partner cue.
_POLL_NEGATION = re.compile(r"\b(?:no|not|never|without|won't|cannot|can't)\b")
_POLL_NEG_WINDOW = 18


def _has_unnegated(pattern: re.Pattern, t: str) -> bool:
    """True when *pattern* matches *t* with no negation in the short run before it."""
    for m in pattern.finditer(t):
        if not _POLL_NEGATION.search(t[max(0, m.start() - _POLL_NEG_WINDOW) : m.start()]):
            return True
    return False


def parse_pollination_min(text: str | None) -> int:
    """Pollination-partner floor from the Pollination prose; 0 = unset.

    An explicit multi-tree / partner requirement wins over any self-* mention
    (GOL-3836 defect 2): "self-pollination has not produced viable seed, so plant
    2+" and "can self-pollinate, but plant 2+ trees" both need a partner (2), and
    "self-incompatible" is not self-fertile. Order: the specific "three / 3+" (3)
    before the "partner / second / two / 2+ / 2 or more / at least two / cross"
    group (2); a bare self-fertile / self-pollinating / parthenocarpic claim with
    no (un-negated) partner language is 1. A partner cue immediately negated
    ("no partner needed") does not count, so a self-fertile tree stays 1.
    """
    t = (text or "").lower()
    if not t.strip():
        return 0
    if _POLL_THREE.search(t):
        return 3
    if _has_unnegated(_POLL_PARTNER, t):
        return 2
    if _POLL_SELF.search(t):
        return 1
    return 0


def _dedup(nums: list[int]) -> str:
    """Comma list of month numbers in first-seen order, deduped."""
    ordered: list[int] = []
    for n in nums:
        if 1 <= n <= 12 and n not in ordered:
            ordered.append(n)
    return ",".join(str(n) for n in ordered)


def _range_months(a: int, b: int) -> list[int]:
    """Inclusive month range a..b, wrapping across the year end if a > b."""
    if a <= b:
        return list(range(a, b + 1))
    return list(range(a, 13)) + list(range(1, b + 1))


def _month_names_to_months(text: str) -> list[int]:
    """Month numbers from named months and ranges ("August to September" -> 8,9).

    Two month tokens joined by a range word / dash expand to the inclusive span;
    otherwise each named month contributes itself, in order of appearance.
    """
    tokens = [(m.start(), m.end(), _MONTHS[m.group(1)]) for m in _MONTH_TOKEN.finditer(text.lower())]
    months: list[int] = []
    prev_end = prev_num = None
    for start, end, num in tokens:
        if prev_num is not None and _RANGE_JOINER.search(text[prev_end:start].lower()):
            months.extend(_range_months(prev_num, num))
        else:
            months.append(num)
        prev_end, prev_num = end, num
    return months


def parse_harvest_months(text: str | None) -> str:
    """Comma list of month numbers from the Harvest-Season prose; "" = unset.

    Precedence (GOL-3836 defect 3): explicit month numbers already in the text
    win; then named months / ranges ("August to September", "September-October");
    then a small season→months table. Named months must beat the coarse table so
    "(June to early July)" reads as 6,7 (not Summer's 7,8).
    """
    text = (text or "").strip()
    if not text:
        return ""
    nums = [int(n) for n in re.findall(r"\d+", text)]
    nums = [n for n in nums if 1 <= n <= 12]
    if nums:
        return _dedup(nums)
    named = _month_names_to_months(text)
    if named:
        return _dedup(named)
    norm = text.lower().replace("–", "-").replace("—", "-")
    if "summer" in norm and "fall" in norm:
        return "8,9,10"
    if "late fall" in norm:
        return "10,11,12"
    if "summer" in norm:
        return "7,8"
    if "fall" in norm:
        return "9,10,11"
    if "spring" in norm:
        return "4,5,6"
    return ""


def _mentions_unnegated(text: str | None, words: tuple[str, ...]) -> bool:
    """True if *text* names one of *words* in a clause with no leading negation.

    Each keyword hit is checked against the run of text from the previous clause
    break up to the keyword; a negation cue in that window ("will not", "avoid",
    "intolerant") reads the mention as the plant *not* tolerating it, so it does
    not count (GOL-3836 defect 1).
    """
    t = (text or "").lower()
    for word in words:
        for m in re.finditer(re.escape(word), t):
            clause_start = max((t.rfind(b, 0, m.start()) for b in _CLAUSE_BREAKS), default=-1)
            if not _NEGATION.search(t[clause_start + 1 : m.start()]):
                return True
    return False


def parse_tolerates_wet(soil: str | None, watering: str | None) -> bool:
    """Suited to a wet spot: soil prose names a wet word (un-negated), or high water."""
    if _mentions_unnegated(soil, _WET_WORDS):
        return True
    return (watering or "").lower() == "high"


def parse_tolerates_poor_soil(soil: str | None) -> bool:
    """Suited to rough ground: soil prose names a poor-soil word (un-negated)."""
    return _mentions_unnegated(soil, _POOR_WORDS)


def parse_tolerates_shade(sun: str | None) -> bool:
    """Suited to shade: sun requirement is partial or shade."""
    return (sun or "") in ("partial", "shade")


def parse_wildlife_value(text: str | None) -> str | None:
    """Wildlife rating from the Wildlife prose; None = unset.

    High on deer/turkey (the deer track's headline species), medium on
    birds/squirrels, else unset.
    """
    t = (text or "").lower()
    if re.search(r"deer|turkey", t):
        return "high"
    if re.search(r"bird|squirrel", t):
        return "medium"
    return None


def parse_planner_facts(rec: dict) -> dict:
    """Build the write-vals of *set* planner values parsed from one template row.

    Only fields whose parse yields a set value are included — an integer > 0, a
    non-blank month list, a True tolerance, a non-blank wildlife rating — so a
    template with nothing to add is left untouched (and keeps its sign-off).
    """
    vals: dict = {}
    years = parse_years_to_fruit_min(rec.get("grove_years_to_fruit"))
    if years > 0:
        vals["grove_years_to_fruit_min"] = years
    pollination = parse_pollination_min(rec.get("grove_pollination"))
    if pollination > 0:
        vals["grove_pollination_min"] = pollination
    months = parse_harvest_months(rec.get("grove_harvest_season"))
    if months:
        vals["grove_harvest_months"] = months
    if parse_tolerates_wet(rec.get("grove_soil"), rec.get("grove_watering")):
        vals["grove_tolerates_wet"] = True
    if parse_tolerates_poor_soil(rec.get("grove_soil")):
        vals["grove_tolerates_poor_soil"] = True
    if parse_tolerates_shade(rec.get("grove_sun")):
        vals["grove_tolerates_shade"] = True
    wildlife = parse_wildlife_value(rec.get("grove_wildlife"))
    if wildlife:
        vals["grove_wildlife_value"] = wildlife
    return vals


def _is_human_held(provenance: dict | None, field: str) -> bool:
    """True when *field* carries a non-machine (human / import / seed) provenance.

    Such a value is protected — the GOL-2543 "never clobber a human" rule — so the
    backfill leaves it alone even when its own parse would set something.
    """
    meta = (provenance or {}).get(field)
    source = meta.get("source") if isinstance(meta, dict) else None
    return bool(source) and source not in _MACHINE_SOURCES


def writable_facts(rec: dict, parsed: dict) -> dict:
    """Parsed vals minus fields a human holds or that already equal the parse.

    So a re-run over the hand-set prod facts (GOL-3382, done 2026-11-08) writes
    nothing: each scoreable field either already holds the parsed value or carries
    a human provenance stamp (GOL-3836 defect 6).
    """
    provenance = rec.get("grove_facts_provenance") or {}
    out: dict = {}
    for name, new in parsed.items():
        if _is_human_held(provenance, name):
            continue
        old = rec.get(name)
        if name == "grove_harvest_months":
            unchanged = (old or "") == new
        elif name in ("grove_tolerates_wet", "grove_tolerates_poor_soil", "grove_tolerates_shade"):
            unchanged = bool(old) == bool(new)
        elif name == "grove_wildlife_value":
            unchanged = (old or None) == new
        else:  # integer floors
            unchanged = (old or 0) == new
        if not unchanged:
            out[name] = new
    return out


# ── XML-RPC orchestration (not exercised by the unit tests) ──────────────────
def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def authenticate() -> tuple[xmlrpc.client.ServerProxy, int]:
    if not ODOO_PASSWORD:
        fail("ODOO_PASSWORD is required")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        fail(f"authentication failed for {ODOO_USER} on {ODOO_URL} db={ODOO_DB}")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    print(f"Authenticated as uid={uid} on db={ODOO_DB} ({ODOO_URL})")
    return models, uid


def call(models, uid, model, method, args, kwargs=None):
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def real_plant_templates(models, uid) -> list[dict]:
    """Gated plant templates minus historical rows, ordered by name.

    Scoped on the planner-layer field rather than the plants category tree
    (GOL-3836 defect 4): the twelve bareroot fruit trees live in the Apple / Plum
    / Fig / Mulberry / Serviceberry / Goods categories, none of them children of
    ``grove_headless.categ_plants``, so a ``categ_id child_of`` scope silently
    skipped them. Every gated planner plant carries ``grove_layer`` (canopy /
    understory / shrub / ground / vine / fungal), so that is the real set.
    """
    return call(
        models,
        uid,
        "product.template",
        "search_read",
        [_PLANT_DOMAIN],
        {"fields": _SOURCE_FIELDS + _PLANNER_FIELDS, "order": "name"},
    )


def _before_after_rows(rec: dict, parsed: dict) -> list[str]:
    """One table line per field the parse would change (old -> new)."""
    rows = []
    for name, new in parsed.items():
        old = rec.get(name)
        old_disp = "" if old in (False, None, 0, "") else old
        rows.append(f"      {name}: {old_disp!r} -> {new!r}")
    return rows


def main() -> None:
    models, uid = authenticate()
    templates = real_plant_templates(models, uid)
    print(f"\n── Backfill planner facts ── {len(templates)} real plant template(s)")
    print(
        "NOTE: a write here clears grove_facts_reviewed on that template, which drops "
        "grove_listing_complete (and so grove_planner_ready) to False until a human "
        "re-reviews it — the planner does NOT go live right after this run (GOL-3836 defect 6)."
    )
    if DRY_RUN:
        print("DRY_RUN=1 (default): planning only, no writes. Set DRY_RUN=0 to apply.\n")

    written = 0
    for rec in templates:
        name, tid = rec["name"], rec["id"]
        parsed = writable_facts(rec, parse_planner_facts(rec))
        if not parsed:
            print(f"  · {name} (id={tid}): nothing to change")
            continue
        print(f"  ~ {name} (id={tid}): {len(parsed)} field(s)")
        for line in _before_after_rows(rec, parsed):
            print(line)
        if DRY_RUN:
            continue
        # Clear the human sign-off alongside the parsed values (machine write).
        call(models, uid, "product.template", "write", [[tid], dict(parsed, grove_facts_reviewed=False)])
        written += 1

    if DRY_RUN:
        print(
            f"\nDone (dry-run). {len(templates)} template(s) inspected; 0 written. Confirm the table, then DRY_RUN=0."
        )
    else:
        print(f"\nDone. {written} template(s) written; grove_facts_reviewed cleared on each. Run the review pass next.")


if __name__ == "__main__":
    main()
