"""Per-product genus/species plant-health compliance carve-outs (GOL-2132).

Some destination states restrict specific plant genera/species for plant-health
reasons (National Plant Board Oct-2025 restriction analysis). This module owns
the carve-out map — which taxon may not ship to which state — keyed on the
genus/species parsed from a product's ``grove_botanical_name``.

It is the compliance layer that sits ON TOP of the green-list zone gate in
``shipping_zones.py``. Two independent gates, in order:

  1. ``shipping_zones`` — the destination must be on the green list to ship at
     all (revenue + broad compliance).
  2. this module — even for a green-list destination, a specific taxon can
     still be blocked into a specific state (targeted plant-health carve-out).

Design mirrors the green list: a pure-Python module with no Odoo imports, so it
unit-tests without a database and mirrors byte-for-byte to the grove-sites
storefront (the PDP notice reads the same map). The Odoo checkout controller
calls ``evaluate_line`` for every standalone cart line and hard-rejects the
order (the potted-block 400 pattern) when a line can't clear the destination.

Bundles (phantom-BOM kits, e.g. Remembrance Grove) are NOT evaluated here — they
ship everywhere via per-state component substitution and are exempt by the
caller, never passed to this module.

Fail-safe by design: a product whose botanical name is empty or unparseable
CANNOT be cleared into a *regulated* state — ``evaluate_line`` blocks it and
flags it for a loud log rather than guess a taxon. Unregulated states are
unaffected, so the existing catalog keeps shipping exactly as before; the
fail-safe only ever tightens shipping into the states that carry restrictions.
"""

# ── Carve-out map (NPB Oct-2025) ─────────────────────────────────────────────
# Keyed on the lowercased taxon token parsed from ``grove_botanical_name``.
# Two rule kinds:
#   ("block", states)  -> the taxon may NOT ship to any state in the set.
#   ("allow", states)  -> the taxon may ship ONLY to states in the set
#                         (blocked everywhere else).
# Lookup is most-specific-first: a genus+species key (e.g. "morus alba") is
# tried before the bare genus ("morus"). That is what keeps the carve-out at
# species resolution where the regulation is — ``Morus alba`` (white mulberry)
# is restricted while ``Morus rubra`` (red mulberry) is clean, because only
# "morus alba" is a key; "morus rubra" and bare "morus" match nothing.
CARVE_OUTS: dict[str, tuple[str, frozenset[str]]] = {
    # Castanea — chestnut / chestnut-hybrid. Chestnut gall wasp / blight certs.
    "castanea": ("block", frozenset({"WA", "OR", "FL"})),
    # Prunus — peach, plum, American plum. Plum pox / stone-fruit certs.
    "prunus": ("block", frozenset({"WA", "OR"})),
    # Cornus — dogwood. Dogwood anthracnose quarantine.
    "cornus": ("block", frozenset({"FL"})),
    # Carya — hickory / pecan. Pecan weevil. (Latent unless hickory ships as a
    # standalone SKU — the bundle's shagbark substitution is handled elsewhere.)
    "carya": ("block", frozenset({"AZ", "NM"})),
    # Morus alba — white mulberry ('Maple Leaf', hybrids). Species-level: the WI
    # male-only exemption does not apply (ours fruit). Morus rubra stays clean.
    "morus alba": ("block", frozenset({"IN", "OH", "WI"})),
    # Diospyros — persimmon. CA restricts persimmon; block that one destination.
    # CA is not on the green list, so this is latent (no green-state impact today).
    "diospyros": ("block", frozenset({"CA"})),
}

# Every state that appears in any carve-out rule (block target or allow target).
# The fail-safe (empty/unparseable botanical -> no-ship) applies ONLY to these
# states: they are the frontier where a restriction could exist, so we refuse to
# guess a taxon into them. A green-list state NOT in this set is unregulated —
# an unparseable botanical name there ships exactly as it did before this module.
REGULATED_STATES: frozenset[str] = frozenset().union(*(states for _kind, states in CARVE_OUTS.values()))


def parse_taxon(botanical_name: str | None) -> tuple[str, str | None] | None:
    """Parse ``(genus, species)`` from a botanical name, both lowercased.

    ``species`` is ``None`` when the name is a bare genus. Returns ``None`` when
    the name is empty or its first token is not alphabetic (unparseable) — the
    caller treats that as the fail-safe case.

    Cultivar epithets and quotes are ignored: ``"Morus alba 'Maple Leaf'"`` ->
    ``("morus", "alba")``; ``"Castanea mollissima"`` -> ``("castanea",
    "mollissima")``; ``"Cornus"`` -> ``("cornus", None)``.
    """
    if not botanical_name:
        return None
    tokens = botanical_name.strip().lower().split()
    if not tokens or not tokens[0].isalpha():
        return None
    genus = tokens[0]
    species = tokens[1] if len(tokens) > 1 and tokens[1].isalpha() else None
    return genus, species


def _rule_for(genus: str, species: str | None) -> tuple[str, frozenset[str]] | None:
    """Most-specific carve-out rule for a taxon, or None if unrestricted."""
    if species is not None:
        rule = CARVE_OUTS.get(f"{genus} {species}")
        if rule is not None:
            return rule
    return CARVE_OUTS.get(genus)


def is_taxon_blocked(genus: str, species: str | None, state_code: str) -> bool:
    """True when this taxon may not ship to ``state_code`` (canonical 2-letter)."""
    rule = _rule_for(genus, species)
    if rule is None:
        return False
    kind, states = rule
    if kind == "block":
        return state_code in states
    # allow-only: blocked anywhere outside the allow set.
    return state_code not in states


def _block_message(botanical_name: str, state_label: str) -> str:
    plant = (botanical_name or "").strip() or "this plant"
    return (
        f"We can't ship {plant} to {state_label} — that state restricts this "
        "plant for plant-health compliance. Remove it to ship the rest of your "
        "order, choose farm pickup, or use “notify me” and we'll reach "
        "out if that changes."
    )


def _failsafe_message(state_label: str) -> str:
    return (
        f"We can't confirm this item is cleared to ship to {state_label} right "
        "now, so we can't ship it there. Remove it to ship the rest of your "
        "order, or choose farm pickup."
    )


def evaluate_line(
    botanical_name: str | None,
    state_code: str,
    state_label: str | None = None,
) -> tuple[str | None, bool]:
    """Evaluate one standalone cart line against the destination state.

    ``state_code`` is the canonical 2-letter USPS code (already normalized by
    ``shipping_zones.canonical_state_code``). ``state_label`` is what to show the
    shopper (the state as they typed it); it defaults to ``state_code``.

    Returns ``(reason, is_failsafe)``:
      * ``reason`` is ``None`` when the line may ship, else a plain-English
        block message for the 400 response / PDP notice.
      * ``is_failsafe`` is ``True`` only when the block is the empty/unparseable
        botanical fail-safe, so the caller can log it loudly (never a silent
        drop, never a guess).

    Do NOT call this for bundle (phantom-BOM) lines — they ship everywhere.
    """
    label = state_label or state_code
    parsed = parse_taxon(botanical_name)
    if parsed is None:
        if state_code in REGULATED_STATES:
            return _failsafe_message(label), True
        return None, False
    genus, species = parsed
    if is_taxon_blocked(genus, species, state_code):
        return _block_message(botanical_name, label), False
    return None, False


def carve_out_feed() -> dict:
    """Read-only snapshot of the carve-out map for the storefront mirror.

    Shipped inside ``shipping_zones.rate_feed`` (like ``green_states``) so
    grove-sites renders the PDP compliance notice from the same source of truth
    the checkout blocks with, and can never drift. Shape::

        {
          "schema": 1,
          "carve_outs": {"castanea": {"kind": "block", "states": ["FL","OR","WA"]}, ...},
          "regulated_states": ["AZ","CA","FL","IN","NM","OH","OR","WA","WI"],
        }
    """
    return {
        "schema": 1,
        "carve_outs": {taxon: {"kind": kind, "states": sorted(states)} for taxon, (kind, states) in CARVE_OUTS.items()},
        "regulated_states": sorted(REGULATED_STATES),
    }
