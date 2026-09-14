"""Per-state effective-component substitution for phantom-BOM bundles (GOL-2237).

Bundles (Kit / phantom-BOM products, e.g. the Remembrance Grove five-native
collection) ship to EVERY green-list state — GOL-2132 exempts them from the
per-product compliance block gate. This module is how they keep that promise:
for a destination that restricts one of the bundle's default components, we ship
a compliant *substitute* component instead of blocking the sale.

Single source of truth: WHICH components must be swapped is derived from
``plant_compliance`` — the same carve-out map the checkout blocks standalone
lines with — so a bundle can never substitute for a taxon that isn't actually
restricted, and can never silently fail to substitute one that is. This module
only owns the SUBSTITUTES: what we ship in place of each restricted genus.

Worked example (Remembrance Grove default = five natives incl. Castanea
[chestnut] + Prunus americana [American plum]):

  * FL     — Castanea blocked -> Shagbark Hickory; Prunus clear -> plum kept.
  * WA/OR  — Castanea blocked -> Shagbark Hickory; Prunus blocked -> Jujube.
  * everywhere else — the default five natives, no substitution at all.

Jujube (Ziziphus jujuba) is NOT North-American native, so a state that receives
it can no longer be sold the "five natives" line — ``effective_composition``
reports ``all_native`` so the storefront can soften that copy (GOL-2237 d4).

Pure Python, no Odoo imports (mirrors plant_compliance / shipping_zones): it
unit-tests without a DB and mirrors byte-for-byte into the grove-sites PDP
notice via ``substitution_feed`` inside ``shipping_zones.rate_feed``.
"""

from typing import NamedTuple

try:
    from . import plant_compliance
except ImportError:  # loaded standalone (tests import by file path)
    import importlib.util as _ilu
    import os as _os

    def _load_sibling(name):
        path = _os.path.join(_os.path.dirname(__file__), f"{name}.py")
        spec = _ilu.spec_from_file_location(f"grove_{name}", path)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    plant_compliance = _load_sibling("plant_compliance")


class Substitute(NamedTuple):
    """What we ship in place of a restricted genus."""

    botanical: str  # botanical name of the substitute component
    label: str  # shopper-facing common name
    native: bool  # True if North-American native (drives the "five natives" copy)


# ── Substitute map ───────────────────────────────────────────────────────────
# Keyed by GENUS (matching the carve-out gate's granularity in plant_compliance).
# A component is substituted iff ``plant_compliance.is_taxon_blocked`` says its
# taxon can't ship to the destination; the entry here is only ever consulted for
# a component that is already known to be blocked, so the value is simply "what
# we ship instead". Every substitute MUST itself clear every state where the
# original is blocked — ``validate_substitutes`` pins that, and the test suite
# asserts it returns no problems.
SUBSTITUTES: dict[str, Substitute] = {
    # Castanea (chestnut) is blocked WA/OR/FL -> ship Shagbark Hickory, a native
    # nut tree that clears all three (Carya is only restricted in AZ/NM).
    "castanea": Substitute("Carya ovata", "Shagbark Hickory", native=True),
    # Prunus (American plum) is blocked WA/OR -> ship Jujube. NOT NA-native, so a
    # state that receives it loses the "five natives" claim.
    "prunus": Substitute("Ziziphus jujuba", "Jujube", native=False),
}


class ComponentSwap(NamedTuple):
    """One substitution the warehouse must make for a destination state."""

    original_botanical: str
    original_label: str
    substitute_botanical: str
    substitute_label: str
    substitute_native: bool


def _substitute_for(genus: str, species: str | None) -> Substitute | None:
    """Substitute for a genus, or None if none is defined. Genus granularity
    matches the carve-out gate (a genus block substitutes at genus resolution)."""
    return SUBSTITUTES.get(genus)


def swaps_for_state(components, state_code: str) -> list[ComponentSwap]:
    """Ordered substitutions needed to ship a bundle into ``state_code``.

    ``components`` is an iterable of ``(botanical_name, display_label)`` for the
    bundle's DEFAULT composition. ``state_code`` is the canonical 2-letter USPS
    code (already normalized by ``shipping_zones.canonical_state_code``).

    A component is swapped iff ``plant_compliance`` blocks its taxon into that
    state; unblocked components (and components with an empty/unparseable
    botanical name — curated bundle contents, left as-is) are untouched.

    Raises ``ValueError`` if a component IS blocked but no substitute is defined:
    that is a data gap that would silently break the "bundles ship everywhere"
    promise, so we fail loud (the checkout catches it and logs rather than 500s).
    """
    swaps: list[ComponentSwap] = []
    for botanical, label in components:
        parsed = plant_compliance.parse_taxon(botanical)
        if parsed is None:
            continue
        genus, species = parsed
        if not plant_compliance.is_taxon_blocked(genus, species, state_code):
            continue
        sub = _substitute_for(genus, species)
        if sub is None:
            raise ValueError(
                f"Bundle component {botanical!r} is restricted into {state_code} "
                f"but no substitute is defined (genus {genus!r})."
            )
        swaps.append(ComponentSwap(botanical, label, sub.botanical, sub.label, sub.native))
    return swaps


def effective_composition(components, state_code: str) -> dict:
    """The component list actually shipped to ``state_code``.

    Restricted components are replaced by their substitute; everything else is
    passed through. Shape (also what the PDP renders)::

        {
          "state": "OR",
          "components": [
            {"botanical","label","native","substituted": bool,
             "replaces_botanical","replaces_label"},   # last two only if swapped
            ...
          ],
          "swaps": [ComponentSwap, ...],   # only the substituted components
          "all_native": bool,              # False once any non-native is swapped in
        }
    """
    swaps = swaps_for_state(components, state_code)
    swap_by_original = {sw.original_botanical: sw for sw in swaps}
    out_components = []
    all_native = True
    for botanical, label in components:
        sw = swap_by_original.get(botanical)
        if sw is None:
            out_components.append({"botanical": botanical, "label": label, "native": True, "substituted": False})
            continue
        all_native = all_native and sw.substitute_native
        out_components.append(
            {
                "botanical": sw.substitute_botanical,
                "label": sw.substitute_label,
                "native": sw.substitute_native,
                "substituted": True,
                "replaces_botanical": sw.original_botanical,
                "replaces_label": sw.original_label,
            }
        )
    return {"state": state_code, "components": out_components, "swaps": swaps, "all_native": all_native}


def packing_slip_note(swaps, state_label: str) -> str:
    """Plain-text packing instruction for Josh, or ``""`` when nothing swaps.

    The checkout stamps this onto the order (chatter + a printed line note) so
    the person packing the bundle ships the substituted components, not the
    default ones. Plain text with newlines; the caller HTML-escapes / <br/>-joins
    for chatter.
    """
    if not swaps:
        return ""
    lines = [f"COMPLIANCE SUBSTITUTION ({state_label}) — pack these swaps for this bundle:"]
    for sw in swaps:
        lines.append(
            f"  • {sw.substitute_label} ({sw.substitute_botanical}) "
            f"IN PLACE OF {sw.original_label} ({sw.original_botanical})"
        )
    return "\n".join(lines)


def validate_substitutes() -> list[str]:
    """Return a list of data-integrity problems (empty == healthy).

    A substitute is only valid if it itself clears every state where the taxon
    it replaces is blocked — otherwise substituting would just move the block,
    not clear it. Pinned by the test suite; kept as a function (not an import-time
    assert) so importing the module stays side-effect free like plant_compliance.
    """
    problems: list[str] = []
    for genus, sub in SUBSTITUTES.items():
        blocked_states = [
            s for s in plant_compliance.REGULATED_STATES if plant_compliance.is_taxon_blocked(genus, None, s)
        ]
        sub_parsed = plant_compliance.parse_taxon(sub.botanical)
        if sub_parsed is None:
            problems.append(f"substitute for {genus!r} has an unparseable botanical name {sub.botanical!r}")
            continue
        sub_genus, sub_species = sub_parsed
        for state in blocked_states:
            if plant_compliance.is_taxon_blocked(sub_genus, sub_species, state):
                problems.append(
                    f"substitute {sub.botanical!r} for {genus!r} is itself blocked into {state} "
                    f"(a state where {genus!r} is blocked)"
                )
    return problems


def substitution_feed() -> dict:
    """Read-only snapshot of the substitute map for the storefront mirror.

    Shipped inside ``shipping_zones.rate_feed`` (beside ``compliance``) so the
    grove-sites PDP computes the effective bundle composition from the same map
    the checkout substitutes with, and the two can never drift. The frontend
    reuses the ``compliance.carve_outs`` already in the feed to decide WHICH
    components to swap, and this block for WHAT to swap them to. Shape::

        {
          "schema": 1,
          "substitutes": {"castanea": {"botanical": "Carya ovata",
                                       "label": "Shagbark Hickory",
                                       "native": true}, ...},
        }
    """
    return {
        "schema": 1,
        "substitutes": {
            genus: {"botanical": s.botanical, "label": s.label, "native": s.native} for genus, s in SUBSTITUTES.items()
        },
    }
