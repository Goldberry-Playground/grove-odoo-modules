#!/usr/bin/env python3
"""Seed the QA planner-catalog fixtures so the nursery planner e2e specs RUN, not skip.

Parent: GOL-3999 (follow-up to GOL-3997). The two nursery planner e2e specs
(``planner-build-order``, ``planner-winter-beta``) call ``skipIfPlannerFeedEmpty``
and **skip** when ``GET /api/planner/catalog`` returns ``plants: 0``. On a fresh
QA bring-up the feed is empty — QA's L3 sync does not carry the planner facts /
listing sign-off, so no template is ``grove_planner_ready`` — and the render path
goes uncovered every QA cycle (GOL-3997 made the specs skip to clear the
Train #5 false-red; this script makes them run).

Why a dedicated seed (not the backfill scripts)
-----------------------------------------------
``backfill_planner_facts.py`` / ``backfill_planner_roles.py`` are the *prod*
one-time parsers. They **clear ``grove_facts_reviewed``** on every template they
write (the "a machine write drops the human sign-off" rule), which drops
``grove_listing_complete`` → ``grove_planner_ready`` to **False** until a human
re-reviews. Running them against QA would therefore leave the feed *emptier*, not
fuller. A planner-ready template needs a human sign-off that QA has no human to
give, so the durable QA answer is a fixed set of **synthetic, fully
planner-ready fixtures** written directly — the same pattern as
``seed_e2e_test_inventory.py``.

What ``grove_planner_ready`` requires (grove_headless/models/product_template.py)
--------------------------------------------------------------------------------
The feed (``planner_catalog`` controller) selects, for the storefront's company:
``sale_ok`` ∧ ``website_published`` ∧ ``grove_shipping_tier != "seed"`` ∧
``grove_planner_ready``. ``grove_planner_ready`` is the stored compute
``_grove_planner_ready_value``:

* ``grove_listing_complete`` — all 12 required growing facts set
  (``_GROVE_REQUIRED_FACTS``), a non-blank ``description_ecommerce``, a non-blank
  ``website_description`` **with** ``grove_guide_ready``, ``grove_facts_reviewed``,
  and no format warnings (dimensions need a unit, Wildlife ≥3 specific words, no
  double spaces — ``_grove_format_warnings``);
* not ``grove_consult_built`` and no ``mrp.bom`` (prod/QA carry none);
* the planner core facts set — ``grove_years_to_fruit_min`` > 0,
  ``grove_pollination_min`` > 0, a non-blank ``grove_harvest_months``,
  ``grove_planner_role``, ``grove_succession`` (+ ``grove_substrate`` iff fungal).

Each fixture below sets that whole set, so each is planner-ready the moment it
lands — no review pass needed.

The fixture set (a plantable WV zone-6 "food forest / open ground / 20")
-----------------------------------------------------------------------
Six plants spanning the strata an open-ground food-forest plan draws from —
canopy, understory, shrub, ground cover and vine — each a real WV zone-6 species
with honest facts, split across ``anchor``/``companion`` roles and
``fast``/``mid``/``long`` succession so the planner has a diverse set to build a
layered plan from. Each is **stocked and priced** (GOL-4003): the planner engine
builds only from *available* plants (grove-sites ``plan.ts`` ``available()`` =
``in_stock && !preorder_cap_reached``), so an unstocked fixture sits in the feed
but drops to the "notify" map and the plan comes back **empty** — exactly the
10/10 spec failure GOL-4003 reported. Stock (``E2E_PLANNER_QTY`` on hand via
``is_storable`` + a quant) makes the feed serve ``in_stock: true``; a non-$0
``E2E_PLANNER_PRICE`` carts a priced line. ``company_id`` is pinned to the nursery
company so the fixtures scope to the nursery tenant feed and never leak onto the
farm/GGG storefronts (``company_domain``).

Because these are now genuinely-buyable "Add to cart" cards, the grove-sites e2e
suite is taught to ignore them by name (``QA Planner Fixture — …``) so they never
shift the checkout suite's ``findProductByCta`` pick / deterministic cart totals,
nor the ``qa-photos`` catalog check (they carry no image by design). See
grove-sites ``apps/nursery/e2e/helpers.ts`` + ``qa-helpers.ts`` (GOL-4003).

Determinism / idempotency
-------------------------
Each fixture is matched by its own ``default_code``; a converged fixture is a
no-op re-run, and an existing one is **reconciled** (the planner-ready field set
is rewritten) rather than duplicated. Safe to run on every QA bring-up.

Prod safety
-----------
This publishes synthetic "QA Planner Fixture — …" plants — now **stocked and
buyable** — into a live storefront catalog. Two guards (identical to
``seed_e2e_test_inventory.py``):

* **Dry run is the DEFAULT** (opt-out) — reports the plan, writes nothing. A live
  run requires an explicit ``DRY_RUN=0``.
* A live run is **REFUSED** unless BOTH the URL host is a known QA host AND the DB
  is a known QA DB, unless ``--force-i-know-this-is-not-qa`` is passed.

Usage
-----
    # Dry run (read-only, DEFAULT):
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com ODOO_DB=odoo \\
    ODOO_USER=josh@goldberrygrove.farm ODOO_PASSWORD=<admin-or-api-key> \\
    python3 scripts/seed_e2e_planner_catalog.py

    # Live (QA only): add DRY_RUN=0 -> creates/reconciles the six fixtures.
    DRY_RUN=0 ODOO_URL=... ODOO_DB=odoo ... python3 scripts/seed_e2e_planner_catalog.py

    # Verify the feed really carries them (the GOL-3999 done-criterion). Runs
    # after the seed, read-only, and FAILS the run on plants: 0:
    E2E_PLANNER_FEED_URL=https://nursery.qa.gatheringatthegrove.com/api/planner/catalog \\
    DRY_RUN=0 ODOO_URL=... python3 scripts/seed_e2e_planner_catalog.py

Knobs (env, all optional):
    DRY_RUN                default "1" (dry)   set "0" for a LIVE run (opt-out)
    COMPANY_NAME           default "At The Grove Nursery"
    E2E_PLANNER_QTY        default "500"       on-hand stock per fixture. Deep so a
                                               20-tree plan + the two planner specs
                                               adding it to a cart on every run never
                                               drain a fixture to out-of-stock.
    E2E_PLANNER_PRICE      default "42.00"     list_price (USD) per fixture, so the
                                               planner carts a priced (non-$0) line.
    E2E_PLANNER_FEED_URL   default "" (skip)   storefront /api/planner/catalog URL;
                                               when set, probed after the seed and a
                                               plants:0 — OR any fixture row served
                                               in_stock:false — FAILS the run.

Flags (argv):
    --force-i-know-this-is-not-qa   allow a LIVE run against a non-QA target

Exit codes: 0 ok, 1 auth/data failure OR refused non-QA live target.
"""

from __future__ import annotations

import json as _json
import os
import sys
import urllib.request as _ureq
import xmlrpc.client
from typing import Any
from urllib.parse import urlsplit as _urlsplit

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
# Dry run is the DEFAULT (opt-out) — matches the repo's dry-run-default convention.
DRY_RUN = os.getenv("DRY_RUN", "1") != "0"

COMPANY_NAME = os.getenv("COMPANY_NAME", "At The Grove Nursery")

# Sellable stock + a non-$0 price make each fixture a plant the planner engine can
# actually BUILD a plan from (GOL-4003). The planner engine's ``available()``
# (grove-sites packages/planner/src/plan.ts) = ``in_stock && !preorder_cap_reached``;
# an out-of-stock plant goes to the "notify" map and the pool is empty, so the plan
# comes back ``empty`` even though the feed carries the row. So the fixtures must be
# stocked. ``E2E_PLANNER_QTY`` is deep (a 20-tree plan draws a handful of each of
# six species, and the two planner specs then add the whole plan to a cart on every
# run) so repeated gate runs never drain a fixture to out-of-stock and silently
# re-empty the plan. ``E2E_PLANNER_PRICE`` gives every line a real, non-$0 price so
# the planner's add-to-cart lands a priced line (a $0 list_price otherwise carts a
# $0 line).
E2E_PLANNER_QTY = int(os.getenv("E2E_PLANNER_QTY", "500"))
E2E_PLANNER_PRICE = float(os.getenv("E2E_PLANNER_PRICE", "42.00"))

# Optional post-seed verification through the real storefront BFF (the GOL-3999
# done-criterion made executable). Empty = skip; set it and a plants:0 answer FAILS.
E2E_PLANNER_FEED_URL = os.getenv("E2E_PLANNER_FEED_URL", "").strip()

# --- Prod-safety allowlist (mirrors seed_e2e_test_inventory.py / GOL-1310) ----
QA_HOSTS = {"localhost", "127.0.0.1", "odoo.qa.gatheringatthegrove.com"}
QA_DBS = {"odoo"}
FORCE_FLAG = "--force-i-know-this-is-not-qa"
FORCE_NOT_QA = FORCE_FLAG in sys.argv

# A shared storefront description + care guide for every fixture. Real prose (not
# a bare tag) so _html_is_blank() reads them as present; generic enough to suit
# any of the six without claiming false specifics.
_FIXTURE_DESCRIPTION_HTML = (
    "<p>Automated QA planner-catalog fixture. A hardy, WV zone-6 food-forest "
    "species carried so the nursery planner end-to-end specs exercise the real "
    "render path. Not a real retail listing.</p>"
)
_FIXTURE_GUIDE_HTML = (
    "<p>QA care-guide fixture. Plant in its layer, water to establish, and let it "
    "settle into the guild. Placeholder guide text for the planner e2e feed.</p>"
)

# ── The fixtures — the single source of truth the planner e2e specs run over ──
# Every field below is set so grove_planner_ready computes True on write:
#   * the 12 required growing facts (botanical name, zone min/max, layer, sun,
#     mature size/spread, spacing, soil, pollination, years-to-fruit, chill hrs);
#     dimensions carry a unit and Wildlife carries ≥3 specific words so the
#     format gate stays quiet;
#   * the planner core facts (years_to_fruit_min, pollination_min,
#     harvest_months, planner_role, succession);
#   * zone_min ≤ 6 ≤ zone_max so each is in-zone for a WV (zone-6) plan.
FIXTURES: list[dict[str, Any]] = [
    {
        "sku": "E2E-PLANNER-CANOPY",
        "name": "QA Planner Fixture — Chinese Chestnut (automated test)",
        "layer": "canopy",
        "role": "anchor",
        "succession": "long",
        "botanical": "Castanea mollissima",
        "zone_min": 4,
        "zone_max": 8,
        "sun": "full",
        "mature_size": "40–60 ft",
        "mature_spread": "40–60 ft",
        "spacing": "40 ft",
        "soil": "Well-drained loam; tolerates rocky ground",
        "pollination": "Plant 2 or more for a crop",
        "years_to_fruit": "3–5 years",
        "chill_hours": "400–600 hours",
        "wildlife": "Deer, turkey, squirrels",
        "harvest_season": "September to October",
        "years_min": 3,
        "pollination_min": 2,
        "harvest_months": "9,10",
    },
    {
        "sku": "E2E-PLANNER-UNDERSTORY",
        "name": "QA Planner Fixture — Pawpaw (automated test)",
        "layer": "understory",
        "role": "anchor",
        "succession": "mid",
        "botanical": "Asimina triloba",
        "zone_min": 5,
        "zone_max": 8,
        "sun": "partial",
        "mature_size": "15–25 ft",
        "mature_spread": "10–15 ft",
        "spacing": "12 ft",
        "soil": "Moist, well-drained bottomland soil",
        "pollination": "Plant 2 for cross-pollination",
        "years_to_fruit": "4–7 years",
        "chill_hours": "400 hours",
        "wildlife": "Deer, raccoons, songbirds",
        "harvest_season": "September to October",
        "years_min": 4,
        "pollination_min": 2,
        "harvest_months": "9,10",
    },
    {
        "sku": "E2E-PLANNER-SHRUB-ANCHOR",
        "name": "QA Planner Fixture — Hazelnut (automated test)",
        "layer": "shrub",
        "role": "anchor",
        "succession": "mid",
        "botanical": "Corylus americana",
        "zone_min": 4,
        "zone_max": 9,
        "sun": "full",
        "mature_size": "8–12 ft",
        "mature_spread": "8–12 ft",
        "spacing": "8 ft",
        "soil": "Adaptable; tolerates clay and poor soil",
        "pollination": "Plant 2 or more for nuts",
        "years_to_fruit": "3–4 years",
        "chill_hours": "800–1200 hours",
        "wildlife": "Deer, turkey, songbirds",
        "harvest_season": "September",
        "years_min": 3,
        "pollination_min": 2,
        "harvest_months": "9",
    },
    {
        "sku": "E2E-PLANNER-SHRUB-COMPANION",
        "name": "QA Planner Fixture — Elderberry (automated test)",
        "layer": "shrub",
        "role": "companion",
        "succession": "fast",
        "botanical": "Sambucus canadensis",
        "zone_min": 3,
        "zone_max": 9,
        "sun": "full",
        "mature_size": "6–12 ft",
        "mature_spread": "6–12 ft",
        "spacing": "6 ft",
        "soil": "Moist soil; tolerates wet feet",
        "pollination": "Plant 2 varieties for best fruit set",
        "years_to_fruit": "2–3 years",
        "chill_hours": "Low chill requirement",
        "wildlife": "Birds, pollinators, songbirds",
        "harvest_season": "August to September",
        "years_min": 2,
        "pollination_min": 2,
        "harvest_months": "8,9",
    },
    {
        "sku": "E2E-PLANNER-GROUND",
        "name": "QA Planner Fixture — Alpine Strawberry (automated test)",
        "layer": "ground",
        "role": "companion",
        "succession": "fast",
        "living_mulch": True,
        "botanical": "Fragaria vesca",
        "zone_min": 3,
        "zone_max": 9,
        "sun": "partial",
        "mature_size": "6–8 in",
        "mature_spread": "12 in",
        "spacing": "10 in",
        "soil": "Rich, moist, well-drained soil",
        "pollination": "Self-fertile",
        "years_to_fruit": "1 year",
        "chill_hours": "Low chill requirement",
        "wildlife": "Birds, bees, pollinators",
        "harvest_season": "June to July",
        "years_min": 1,
        "pollination_min": 1,
        "harvest_months": "6,7",
    },
    {
        "sku": "E2E-PLANNER-VINE",
        "name": "QA Planner Fixture — Hardy Kiwi (automated test)",
        "layer": "vine",
        "role": "companion",
        "succession": "mid",
        "botanical": "Actinidia arguta",
        "zone_min": 4,
        "zone_max": 8,
        "sun": "full",
        "mature_size": "20–30 ft",
        "mature_spread": "10 ft",
        "spacing": "15 ft",
        "soil": "Fertile, well-drained soil",
        "pollination": "Plant a male and female vine",
        "years_to_fruit": "3–5 years",
        "chill_hours": "400–600 hours",
        "wildlife": "Birds, bees, pollinators",
        "harvest_season": "September to October",
        "years_min": 3,
        "pollination_min": 2,
        "harvest_months": "9,10",
    },
]


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ── Pure fixture-vals builder (unit-tested in test_planner_catalog_seed.py) ───
def planner_fixture_vals(spec: dict, company_id: int) -> dict:
    """Build the full write-vals that make one fixture ``grove_planner_ready``.

    Pure (no network), so the unit test can assert every gate field is present.
    Covers the 12 required growing facts, the storefront description + care guide
    (+ ``grove_guide_ready`` / ``grove_facts_reviewed`` sign-offs), and the
    planner core facts. ``grove_shipping_tier`` is set to ``"bareroot"`` so the
    retail feed keeps the plant year-round (it drops *potted-only* plants outside
    the leafed window, GOL-3800) and so the ``!= "seed"`` filter keeps it.
    ``company_id`` scopes it to the nursery tenant feed.

    ``is_storable`` is True so Odoo tracks on-hand for the fixture — the caller
    then stocks the single variant (``apply_stock``) so the feed serves
    ``in_stock: true`` and the planner engine builds from it instead of dropping
    it to "notify" (GOL-4003). ``list_price`` is a real, non-$0 price so the
    planner add-to-cart carts a priced line.
    """
    vals: dict = {
        "name": spec["name"],
        "default_code": spec["sku"],
        "type": "consu",
        # Inventory-tracked goods, so qty_available is real and the feed's
        # _list_in_stock (any variant qty_available > 0) can read True (GOL-4003).
        "is_storable": True,
        "sale_ok": True,
        "purchase_ok": False,
        "is_published": True,
        "list_price": E2E_PLANNER_PRICE,
        "company_id": company_id,
        # Bareroot tier so the plant stays in the RETAIL planner feed year-round.
        # The retail feed drops *potted-only* plants outside LEAFED_WINDOW
        # (GOL-3800, _planner_potted_only_dropped): the one-button cart rides a
        # wave and _validate_ship_wave refuses a potted line out of season. A
        # bareroot-tier plant is never dropped — and these synthetic trees are
        # plantable bareroot stock, so "bareroot" is the honest tier. (It is also
        # != "seed", so the feed's seed filter keeps it.)
        "grove_shipping_tier": "bareroot",
        # 12 required growing facts (grove_listing_complete)
        "grove_botanical_name": spec["botanical"],
        "grove_zone_min": spec["zone_min"],
        "grove_zone_max": spec["zone_max"],
        "grove_layer": spec["layer"],
        "grove_sun": spec["sun"],
        "grove_mature_size": spec["mature_size"],
        "grove_mature_spread": spec["mature_spread"],
        "grove_spacing": spec["spacing"],
        "grove_soil": spec["soil"],
        "grove_pollination": spec["pollination"],
        "grove_years_to_fruit": spec["years_to_fruit"],
        "grove_chill_hours": spec["chill_hours"],
        # Spec-text fields the format gate inspects (kept clean: Wildlife ≥3 words)
        "grove_wildlife": spec["wildlife"],
        "grove_harvest_season": spec["harvest_season"],
        # Storefront content + the two human sign-offs
        "description_ecommerce": _FIXTURE_DESCRIPTION_HTML,
        "website_description": _FIXTURE_GUIDE_HTML,
        "grove_guide_ready": True,
        "grove_facts_reviewed": True,
        # Planner core facts (grove_planner_ready)
        "grove_years_to_fruit_min": spec["years_min"],
        "grove_pollination_min": spec["pollination_min"],
        "grove_harvest_months": spec["harvest_months"],
        "grove_planner_role": spec["role"],
        "grove_succession": spec["succession"],
        # A real species mix, never a bundle/consult build.
        "grove_consult_built": False,
    }
    if spec.get("living_mulch"):
        vals["grove_living_mulch"] = True
    return vals


def guard_environment() -> None:
    """Refuse a LIVE run unless the target is known-QA (mirrors the sibling seed)."""
    if DRY_RUN:
        return
    host = (_urlsplit(ODOO_URL).hostname or "").lower()
    host_ok = host in QA_HOSTS
    db_ok = ODOO_DB in QA_DBS
    if host_ok and db_ok:
        return
    if FORCE_NOT_QA:
        print(
            f"WARNING: {FORCE_FLAG} set — live seed against non-QA target host={host!r} db={ODOO_DB!r}.",
            file=sys.stderr,
        )
        return
    reasons = []
    if not host_ok:
        reasons.append(f"host {host!r} not in QA_HOSTS {sorted(QA_HOSTS)}")
    if not db_ok:
        reasons.append(f"db {ODOO_DB!r} not in QA_DBS {sorted(QA_DBS)}")
    fail(
        "REFUSED live seed against a non-QA target (" + "; ".join(reasons) + "). "
        "This script publishes synthetic planner fixtures into a live storefront. "
        f"Point it at QA, run with DRY_RUN=1, or pass {FORCE_FLAG} if you are certain."
    )


def authenticate() -> tuple[xmlrpc.client.ServerProxy, int]:
    if not ODOO_PASSWORD:
        fail("ODOO_PASSWORD env var is required (admin password or a user API key)")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        fail(f"Authentication failed for user {ODOO_USER} on db {ODOO_DB}")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    print(f"Authenticated as uid={uid} on db={ODOO_DB}")
    return models, uid


def call(models, uid, model: str, method: str, args: list, kwargs: dict | None = None):
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def resolve_company_id(models, uid) -> int:
    rows = call(
        models,
        uid,
        "res.company",
        "search_read",
        [[("name", "=", COMPANY_NAME)]],
        {"fields": ["id"], "limit": 1},
    )
    if not rows:
        fail(f"Company {COMPANY_NAME!r} not found on this DB")
    return rows[0]["id"]


def resolve_stock_location(models, uid, company_id: int) -> int:
    """The nursery warehouse's lot_stock_id, where fixture on-hand lands."""
    wh = call(
        models,
        uid,
        "stock.warehouse",
        "search_read",
        [[("company_id", "=", company_id)]],
        {"fields": ["lot_stock_id"], "limit": 1},
    )
    if not wh:
        fail(f"No warehouse for company {company_id}")
    return wh[0]["lot_stock_id"][0]


def apply_stock(models, uid, ctx, variant_id: int, location_id: int, qty: float) -> None:
    """Idempotently set on-hand ``qty`` for ``variant_id`` at ``location_id``.

    Mirrors ``seed_e2e_test_inventory.apply_stock``: look up the existing quant and
    WRITE ``inventory_quantity`` to the fixed target (converges regardless of prior
    test churn) rather than blind-creating, then apply. The write carries
    ``inventory_mode`` context or Odoo's quant access rule rejects it.
    ``action_apply_inventory`` returns None, which the XML-RPC marshaller rejects,
    so it goes over JSON-RPC.
    """
    quant = call(
        models,
        uid,
        "stock.quant",
        "search",
        [[("product_id", "=", variant_id), ("location_id", "=", location_id)]],
        {"limit": 1, "context": ctx["context"]},
    )
    if quant:
        quant_id = quant[0]
        call(
            models,
            uid,
            "stock.quant",
            "write",
            [[quant_id], {"inventory_quantity": qty}],
            {"context": {"inventory_mode": True, **ctx["context"]}},
        )
    else:
        quant_id = call(
            models,
            uid,
            "stock.quant",
            "create",
            [{"product_id": variant_id, "location_id": location_id, "inventory_quantity": qty}],
            {"context": {"inventory_mode": True, **ctx["context"]}},
        )
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "object",
            "method": "execute_kw",
            "args": [ODOO_DB, uid, ODOO_PASSWORD, "stock.quant", "action_apply_inventory", [[quant_id]], ctx],
        },
    }
    resp = _json.loads(
        _ureq.urlopen(
            _ureq.Request(
                f"{ODOO_URL}/jsonrpc",
                data=_json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            ),
            timeout=30,
        ).read()
    )
    if resp.get("error"):
        fail(f"action_apply_inventory jsonrpc error: {resp['error']}")


def seed_fixture(models, uid, ctx, company_id: int, stock_location_id: int, spec: dict) -> int | None:
    """Create or reconcile one planner-ready fixture template, keyed by default_code."""
    sku = spec["sku"]
    print(f"\n════ Fixture: {sku!r} ({spec['layer']}/{spec['role']}/{spec['succession']}) ════")
    vals = planner_fixture_vals(spec, company_id)

    existing = call(
        models,
        uid,
        "product.template",
        "search",
        [[("default_code", "=", sku), ("company_id", "in", [company_id, False])]],
        {"limit": 1},
    )
    if existing:
        tmpl_id = existing[0]
        if DRY_RUN:
            print(
                f"  = fixture exists (id={tmpl_id}); would reconcile {len(vals)} field(s) to "
                f"planner-ready and stock {E2E_PLANNER_QTY} on hand @ ${E2E_PLANNER_PRICE:.2f}"
            )
            return tmpl_id
        call(models, uid, "product.template", "write", [[tmpl_id], vals], ctx)
        print(f"  ~ reconciled fixture (id={tmpl_id}) to planner-ready")
    elif DRY_RUN:
        print(
            f"  + WOULD CREATE planner-ready fixture {spec['name']!r}, stocked "
            f"{E2E_PLANNER_QTY} on hand @ ${E2E_PLANNER_PRICE:.2f}"
        )
        return None
    else:
        tmpl_id = call(models, uid, "product.template", "create", [vals], ctx)
        print(f"  + created fixture (id={tmpl_id})")

    _stock_single_variant(models, uid, ctx, stock_location_id, tmpl_id, sku)
    return _report_ready(models, uid, tmpl_id)


def _stock_single_variant(models, uid, ctx, stock_location_id: int, tmpl_id: int, sku: str) -> None:
    """Stock the fixture's one variant to ``E2E_PLANNER_QTY`` on hand (GOL-4003).

    These fixtures carry no variant axis, so a template has exactly one variant;
    stocking it makes ``qty_available > 0`` → the feed serves ``in_stock: true``
    and the planner engine builds from it. Fails loudly if the variant did not end
    up on hand, so an unstocked fixture can never slip through as a green seed.
    """
    variants = call(
        models,
        uid,
        "product.product",
        "search_read",
        [[("product_tmpl_id", "=", tmpl_id)]],
        {"fields": ["id", "qty_available"]},
    )
    if len(variants) != 1:
        fail(f"fixture {sku!r} (id={tmpl_id}) has {len(variants)} variants, expected exactly 1: {variants}")
    variant_id = variants[0]["id"]
    apply_stock(models, uid, ctx, variant_id, stock_location_id, float(E2E_PLANNER_QTY))
    on_hand = call(models, uid, "product.product", "read", [[variant_id], ["qty_available"]])[0]["qty_available"]
    print(f"    stock: variant id={variant_id} on hand = {on_hand} @ location {stock_location_id}")
    if on_hand < 1:
        fail(
            f"fixture {sku!r} (id={tmpl_id}, variant {variant_id}) is still 0 on hand after apply — "
            "the feed would serve in_stock:false and the planner would drop it to 'notify' (GOL-4003)."
        )


def _report_ready(models, uid, tmpl_id: int) -> int:
    """Read back the computed gate flags so the live run proves itself."""
    rec = call(
        models,
        uid,
        "product.template",
        "read",
        [[tmpl_id], ["grove_listing_complete", "grove_planner_ready", "grove_listing_missing"]],
    )[0]
    flag = "READY" if rec["grove_planner_ready"] else "NOT READY"
    print(
        f"    -> grove_listing_complete={rec['grove_listing_complete']} "
        f"grove_planner_ready={rec['grove_planner_ready']} [{flag}]"
    )
    if not rec["grove_planner_ready"]:
        fail(
            f"fixture id={tmpl_id} did NOT compute planner-ready "
            f"(missing: {rec.get('grove_listing_missing') or 'n/a'}). The gate changed — fix the fixture vals."
        )
    return tmpl_id


def verify_feed() -> None:
    """GET the storefront planner feed and FAIL on plants: 0 (the done-criterion)."""
    print(f"\n── Planner feed probe ── GET {E2E_PLANNER_FEED_URL}")
    try:
        req = _ureq.Request(E2E_PLANNER_FEED_URL, headers={"X-Grove-Tenant": "nursery"})
        with _ureq.urlopen(req, timeout=30) as resp:
            body = _json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001 - any failure here must be loud
        fail(f"planner feed probe against {E2E_PLANNER_FEED_URL} failed: {exc}")
    rows = body.get("plants") or []
    plants = len(rows)
    print(
        f"  source={body.get('source')} plants={plants} bundles={len(body.get('bundles') or [])} "
        f"catalog_version={body.get('catalog_version')}"
    )
    if not plants:
        fail("planner feed still returns plants:0 after seeding — fixtures not visible to the nursery tenant.")

    # in_stock readback (GOL-4003): a fixture row that the feed serves OUT OF STOCK
    # goes to the planner's "notify" map, not its buildable pool — the plan then
    # comes back empty even though the row is present. Check it right next to the
    # plants:0 guard so this fails at SEED time, not later in a red e2e run.
    fixture_rows = [r for r in rows if str(r.get("name") or "").startswith("QA Planner Fixture")]
    oos = [r for r in fixture_rows if not r.get("in_stock")]
    in_stock = [r for r in fixture_rows if r.get("in_stock")]
    print(
        f"  fixtures in feed={len(fixture_rows)}  in_stock={len(in_stock)}  "
        f"out_of_stock={len(oos)}  (checked row.in_stock)"
    )
    if not fixture_rows:
        fail(
            "planner feed carries plants but none are the QA Planner Fixture rows — "
            "the seed did not publish to the nursery tenant feed."
        )
    if oos:
        names = ", ".join(sorted(str(r.get("name")) for r in oos))
        fail(
            f"planner feed serves {len(oos)} fixture row(s) with in_stock:false ({names}). "
            "The planner engine only builds from in-stock plants (plan.ts available()), so these "
            "would drop to 'notify' and the plan would be empty — stock them (GOL-4003)."
        )
    print(f"  OK: feed carries {plants} planner-ready plant(s), all {len(in_stock)} fixture(s) in_stock.")


def main() -> None:
    guard_environment()
    models, uid = authenticate()
    company_id = resolve_company_id(models, uid)
    stock_location_id = resolve_stock_location(models, uid, company_id)
    ctx = {"context": {"allowed_company_ids": [company_id], "company_id": company_id}}
    print(f"\n── Seed planner-catalog fixtures ── company={COMPANY_NAME!r} (id={company_id})")
    if DRY_RUN:
        print("DRY_RUN=1 (default): planning only, no writes. Set DRY_RUN=0 to apply.")

    ready = 0
    for spec in FIXTURES:
        tid = seed_fixture(models, uid, ctx, company_id, stock_location_id, spec)
        if tid and not DRY_RUN:
            ready += 1

    if DRY_RUN:
        print(f"\nDone (dry-run). {len(FIXTURES)} fixture(s) planned; 0 written. Set DRY_RUN=0 to apply.")
        return
    print(f"\nDone. {ready}/{len(FIXTURES)} fixture(s) seeded planner-ready.")
    if E2E_PLANNER_FEED_URL:
        verify_feed()


if __name__ == "__main__":
    main()
