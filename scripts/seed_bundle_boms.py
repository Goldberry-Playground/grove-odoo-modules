#!/usr/bin/env python3
"""Seed phantom (Kit) Bills of Materials for the *existing* prod bundle products.

GOL-2589. Prod carries ZERO ``mrp.bom`` records, so the GOL-2237 bundle path
(per-state component substitution + packing-slip note) and per-component
inventory have never run on prod. The GOL-2587 hotfix papers over the checkout
carve-out gate with ``grove_compliance_exempt``; this script installs the real
kit BoMs so the substitution + inventory path is exercised for real.

Unlike ``seed_kit_boms.py`` (which *creates* sample kit products by SKU on a dev
db), this script attaches phantom BoMs to bundle products that **already exist on
prod**, addressed by their ``product.template`` id. It never creates a product.

Composition (from each bundle's product description):

  * 22  Remembrance Grove  — READY. 1× each of tmpl 8 (Chestnut – Hybrid),
        4 (American Persimmon), 5 (American Plum), 19 (Service Berry),
        18 (Red Mulberry).
        NOTE for review: this differs from the GOL-2237 *substitution engine*
        default (Pawpaw + Eastern Redbud instead of Persimmon + Red Mulberry).
        The product description is authoritative for the physical kit; the
        substitution table only kicks in when a component is state-blocked. Josh
        should confirm the two agree before the real run.

  * 132 Mountain Mama      — NEEDS_INPUT (not seeded). 2× chestnut (American 93
        OR Hybrid 8 — Josh decides), 2× PawPaw (91), 1× mountain laurel (no
        product on prod yet — Josh/Otto create it or drop it from the BoM).

  * 133 Pollinator, 134 Centennial, 135 Food Forest — CUSTOM_MIX. Composition is
        "we build the mix with you", so it is NOT representable as a fixed BoM.
        They stay ``grove_compliance_exempt``-flagged (the hotfix) and are only
        reported here, never seeded.

Safety:
  * DRY_RUN=1 by default — prints the plan and inventory impact, writes nothing.
  * Idempotent by parent template id: a bundle that already has a phantom BoM is
    skipped.
  * A kit's availability becomes ``min(component_on_hand // qty)``. If seeding a
    BoM would flip a currently-sellable bundle to sold out, the script STOPS
    (exit 2) and writes nothing — Josh decides before the real run.
  * On a real (DRY_RUN=0) run, after a BoM is created for a bundle its
    ``grove_compliance_exempt`` flag is cleared so the real substitution path is
    exercised (issue step 3). CUSTOM_MIX / NEEDS_INPUT bundles keep the flag.

Usage:
    # dry run (default) against prod, read-only:
    ODOO_URL=... ODOO_DB=... ODOO_USER=... ODOO_PASSWORD=... \
        python3 scripts/seed_bundle_boms.py

    # real run, only with Josh's go:
    DRY_RUN=0 ODOO_PASSWORD=... python3 scripts/seed_bundle_boms.py
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "Goldberry")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
COMPANY_NAME = os.getenv("ODOO_COMPANY", "Goldberry Grove Farm")

# DRY_RUN defaults ON. Any value other than "0" keeps it a dry run.
DRY_RUN = os.getenv("DRY_RUN", "1") != "0"

# Bundle lifecycle states.
READY = "READY"  # fixed composition, safe to seed
NEEDS_INPUT = "NEEDS_INPUT"  # composition undecided or a component product is missing
CUSTOM_MIX = "CUSTOM_MIX"  # "we build the mix with you" — not a fixed BoM


@dataclass(frozen=True)
class Component:
    """One kit line: a component product template id and how many go in the kit."""

    template_id: int
    qty: float
    label: str


@dataclass(frozen=True)
class Bundle:
    """A prod bundle product (by template id) and its intended composition."""

    template_id: int
    name: str
    status: str
    components: tuple[Component, ...] = ()
    note: str = ""


# The composition table. Component template ids are PROD product.template ids.
BUNDLES: list[Bundle] = [
    Bundle(
        template_id=22,
        name="Remembrance Grove",
        status=READY,
        components=(
            Component(8, 1, "Chestnut – Hybrid"),
            Component(4, 1, "American Persimmon"),
            Component(5, 1, "American Plum"),
            Component(19, 1, "Service Berry"),
            Component(18, 1, "Red Mulberry"),
        ),
    ),
    Bundle(
        template_id=132,
        name="Mountain Mama",
        status=NEEDS_INPUT,
        # Left unresolved on purpose — recorded so the dry run reports exactly
        # what Josh must decide before this bundle can be seeded.
        components=(Component(91, 2, "PawPaw"),),
        note=(
            "BLOCKED on two decisions: (1) chestnut variant — American (tmpl 93) "
            "OR Hybrid (tmpl 8), 2×; (2) mountain laurel (1×) has no product on "
            "prod — Josh/Otto create it or drop it from the BoM. Not seeded until "
            "both are resolved."
        ),
    ),
    Bundle(
        template_id=133,
        name="Pollinator",
        status=CUSTOM_MIX,
        note="Built with the customer — no fixed BoM. Stays compliance-exempt (hotfix).",
    ),
    Bundle(
        template_id=134,
        name="Centennial",
        status=CUSTOM_MIX,
        note="Built with the customer — no fixed BoM. Stays compliance-exempt (hotfix).",
    ),
    Bundle(
        template_id=135,
        name="Food Forest",
        status=CUSTOM_MIX,
        note="Built with the customer — no fixed BoM. Stays compliance-exempt (hotfix).",
    ),
]


# ── Pure planning core (unit-tested; no network) ─────────────────────────────


def kit_available_from_stock(components: tuple[Component, ...], on_hand: dict[int, float]) -> int:
    """Whole kits buildable from current component stock: ``min(on_hand // qty)``.

    ``on_hand`` maps component template id → units on hand. Raises KeyError if a
    component's stock is missing (caller must resolve every component first) and
    ValueError on a non-positive component qty (a malformed BoM line).
    """
    if not components:
        return 0
    per_component = []
    for c in components:
        if c.qty <= 0:
            raise ValueError(f"component {c.template_id} ({c.label}) has non-positive qty {c.qty}")
        stock = on_hand[c.template_id]  # KeyError is intentional — unresolved component
        per_component.append(math.floor(stock / c.qty))
    return int(min(per_component))


def would_flip_to_soldout(current_sellable: float, kit_available: int) -> bool:
    """True if a bundle sellable today (>0 on hand) would drop to 0 buildable kits."""
    return current_sellable > 0 and kit_available <= 0


# ── XML-RPC I/O (thin; exercised on prod, not in unit tests) ─────────────────


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def authenticate():
    import xmlrpc.client

    if not ODOO_PASSWORD:
        fail("ODOO_PASSWORD env var is required")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        fail(f"Authentication failed for user {ODOO_USER} on db {ODOO_DB}")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    print(f"Authenticated as uid={uid} on db={ODOO_DB} ({ODOO_URL})")
    return models, uid


def call(models, uid, model, method, args, kwargs=None):
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def read_template(models, uid, template_id: int) -> dict[str, Any] | None:
    rows = call(
        models,
        uid,
        "product.template",
        "read",
        [[template_id]],
        {"fields": ["name", "product_variant_id", "grove_compliance_exempt"]},
    )
    return rows[0] if rows else None


def variant_id_of(template_row: dict[str, Any]) -> int | None:
    ref = template_row.get("product_variant_id")
    if not ref:
        return None
    return ref[0] if isinstance(ref, list) else ref


def on_hand_of_variant(models, uid, variant_id: int) -> float:
    rows = call(models, uid, "product.product", "read", [[variant_id]], {"fields": ["qty_available"]})
    return float(rows[0]["qty_available"]) if rows else 0.0


def phantom_bom_exists(models, uid, template_id: int) -> bool:
    ids = call(
        models,
        uid,
        "mrp.bom",
        "search",
        [[("product_tmpl_id", "=", template_id), ("type", "=", "phantom")]],
        {"limit": 1},
    )
    return bool(ids)


# ── Orchestration ────────────────────────────────────────────────────────────


@dataclass
class PlanResult:
    would_flip: bool = False
    seeded: list[str] = field(default_factory=list)


def process_ready_bundle(models, uid, company_id: int, bundle: Bundle, result: PlanResult) -> None:
    parent = read_template(models, uid, bundle.template_id)
    if parent is None:
        print(f"  SKIP {bundle.template_id} {bundle.name} — no product.template with this id on this db")
        return
    if phantom_bom_exists(models, uid, bundle.template_id):
        print(f"  SKIP {bundle.template_id} {bundle.name} — phantom BoM already exists (idempotent)")
        return

    # Resolve every component to a variant + its on-hand qty before touching anything.
    on_hand: dict[int, float] = {}
    variant_by_tmpl: dict[int, int] = {}
    missing: list[str] = []
    for c in bundle.components:
        row = read_template(models, uid, c.template_id)
        vid = variant_id_of(row) if row else None
        if vid is None:
            missing.append(f"{c.template_id} ({c.label})")
            continue
        variant_by_tmpl[c.template_id] = vid
        on_hand[c.template_id] = on_hand_of_variant(models, uid, vid)
    if missing:
        print(f"  SKIP {bundle.template_id} {bundle.name} — missing component product(s): {missing}")
        return

    kit_available = kit_available_from_stock(bundle.components, on_hand)
    parent_variant = variant_id_of(parent)
    current_sellable = on_hand_of_variant(models, uid, parent_variant) if parent_variant else 0.0

    stock_detail = ", ".join(f"{c.label}={on_hand[c.template_id]:g}/{c.qty:g}" for c in bundle.components)
    print(
        f"  {bundle.template_id} {bundle.name}: on hand today={current_sellable:g}, "
        f"buildable kits after BoM={kit_available}  [{stock_detail}]"
    )

    if would_flip_to_soldout(current_sellable, kit_available):
        result.would_flip = True
        print(
            f"    ⛔ STOP: seeding this BoM would flip {bundle.name} from "
            f"{current_sellable:g} on hand to 0 buildable kits. Josh decides before the real run."
        )
        return

    if DRY_RUN:
        lines = ", ".join(f"{c.qty:g}× {c.label} (tmpl {c.template_id})" for c in bundle.components)
        print(f"    DRY_RUN: would create phantom BoM → {lines}")
        print("    DRY_RUN: would clear grove_compliance_exempt on this bundle")
        return

    bom_vals = {
        "product_tmpl_id": bundle.template_id,
        "product_id": parent_variant,
        "type": "phantom",
        "product_qty": 1.0,
        "company_id": company_id,
        "bom_line_ids": [
            (0, 0, {"product_id": variant_by_tmpl[c.template_id], "product_qty": c.qty}) for c in bundle.components
        ],
    }
    bom_id = call(models, uid, "mrp.bom", "create", [bom_vals])
    call(models, uid, "product.template", "write", [[bundle.template_id], {"grove_compliance_exempt": False}])
    result.seeded.append(bundle.name)
    print(f"    CREATED phantom BoM id={bom_id}; cleared grove_compliance_exempt")


def main() -> int:
    mode = "DRY RUN (no writes)" if DRY_RUN else "LIVE RUN (writes enabled)"
    print(f"seed_bundle_boms.py — {mode}\n")
    models, uid = authenticate()
    company_ids = call(models, uid, "res.company", "search", [[("name", "=", COMPANY_NAME)]], {"limit": 1})
    if not company_ids:
        fail(f"Could not find company {COMPANY_NAME!r}")
    company_id = company_ids[0]
    print(f"Target company_id={company_id} ({COMPANY_NAME})\n")

    result = PlanResult()
    for bundle in BUNDLES:
        if bundle.status == CUSTOM_MIX:
            print(f"  SKIP {bundle.template_id} {bundle.name} — CUSTOM_MIX: {bundle.note}")
            continue
        if bundle.status == NEEDS_INPUT:
            print(f"  SKIP {bundle.template_id} {bundle.name} — NEEDS_INPUT: {bundle.note}")
            continue
        process_ready_bundle(models, uid, company_id, bundle, result)

    print()
    if result.would_flip:
        print("STOP: at least one bundle would flip to sold out. Nothing was written. Escalate to Josh.")
        return 2
    if DRY_RUN:
        print("Dry run complete. Re-run with DRY_RUN=0 (Josh's go) to write.")
    else:
        print(f"Done. Seeded BoMs for: {result.seeded or 'none'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
