"""Pure mapping rules for plant-fact enrichment (GOL-2383, spec section B).

No Odoo imports — this module is loaded both as a package under the Odoo
runtime (``odoo.addons.grove_headless.services.plant_data``) and, in CI, by
file path from ``tests/test_plant_data.py`` (same pattern as shippo_client).

Everything here is a *proposal*: the mapping never touches the database. It
turns raw USDA / Perenual JSON into ``FactValue`` objects keyed by the real
``product.template`` field names, so the Fetch-button handler (section A wiring,
follow-up PR) can apply the conservative "write only empty fields" policy and
record provenance without re-deriving anything.

The precedence table below encodes the spec's "wins" column verbatim. It is the
single source of truth for *which provider owns a field*, so the async
architecture stays correct: USDA runs synchronously from the button and writes
only the fields it is authoritative-first for; Perenual runs later from the
budgeted job and fills the fields it owns. ``merge()`` combines two already-
mapped ``PlantFacts`` (e.g. in a test or a backfill that has both in hand)
applying the same precedence.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

# ── Value + result containers ──────────────────────────────────────────────


@dataclass
class FactValue:
    """A single proposed field value with its provenance."""

    value: Any
    source: str  # "usda" | "perenual" | "agent" | "human"
    ref: str  # URL or provider id the value was read from


@dataclass
class PlantFacts:
    """Result of a provider lookup.

    ``fields`` maps ``product.template`` field name -> FactValue.
    ``hints`` are chatter-only notes and are NEVER written to a field.
    ``candidates`` are shown when no exact name match was found.
    """

    fields: dict[str, FactValue] = field(default_factory=dict)
    hints: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    # provider id resolved during this lookup (USDA symbol / Perenual species id),
    # so the handler can cache it on the product for the next fetch. None on skip
    # or no-match.
    resolved_id: Any = None


# ── Field precedence (spec section B "Rule" column) ─────────────────────────
# Ordered list of providers allowed to write each field, best first. A field
# absent here is NEVER auto-filled (mature spread, spacing, chill hours,
# pollination, years to fruit).
FIELD_PRECEDENCE: dict[str, tuple[str, ...]] = {
    "grove_zone_min": ("perenual",),
    "grove_zone_max": ("perenual",),
    "grove_sun": ("perenual", "usda"),
    "grove_layer": ("usda",),
    "grove_mature_size": ("usda", "perenual"),
    "grove_soil": ("perenual", "usda"),
    "grove_growth_rate": ("usda", "perenual"),
    "grove_bloom_season": ("usda", "perenual"),
    "grove_harvest_season": ("perenual", "usda"),
    "grove_watering": ("perenual", "usda"),
    "grove_wildlife": ("perenual", "usda"),
}

# Fields a given provider is allowed to propose at all (a provider must never
# emit a field it is not listed for in FIELD_PRECEDENCE).
PROVIDER_FIELDS: dict[str, frozenset[str]] = {
    prov: frozenset(f for f, order in FIELD_PRECEDENCE.items() if prov in order) for prov in ("usda", "perenual")
}


# ── Name resolution ─────────────────────────────────────────────────────────

_QUOTED = re.compile(r"['\"‘’“”].*?['\"‘’“”]")
_HTML = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
# Hybrid / indeterminate markers: no auto-match is attempted.
_SKIP_TOKENS = {"spp.", "sp.", "hybrid", "x", "×"}


def resolve_binomial(botanical_name: str | None) -> tuple[str | None, str | None]:
    """Return ``(binomial, skip_reason)``.

    The binomial is the first two tokens of the name, lower-cased, with cultivar
    quotes and trailing authors dropped. When the name carries a hybrid or
    indeterminate marker (``spp.``, ``hybrid``, ``x`` / ``×``) no auto-match
    is attempted and ``(None, reason)`` is returned so the caller can say so in
    chatter.
    """
    if not botanical_name or not botanical_name.strip():
        return None, "no botanical name set"
    cleaned = _QUOTED.sub(" ", botanical_name)  # drop 'Cultivar' quotes
    cleaned = _WS.sub(" ", cleaned).strip().lower()
    tokens = cleaned.split(" ")
    marker = next((t for t in tokens if t in _SKIP_TOKENS), None)
    if marker:
        return None, f"name contains '{marker}' (hybrid/indeterminate) — set the id by hand"
    if len(tokens) < 2:
        return None, "botanical name is not a binomial (need genus and species)"
    return f"{tokens[0]} {tokens[1]}", None


def _norm_scientific(name: str | None) -> str:
    """Strip HTML italics and collapse whitespace, lower-cased."""
    if not name:
        return ""
    return _WS.sub(" ", _HTML.sub("", name)).strip().lower()


def usda_pick_exact(results: list[dict], binomial: str) -> dict | None:
    """Pick the species-rank result whose scientific name is the binomial.

    Live USDA search returns ``ScientificNameWithoutAuthor`` as null, so the
    binomial is derived from ``ScientificName`` (tags + author stripped).
    Infraspecific taxa (var./subsp.) share the same first two tokens, so a match
    must additionally be Rank == "Species".
    """
    for entry in results:
        plant = entry.get("Plant", entry)
        nwa = plant.get("ScientificNameWithoutAuthor")
        norm = _norm_scientific(nwa) if nwa else _norm_scientific(plant.get("ScientificName"))
        first_two = " ".join(norm.split(" ")[:2])
        rank = (plant.get("Rank") or "").lower()
        if first_two == binomial and rank in ("species", ""):
            # reject infraspecific names that happen to start with the binomial
            if "var." in norm or "subsp." in norm or "ssp." in norm:
                continue
            return plant
    return None


def usda_candidates(results: list[dict], limit: int = 5) -> list[str]:
    out = []
    for entry in results[:limit]:
        plant = entry.get("Plant", entry)
        out.append(f"{plant.get('Symbol')} — {_norm_scientific(plant.get('ScientificName'))}")
    return out


# ── Shared helpers ──────────────────────────────────────────────────────────


def _fmt_feet(raw: str | float | None) -> str | None:
    """'55.0' -> '55'; '6.5' -> '6.5'."""
    if raw in (None, ""):
        return None
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    return str(int(n)) if n == int(n) else str(n)


def temp_to_zone(temp_f: float) -> str:
    """USDA hardiness zone for an average annual extreme minimum temperature.

    Each zone-half spans 5 °F; zone 1a starts at -60 °F. -21 °F -> 4b.
    """
    half = int(math.floor((temp_f + 60) / 5))
    half = max(half, 0)
    zone_num = 1 + half // 2
    letter = "a" if half % 2 == 0 else "b"
    return f"{zone_num}{letter}"


# ── USDA characteristic mapping ─────────────────────────────────────────────

_USDA_SHADE_SUN = {"low": "full", "medium": "partial", "high": "partial", "intolerant": "full"}
_USDA_GROWTH = {"slow": "slow", "moderate": "moderate", "rapid": "fast"}
_USDA_MOISTURE = {"low": "low", "medium": "moderate", "high": "high"}
_WILDLIFE_GROUPS = {
    "LargeMammals": "large mammals",
    "SmallMammals": "small mammals",
    "WaterBirds": "water birds",
    "TerrestrialBirds": "terrestrial birds",
    "Palatable": "browsers",
}


def _chars_index(characteristics: list[dict]) -> dict[str, str]:
    return {
        c.get("PlantCharacteristicName"): c.get("PlantCharacteristicValue")
        for c in (characteristics or [])
        if c.get("PlantCharacteristicName")
    }


def map_usda(profile: dict, characteristics: list[dict], wildlife: dict, ref: str) -> PlantFacts:
    """Map raw USDA PLANTS payloads to a PlantFacts (fields it is allowed to own)."""
    facts = PlantFacts()
    ch = _chars_index(characteristics)

    def put(name: str, value: Any):
        if value not in (None, "") and name in PROVIDER_FIELDS["usda"]:
            facts.fields[name] = FactValue(value=value, source="usda", ref=ref)

    # sun — USDA never yields "shade"
    shade = (ch.get("Shade Tolerance") or "").strip().lower()
    put("grove_sun", _USDA_SHADE_SUN.get(shade))

    # layer — growth habit + mature height
    habits = [h.lower() for h in (profile.get("GrowthHabits") or [])]
    height = None
    try:
        height = float(ch["Height, Mature (feet)"]) if ch.get("Height, Mature (feet)") else None
    except (TypeError, ValueError):
        height = None
    layer = None
    if any("tree" in h for h in habits):
        layer = "canopy" if (height is not None and height >= 40) else "understory"
    elif any("shrub" in h for h in habits):
        layer = "shrub"
    elif any("vine" in h for h in habits):
        layer = "vine"
    elif any(h in ("forb/herb", "forb", "herb", "graminoid") or "forb" in h or "graminoid" in h for h in habits):
        layer = "ground"
    put("grove_layer", layer)

    # mature size — "up to N ft"
    fh = _fmt_feet(ch.get("Height, Mature (feet)"))
    put("grove_mature_size", f"up to {fh} ft" if fh else None)

    # soil — texture adaptations + pH range
    textures = [
        label
        for key, label in (
            ("Adapted to Coarse Textured Soils", "coarse"),
            ("Adapted to Medium Textured Soils", "medium"),
            ("Adapted to Fine Textured Soils", "fine"),
        )
        if (ch.get(key) or "").strip().lower() == "yes"
    ]
    soil_parts = []
    if textures:
        soil_parts.append(_join_and(textures).capitalize() + " textures")
    ph_min, ph_max = ch.get("pH, Minimum"), ch.get("pH, Maximum")
    if ph_min and ph_max:
        soil_parts.append(f"pH {ph_min}–{ph_max}")
    put("grove_soil", "; ".join(soil_parts) if soil_parts else None)

    # growth rate
    put("grove_growth_rate", _USDA_GROWTH.get((ch.get("Growth Rate") or "").strip().lower()))

    # bloom season
    put("grove_bloom_season", (ch.get("Bloom Period") or "").strip() or None)

    # harvest season — Fruit/Seed Period Begin–End
    begin = (ch.get("Fruit/Seed Period Begin") or "").strip()
    end = (ch.get("Fruit/Seed Period End") or "").strip()
    if begin and end:
        put("grove_harvest_season", begin if begin == end else f"{begin}–{end}")
    elif begin or end:
        put("grove_harvest_season", begin or end)

    # watering
    put("grove_watering", _USDA_MOISTURE.get((ch.get("Moisture Use") or "").strip().lower()))

    # wildlife — animal groups rated >= Medium in Food/Cover
    put("grove_wildlife", _usda_wildlife(wildlife))

    # hints (chatter-only): zone from min temperature, spacing from density
    min_temp = ch.get("Temperature, Minimum (°F)")
    if min_temp not in (None, ""):
        try:
            t = float(min_temp)
            facts.hints.append(
                f"USDA minimum temperature {int(t) if t == int(t) else t} °F "
                f"≈ zone {temp_to_zone(t)} (zones written from Perenual only)"
            )
        except (TypeError, ValueError):
            pass
    d_min, d_max = ch.get("Planting Density per Acre, Minimum"), ch.get("Planting Density per Acre, Maximum")
    if d_min and d_max:
        facts.hints.append(f"USDA planting density {d_min}–{d_max}/acre (forestry spacing; set spacing by hand)")

    return facts


def _usda_wildlife(wildlife: dict) -> str | None:
    if not wildlife:
        return None
    rated = set()
    for section in ("Food", "Cover"):
        for row in wildlife.get(section, []) or []:
            for key, label in _WILDLIFE_GROUPS.items():
                if (row.get(key) or "").strip().lower() in ("medium", "high"):
                    rated.add(label)
    if not rated:
        return None
    return "Attracts " + _join_and(sorted(rated))


# ── Perenual mapping ────────────────────────────────────────────────────────

_PERENUAL_WATERING = {"minimum": "low", "average": "moderate", "frequent": "high"}
_PERENUAL_GROWTH = {"low": "slow", "moderate": "moderate", "high": "fast"}


def perenual_pick_exact(results: list[dict], binomial: str) -> dict | None:
    for entry in results:
        names = entry.get("scientific_name") or []
        for n in names:
            if " ".join(_norm_scientific(n).split(" ")[:2]) == binomial:
                return entry
    return None


def perenual_candidates(results: list[dict], limit: int = 5) -> list[str]:
    out = []
    for entry in results[:limit]:
        names = entry.get("scientific_name") or []
        out.append(f"{entry.get('id')} — {names[0] if names else entry.get('common_name')}")
    return out


def map_perenual(details: dict, ref: str) -> PlantFacts:
    facts = PlantFacts()

    def put(name: str, value: Any):
        if value not in (None, "") and name in PROVIDER_FIELDS["perenual"]:
            facts.fields[name] = FactValue(value=value, source="perenual", ref=ref)

    # zones — Perenual is the only source
    hardiness = details.get("hardiness") or {}
    zmin, zmax = _to_int(hardiness.get("min")), _to_int(hardiness.get("max"))
    put("grove_zone_min", zmin)
    put("grove_zone_max", zmax)

    # sun
    put("grove_sun", _perenual_sun(details.get("sunlight")))

    # soil
    soil = [s for s in (details.get("soil") or []) if s]
    put("grove_soil", _join_and([s.lower() for s in soil]).capitalize() if soil else None)

    # growth rate
    put("grove_growth_rate", _PERENUAL_GROWTH.get((details.get("growth_rate") or "").strip().lower()))

    # bloom / harvest
    put("grove_bloom_season", (details.get("flowering_season") or "").strip() or None)
    put("grove_harvest_season", (details.get("harvest_season") or "").strip() or None)

    # watering
    put("grove_watering", _PERENUAL_WATERING.get((details.get("watering") or "").strip().lower()))

    # wildlife — attracts list
    attracts = [a for a in (details.get("attracts") or []) if a]
    put("grove_wildlife", "Attracts " + _join_and([a.lower() for a in attracts]) if attracts else None)

    # mature size — Perenual dimensions (USDA wins; used only as a fallback)
    put("grove_mature_size", _perenual_dimensions(details.get("dimensions")))

    return facts


def _perenual_sun(sunlight) -> str | None:
    if not sunlight:
        return None
    vals = [s.strip().lower() for s in sunlight if s]
    if not vals:
        return None
    if any("part" in v for v in vals):
        return "partial"
    if all("full" in v for v in vals):
        return "full"
    if all("shade" in v for v in vals):
        return "shade"
    return "partial"


def _perenual_dimensions(dimensions) -> str | None:
    """Perenual v2 returns a list of {type, min_value, max_value, unit}."""
    if not dimensions:
        return None
    rows = dimensions if isinstance(dimensions, list) else [dimensions]
    for row in rows:
        if (row.get("type") or "").lower() in ("height", "spread and height", ""):
            lo, hi, unit = row.get("min_value"), row.get("max_value"), row.get("unit") or "ft"
            if lo and hi:
                return f"{_fmt_feet(lo)}–{_fmt_feet(hi)} {unit}"
            if lo or hi:
                return f"up to {_fmt_feet(hi or lo)} {unit}"
    return None


# ── Merge (both providers in hand) ──────────────────────────────────────────


def merge(*sources: PlantFacts) -> PlantFacts:
    """Combine mapped PlantFacts by FIELD_PRECEDENCE (best provider wins).

    ``sources`` are given in no particular order; precedence is decided per
    field by FIELD_PRECEDENCE, not by argument order.
    """
    by_source: dict[str, PlantFacts] = {}
    merged = PlantFacts()
    for s in sources:
        merged.hints.extend(s.hints)
        merged.candidates.extend(s.candidates)
        for fv in s.fields.values():
            by_source[fv.source] = by_source.get(fv.source) or PlantFacts()
        for name, fv in s.fields.items():
            by_source.setdefault(fv.source, PlantFacts()).fields[name] = fv
    for name, order in FIELD_PRECEDENCE.items():
        for prov in order:
            fv = by_source.get(prov, PlantFacts()).fields.get(name)
            if fv is not None:
                merged.fields[name] = fv
                break
    return merged


# ── Budget helpers (pure; the Odoo enrich job wraps these) ───────────────────

DEFAULT_DAILY_BUDGET = 100


def counter_key(utc_date: str) -> str:
    """ir.config_parameter key for the per-UTC-day Perenual call counter."""
    return f"grove_headless.perenual_calls.{utc_date}"


def calls_needed(has_cached_id: bool) -> int:
    """2 Perenual HTTP calls per job (species-list + details); 1 if id cached."""
    return 1 if has_cached_id else 2


def under_budget(used: int, needed: int, budget: int) -> bool:
    """True when ``needed`` more calls still fit under ``budget``."""
    return used + needed <= budget


# ── tiny text helper ────────────────────────────────────────────────────────


def _join_and(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
