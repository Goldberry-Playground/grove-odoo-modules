#!/usr/bin/env python3
"""Seed the QA Playwright E2E **test-inventory fixtures** (GOL-1148 + GOL-1154).

Parent: GOL-1074 (nursery checkout E2E). This is the gating data fixture for a
green run: the suite's ``findProductByCta(page, "Add to Cart")`` walks the
``/shop`` grid, opens each product's detail page, and looks for an **enabled
"Add to Cart"** button *as rendered on first paint* (it does not switch the
Format dropdown).

Why the real catalog isn't enough
---------------------------------
The live nursery catalog does carry in-stock potted variants (e.g. American
Plum's Potted variant has stock), but every real template carries a two-value
``Format`` axis (**Bareroot** + **Potted**) where **Bareroot is the default-
selected format** and, in preorder season, has **0 on hand**. So the buy box's
``buyStateFor`` (grove-sites ``apps/nursery/lib/buy-state.ts``) resolves the
*initial* selection to ``reservable`` -> the CTA reads **"Reserve"**. A shopper
must manually flip Format -> Potted to reach "Add to Cart"; the E2E helper never
flips it, so it sees zero "Add to Cart" buttons and specs 1/2/5/6 (+ the happy
Stripe pay) can't add a purchasable line. This is expected preorder-season data,
not a checkout regression.

What these fixtures guarantee
-----------------------------
TWO single-value ("Format"-only) templates with stock on hand, so each one's
*default* (only) variant renders **"Add to Cart"** on first paint with no
dropdown interaction. Both are ``sale_ok`` + ``website_published`` (appear in
``/shop``, purchasable — not "coming soon"), carry exactly one ``Format`` value
(no Bareroot/Potted sibling, so the default selection IS the in-stock variant ->
``buyStateFor`` mode ``in-stock`` -> CTA "Add to Cart"), and are named ``AAA ...``
so they sort FIRST under the ``/shop`` grid's ``name asc`` order.

  1. **Potted-ONLY pickup fixture** (``E2E-POTTED-INSTOCK``, GOL-1148)
     ``grove_shipping_tier = "potted"`` -> exercises the **pickup-only**
     fulfillment path (GOL-1057/1114). A *ship* submit for a potted line 400s
     ("...available for farm pickup only..."), so this fixture is the pickup
     leg, NOT the shippable happy-path.

  2. **Bareroot-ONLY shippable fixture** (``E2E-BAREROOT-INSTOCK``, GOL-1154)
     ``grove_shipping_tier = "bareroot"`` + a ``grove_tree_length`` (so Box
     Engine v2 can size a box) -> a genuinely **shippable** in-stock line. This
     is what the ``@stripe`` ship-flow specs (1 happy-path, 5 declined, 6
     cart-clear) assert against: itemized goods + a non-$0 **shipping** line +
     "Ships now" (no reserve badge, no pickup gate). The potted fixture cannot
     satisfy these because a ship submit 400s on it.

  3. **Plants-category bareroot fixture** (``E2E-BAREROOT-PLANT``, GOL-2463)
     Same shippable shape as (2), but **categorised under the Plants root**
     (``grove_headless.categ_trees``) so ``is_qualifying_plant()`` counts it.
     Fixtures (1) and (2) carry NO ``categ_id``, so they are not plants and can
     never earn a volume tier: a 6-unit cart of ``E2E-BAREROOT-INSTOCK`` returns
     ``qualifyingUnits: 0`` from ``/api/cart/tiers``. That is *deliberately*
     preserved — putting 797 under Plants would silently apply 10-20% off to
     every existing gate spec with a 5+ qty cart and turn a green run red for an
     unrelated reason. So the volume-tier / promo-nudge specs get their OWN
     plant instead, and the deterministic carts stay deterministic.

     Being under Plants makes this fixture **gated** by the listing-content gate
     (GOL-2382, ``_grove_is_gated``): a gated template cannot cross
     unpublished→published while it is missing any of the 12 growing facts, the
     eCommerce description, an approved care guide or the facts sign-off. So this
     fixture seeds that content too (``listing_gate_content()``) — which also
     keeps the nightly listing audit (GOL-2385) from Discord-pinging Josh about
     a permanently-incomplete QA fixture every morning.

     Its name keeps the ``AAA QA E2E `` prefix (grove-sites
     ``E2E_FIXTURE_NAME_RE`` excludes exactly that from the catalog set, and
     these fixtures carry no photo — ``qa-photos.spec.ts`` would otherwise fail
     on a "Photo coming soon" placeholder) while sorting LAST of the three under
     ``/shop``'s ``name asc``, so ``findProductByCta`` still lands on the
     existing fixtures first and the current specs keep buying exactly what they
     buy today.

The ``Reserve`` (bareroot preorder) and ``Coming soon`` states the other specs
need are ALREADY present in the real catalog, so these fixtures only add the
missing "enabled Add to Cart" states (pickup + shippable) plus the plant-
categorised one the volume-tier specs need.

Determinism
-----------
* Each fixture is matched by its own ``default_code``, so a converged fixture is
  a no-op re-run. On an existing template it **reconciles** the fields the specs
  depend on (published / sale_ok / list_price / shipping tier / tree length /
  sale taxes) rather than forking a duplicate.
* Given NO ``public_categ_ids``, so neither inflates a ``?cat=<slug>`` facet
  count (keeps the catalog-browse specs' facet targets honest); they still show
  in the unfiltered grid the helper scans.
* Stock is set idempotently: the variant's quant is looked up and its
  ``inventory_quantity`` is **written to a fixed target** (not blind-created),
  so a re-run converges to the target on hand regardless of prior test churn.

Coordinate the exact product identity / price / stock level with Ada (E2E spec
fixture design, GOL-1074) via the env knobs below — the defaults are sensible
but the SKU/name/price/qty are the single source of truth the specs assert
against.

Prod safety (GOL-1310)
----------------------
This script publishes buyable ``AAA …`` fixtures that sort FIRST in ``/shop``
and stocks them 50-on-hand — pointing it at production would drop fake,
genuinely purchasable test trees at the top of the live storefront. Two guards:

* **Dry run is the DEFAULT** (opt-out). It reports the plan and writes nothing.
  A live run requires an explicit ``DRY_RUN=0``.
* A live run is **REFUSED** unless BOTH the URL host is a known QA host
  (``localhost`` / ``127.0.0.1`` / ``odoo.qa.gatheringatthegrove.com``) AND the
  DB is a known QA DB (``odoo``). Override only with ``--force-i-know-this-is-not-qa``.

Usage
-----
    # Dry run (read-only, DEFAULT): resolves company/warehouse/axis, reports the
    # plan for BOTH fixtures. Writes nothing.
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com \\
    ODOO_DB=odoo \\
    ODOO_USER=josh@goldberrygrove.farm \\
    ODOO_PASSWORD=<admin-or-api-key> \\
    python3 scripts/seed_e2e_test_inventory.py

    # Live: add DRY_RUN=0 -> creates/reconciles both fixtures and applies stock.
    # (Only permitted against a known-QA host+DB; otherwise refused.)
    DRY_RUN=0 ODOO_URL=... ODOO_DB=odoo ... python3 scripts/seed_e2e_test_inventory.py

    # Seed only one fixture: FIXTURE=potted (or bareroot | plant).

    # Verify the plant fixture really earns a tier, through the real storefront
    # BFF (GOL-2463 done-criterion). Runs after the seed, read-only:
    E2E_TIERS_URL=https://nursery.qa.gatheringatthegrove.com/api/cart/tiers \\
    DRY_RUN=0 ODOO_URL=... FIXTURE=plant python3 scripts/seed_e2e_test_inventory.py

Knobs (env, all optional):
    DRY_RUN           default "1" (dry)      set "0" for a LIVE run (opt-out)
    FIXTURE           default "" (all)       "potted" | "bareroot" | "plant"
    E2E_POTTED_SKU    default "E2E-POTTED-INSTOCK"
    E2E_BAREROOT_SKU  default "E2E-BAREROOT-INSTOCK"
    E2E_PLANT_SKU     default "E2E-BAREROOT-PLANT"
    E2E_PRICE         default "42.00"        list_price (USD), all fixtures
    E2E_QTY           default "50"           on-hand target, potted + bareroot
    E2E_PLANT_QTY     default "500"          on-hand target, plant fixture. Higher
                                             because each volume-tier run buys 5-10
                                             units, and a drained free_qty flips the
                                             cart to a deposit cart -> false red.
    E2E_TREE_LENGTH   default "20"           grove_tree_length for the shippable
                                             fixtures: 16|20|32|46
    E2E_PLANT_CATEG   default "grove_headless.categ_trees"
                                             xmlid of the plant fixture's product
                                             category; MUST be at/under
                                             grove_headless.categ_plants or the
                                             fixture cannot earn a tier.
    E2E_TIERS_URL     default "" (skip)      storefront /api/cart/tiers endpoint; when
                                             set, the plant fixture is probed after
                                             seeding and a 0 qualifyingUnits FAILS.
    E2E_TIERS_QTY     default "6"            cart qty used for that probe.

Flags (argv):
    --force-i-know-this-is-not-qa   allow a LIVE run against a non-QA target

Exit codes: 0 ok, 1 auth/data failure OR refused non-QA live target (fails loudly).
"""

from __future__ import annotations

import json as _json
import os
import re
import sys
import urllib.request as _ureq
import xmlrpc.client
from typing import Any
from urllib.parse import urlsplit as _urlsplit

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
# QA `odoo` DB by default (NOT the prod-style "Goldberry") — see guard_environment().
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
# Dry run is the DEFAULT (opt-out), matching the repo's dry-run-default convention.
# A live run requires an explicit DRY_RUN=0 *and* passes guard_environment().
DRY_RUN = os.getenv("DRY_RUN", "1") != "0"

# --- Prod-safety allowlist (GOL-1310) -------------------------------------
# This script publishes buyable "AAA …" fixtures that sort FIRST in /shop and
# stocks them 50-on-hand. A live run against prod would put fake, genuinely
# purchasable test trees at the top of the real storefront. So a live run is
# REFUSED unless BOTH the URL host and the DB are known-QA, or the operator
# passes --force-i-know-this-is-not-qa.
QA_HOSTS = {"localhost", "127.0.0.1", "odoo.qa.gatheringatthegrove.com"}
QA_DBS = {"odoo"}
FORCE_FLAG = "--force-i-know-this-is-not-qa"
FORCE_NOT_QA = FORCE_FLAG in sys.argv

COMPANY_NAME = "At The Grove Nursery"
SALE_TAXES = ["WV State Sales Tax 6%", "WV Municipal Tax 1%"]
FORMAT_ATTR = "Format"

E2E_PRICE = float(os.getenv("E2E_PRICE", "42.00"))
E2E_QTY = int(os.getenv("E2E_QTY", "50"))
# The plant fixture is stocked much deeper than the other two: every volume-tier
# run buys 5-10 units of it, and once free_qty hits 0 the cart flips to a deposit
# cart and the tier assertions go false-red (the GOL-2375 trap, seen on 797).
E2E_PLANT_QTY = int(os.getenv("E2E_PLANT_QTY", "500"))
# grove_tree_length is a selection on product.template; only these ship (16|20|32|46).
E2E_TREE_LENGTH = os.getenv("E2E_TREE_LENGTH", "20")
# Product category for the plant fixture. is_qualifying_plant() (grove_headless
# promotions.py) counts a unit only when its template is type 'consu' AND its
# categ_id sits at/under grove_headless.categ_plants — so this xmlid is the whole
# reason the plant fixture can earn a tier.
E2E_PLANT_CATEG = os.getenv("E2E_PLANT_CATEG", "grove_headless.categ_trees")
PLANTS_ROOT_XMLID = "grove_headless.categ_plants"

# Optional post-seed verification through the real storefront BFF (GOL-2463).
E2E_TIERS_URL = os.getenv("E2E_TIERS_URL", "").strip()
E2E_TIERS_QTY = int(os.getenv("E2E_TIERS_QTY", "6"))

# The 12 growing facts the listing-content gate (GOL-2382) requires before a
# Plants-categorised template may cross unpublished -> published. Mirrors
# grove_headless/models/product_template.py::_GROVE_REQUIRED_FACTS; the unit test
# asserts listing_gate_content() covers every one of them.
REQUIRED_LISTING_FACTS = (
    "grove_botanical_name",
    "grove_zone_min",
    "grove_zone_max",
    "grove_layer",
    "grove_sun",
    "grove_mature_size",
    "grove_mature_spread",
    "grove_spacing",
    "grove_soil",
    "grove_pollination",
    "grove_years_to_fruit",
    "grove_chill_hours",
)


def listing_gate_content(name: str) -> dict[str, Any]:
    """Every field a gated (Plants-categorised) template needs to publish.

    The gate refuses the False->True publish transition while any of the 12
    growing facts, the eCommerce description, an approved care guide
    (``website_description`` + ``grove_guide_ready``) or the facts sign-off
    (``grove_facts_reviewed``) is missing — so a Plants fixture that skipped this
    would simply fail to create. Filling it also keeps the nightly listing audit
    (GOL-2385) from raising a Discord ping + an activity on a QA fixture every
    single morning.

    Deliberately NOT accompanied by a ``grove_facts_provenance`` stamp: a write
    that stamps provenance alongside a fact is treated as a machine enrichment
    write and CLEARS ``grove_facts_reviewed`` in the same write
    (product_template.py::write), which would leave the fixture ungated-but-
    unreviewed and un-publishable on the next run.
    """
    return {
        "grove_botanical_name": "Testus fixtura",
        "grove_zone_min": 4,
        "grove_zone_max": 8,
        "grove_layer": "canopy",
        "grove_sun": "full",
        "grove_mature_size": "20-25 ft",
        "grove_mature_spread": "15-20 ft",
        "grove_spacing": "20 ft",
        "grove_soil": "Well-drained loam",
        "grove_pollination": "Self-fertile",
        "grove_years_to_fruit": "3-4",
        "grove_chill_hours": "Not applicable",
        "description_ecommerce": (
            f"<p>{name}. Automated QA fixture for the volume-tier / promo-nudge "
            "Playwright specs (GOL-2463). Not a real plant — do not ship.</p>"
        ),
        "website_description": (
            "<p>Automated QA care guide placeholder. This template exists so the "
            "volume-tier specs have a Plants-categorised product to buy.</p>"
        ),
        "grove_guide_ready": True,
        "grove_facts_reviewed": True,
    }


# Fields in `want` that Odoo's read() returns as a many2one pair [id, name].
_M2O_WANT_FIELDS = frozenset({"categ_id"})

# Gate fields stored as HTML; "set" means non-blank once tags are stripped, the
# same test Odoo's own gate uses (product_template.py::_html_is_blank).
_HTML_GATE_FIELDS = frozenset({"description_ecommerce", "website_description"})


def _html_is_blank(value) -> bool:
    """True when an HTML field holds no visible text (mirrors the Odoo gate)."""
    text = re.sub(r"<[^>]*>", "", str(value or ""))
    return not text.replace("&nbsp;", " ").strip()


def want_drift(cur: dict, want: dict) -> dict:
    """The subset of ``want`` whose stored value differs — pure, unit-tested.

    ``categ_id`` reads back as ``[id, display_name]``; comparing that pair to the
    bare id we want would report drift on EVERY run and rewrite the category
    forever, so many2one fields are compared on the id.
    """
    drift: dict[str, Any] = {}
    for key, value in want.items():
        current = cur.get(key)
        if key in _M2O_WANT_FIELDS:
            current = current[0] if current else False
        if current != value:
            drift[key] = value
    return drift


def missing_gate_content(cur: dict, content: dict) -> dict:
    """Only the listing-gate fields that are currently UNSET — pure, unit-tested.

    Gap-fill, never clobber: if an operator wrote a better description or ticked
    the sign-off by hand, a re-run leaves it alone; if a field got cleared, the
    fixture is repaired so it can still publish and stays out of the nightly
    incomplete-listing audit.
    """
    out: dict[str, Any] = {}
    for key, value in content.items():
        current = cur.get(key)
        if key in _HTML_GATE_FIELDS:
            is_set = not _html_is_blank(current)
        elif isinstance(value, bool):
            is_set = bool(current)
        elif isinstance(value, int):
            is_set = bool(current) and current > 0
        else:
            is_set = bool(str(current or "").strip())
        if not is_set:
            out[key] = value
    return out


# The fixtures — the single source of truth the E2E specs assert against.
# Each is a one-Format-value template so its default (only) variant is the
# in-stock line and the CTA renders "Add to Cart" on first paint.
#
# `categ_xmlid` is the volume-tier switch: None (the two original fixtures)
# leaves categ_id untouched, so they stay non-plants and their carts stay
# deterministic. Only the `plant` fixture is categorised — and therefore only it
# is subject to the listing-content gate, hence `gated`.
FIXTURES: list[dict[str, Any]] = [
    {
        "key": "potted",  # GOL-1148 — pickup-only leg
        "sku": os.getenv("E2E_POTTED_SKU", "E2E-POTTED-INSTOCK"),
        "name": "AAA QA E2E Potted Tree (automated test fixture)",
        "format_value": "Potted",
        "shipping_tier": "potted",  # farm-pickup only (GOL-1114) — ship submit 400s
        "tree_length": None,  # irrelevant for pickup; leave unset
        "categ_xmlid": None,  # NOT a plant — never earns a volume tier
        "gated": False,
        "qty": E2E_QTY,
    },
    {
        "key": "bareroot",  # GOL-1154 — shippable happy-path leg
        "sku": os.getenv("E2E_BAREROOT_SKU", "E2E-BAREROOT-INSTOCK"),
        "name": "AAA QA E2E Bareroot Tree (automated test fixture)",
        "format_value": "Bareroot",
        "shipping_tier": "bareroot",  # genuinely shippable -> Box Engine sizes a box
        "tree_length": E2E_TREE_LENGTH,  # required so shipping isn't $0 (breaker trips otherwise)
        "categ_xmlid": None,  # NOT a plant — keeps every existing gate cart tier-free
        "gated": False,
        "qty": E2E_QTY,
    },
    {
        "key": "plant",  # GOL-2463 — volume-tier / promo-nudge leg (Train #2)
        "sku": os.getenv("E2E_PLANT_SKU", "E2E-BAREROOT-PLANT"),
        # The name carries TWO hard constraints, both load-bearing:
        #   * it MUST keep the "AAA QA E2E " prefix — grove-sites
        #     `E2E_FIXTURE_NAME_RE` (apps/nursery/e2e/qa-helpers.ts) excludes
        #     exactly that prefix from the catalog set, and these fixtures are
        #     seeded WITHOUT a photo, so anything else makes qa-photos.spec.ts
        #     fail on a "Photo coming soon" placeholder; and
        #   * it must sort LAST of the three under /shop's `name asc`
        #     ("Volume" > "Potted" > "Bareroot"), so findProductByCta still
        #     reaches the existing fixtures first and today's specs keep buying
        #     exactly what they buy today.
        "name": "AAA QA E2E Volume Tier Plant (automated test fixture)",
        "format_value": "Bareroot",
        "shipping_tier": "bareroot",
        "tree_length": E2E_TREE_LENGTH,
        "categ_xmlid": E2E_PLANT_CATEG,  # under Plants -> is_qualifying_plant() True
        "gated": True,  # ... which also means the listing-content gate applies
        "qty": E2E_PLANT_QTY,
    },
]


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def guard_environment() -> None:
    """Refuse a LIVE run unless the target is known-QA (GOL-1310).

    A live run is allowed only when BOTH the URL host is in ``QA_HOSTS`` AND the
    DB is in ``QA_DBS``. Anything else exits non-zero with a refusal, unless the
    operator passes ``--force-i-know-this-is-not-qa``. Dry runs are always
    allowed (read-only, no writes). Runs before any network call.
    """
    if DRY_RUN:
        return
    host = (_urlsplit(ODOO_URL).hostname or "").lower()
    host_ok = host in QA_HOSTS
    db_ok = ODOO_DB in QA_DBS
    if host_ok and db_ok:
        return
    if FORCE_NOT_QA:
        print(
            f"WARNING: {FORCE_FLAG} set — live seed against non-QA target host={host!r} db={ODOO_DB!r}. Proceeding.",
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
        "This script publishes buyable 'AAA …' fixtures that sort first in /shop. "
        f"Point it at QA, run with DRY_RUN=1, or pass {FORCE_FLAG} if you are "
        "certain this is not production."
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


def find_or_create(models, uid, model: str, domain: list, vals: dict, label: str) -> int:
    ids = call(models, uid, model, "search", [domain], {"limit": 1})
    if ids:
        print(f"  = {model} '{label}' exists (id={ids[0]})")
        return ids[0]
    if DRY_RUN:
        print(f"  + WOULD CREATE {model} '{label}'")
        return 0
    new_id = call(models, uid, model, "create", [vals])
    print(f"  + created {model} '{label}' (id={new_id})")
    return new_id


def resolve_sale_taxes(models, uid, company_id: int) -> list[int]:
    """Branch-aware sale-tax resolution (mirrors seed_variety_products.py).

    The nursery can be a *branch* whose sale taxes live on a parent company, so
    resolve each expected tax to the nearest company up the parent chain.
    """
    chain = []
    cid = company_id
    while cid:
        chain.append(cid)
        parent = call(models, uid, "res.company", "read", [[cid], ["parent_id"]])[0]["parent_id"]
        cid = parent[0] if parent else False
    tax_ids: list[int] = []
    for name in SALE_TAXES:
        matches = call(
            models,
            uid,
            "account.tax",
            "search_read",
            [[("name", "=", name), ("type_tax_use", "=", "sale"), ("company_id", "in", chain)]],
            {"fields": ["id", "company_id"]},
        )
        by = {t["company_id"][0]: t["id"] for t in matches if t.get("company_id")}
        chosen = next((by[c] for c in chain if c in by), None)
        tax_ids.append(chosen)
    if len(tax_ids) != len(SALE_TAXES) or any(t is None for t in tax_ids):
        fail(f"Expected sale taxes {SALE_TAXES} for company {company_id} (chain {chain}); got {tax_ids}")
    return tax_ids


def resolve_xmlid(models, uid, xmlid: str, expected_model: str) -> int:
    """Resolve ``module.name`` to a res_id, failing loudly if it is missing.

    Read straight off ``ir.model.data`` rather than ``check_object_reference``:
    the plain search_read is available to any RPC user, and a missing row here
    means grove_headless isn't installed/upgraded on the target — which we want
    as a hard error, not a silently uncategorised fixture.
    """
    if "." not in xmlid:
        fail(f"xmlid {xmlid!r} must be 'module.name'")
    module, name = xmlid.split(".", 1)
    rows = call(
        models,
        uid,
        "ir.model.data",
        "search_read",
        [[("module", "=", module), ("name", "=", name)]],
        {"fields": ["model", "res_id"], "limit": 1},
    )
    if not rows:
        fail(f"xmlid {xmlid!r} not found on this database (is grove_headless installed and upgraded?)")
    if rows[0]["model"] != expected_model:
        fail(f"xmlid {xmlid!r} points at {rows[0]['model']}, expected {expected_model}")
    return rows[0]["res_id"]


def resolve_plant_categ(models, uid, xmlid: str) -> int:
    """Resolve the plant fixture's category AND prove it is under Plants.

    ``is_qualifying_plant()`` compares materialised ``parent_path`` prefixes, so
    a category that merely *looks* plant-ish (or a Plants tree that got re-
    parented) would seed a fixture that still counts 0 qualifying units — the
    exact GOL-2463 failure this script exists to prevent. Check it here, before
    anything is written, instead of discovering it in a red e2e run.
    """
    categ_id = resolve_xmlid(models, uid, xmlid, "product.category")
    plants_root_id = resolve_xmlid(models, uid, PLANTS_ROOT_XMLID, "product.category")
    paths = call(
        models,
        uid,
        "product.category",
        "read",
        [[categ_id, plants_root_id], ["complete_name", "parent_path"]],
    )
    by_id = {row["id"]: row for row in paths}
    categ, root = by_id[categ_id], by_id[plants_root_id]
    if not categ.get("parent_path") or not root.get("parent_path"):
        fail(f"category {xmlid!r} or the Plants root has no parent_path; cannot verify plant membership")
    if not categ["parent_path"].startswith(root["parent_path"]):
        fail(
            f"category {xmlid!r} ({categ['complete_name']!r}, path {categ['parent_path']}) is NOT under "
            f"{PLANTS_ROOT_XMLID} (path {root['parent_path']}); the fixture could never earn a volume tier"
        )
    print(f"  = plant category {xmlid} -> {categ['complete_name']!r} (id={categ_id}, under Plants)")
    return categ_id


def probe_volume_tiers(url: str, template_id: int, variant_id: int, qty: int) -> dict:
    """POST one cart to the storefront BFF's /api/cart/tiers and return the body.

    This is the GOL-2463 done-criterion, executable: a fixture that is not under
    Plants answers ``qualifyingUnits: 0`` no matter how many units are in the
    cart, which is precisely the bug. Read-only — the endpoint prices a
    hypothetical cart and creates nothing.
    """
    payload = {"items": [{"variantId": variant_id, "templateId": template_id, "quantity": qty}]}
    req = _ureq.Request(url, data=_json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with _ureq.urlopen(req, timeout=30) as resp:
        return _json.loads(resp.read())


def apply_stock(models, uid, ctx, variant_id: int, location_id: int, qty: float) -> None:
    """Idempotently set on-hand ``qty`` for ``variant_id`` at ``location_id``.

    Look up the existing quant and WRITE ``inventory_quantity`` to the fixed
    target (converges regardless of prior test churn) instead of blind-creating,
    then apply. The write must carry ``inventory_mode`` context or Odoo's quant
    access rule rejects it. ``action_apply_inventory`` returns None, which the
    XML-RPC marshaller rejects, so it goes over JSON-RPC (mirrors
    seed_variety_products).
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


def seed_fixture(models, uid, ctx, company_id, tax_ids, stock_location_id, format_attr, spec, categ_id=None):
    """Create or reconcile one single-Format fixture template + its stock.

    Returns ``(template_id, variant_id)`` on a live run, or ``None`` on a dry run
    (nothing was resolved, so there is nothing to hand to the tier probe).
    """
    sku = spec["sku"]
    qty = spec.get("qty", E2E_QTY)
    print(f"\n════ Fixture: {spec['key']} ({sku!r}) ════")

    format_value_id = find_or_create(
        models,
        uid,
        "product.attribute.value",
        [("name", "=", spec["format_value"]), ("attribute_id", "=", format_attr)],
        {"name": spec["format_value"], "attribute_id": format_attr},
        f"{FORMAT_ATTR}:{spec['format_value']}",
    )

    # Fields the E2E specs depend on; reconciled on an existing fixture so a
    # re-run converges even if a prior run (or a manual poke) drifted them.
    want: dict[str, Any] = {
        "name": spec["name"],
        "list_price": E2E_PRICE,
        "sale_ok": True,
        "purchase_ok": True,
        "is_published": True,
        "grove_shipping_tier": spec["shipping_tier"],
    }
    if spec["tree_length"] is not None:
        want["grove_tree_length"] = spec["tree_length"]
    if categ_id:
        want["categ_id"] = categ_id

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
        gate_fields = list(listing_gate_content(spec["name"])) if spec.get("gated") else []
        cur = call(models, uid, "product.template", "read", [[tmpl_id], list(want) + ["taxes_id"] + gate_fields])[0]
        # categ_id reads back as [id, display_name]; compare on the id or every
        # run would "drift" and rewrite it forever.
        drift = want_drift(cur, want)
        # A gated fixture only has its listing content GAP-FILLED: whatever an
        # operator improved by hand survives, and an unset field is restored so
        # the fixture can still publish / stays out of the nightly audit.
        if gate_fields:
            drift.update(missing_gate_content(cur, listing_gate_content(spec["name"])))
        # Sale taxes are a m2m; reconcile them to the resolved set if they differ
        # (an existing fixture created outside this script may lack them).
        if sorted(cur.get("taxes_id", [])) != sorted(tax_ids):
            drift["taxes_id"] = [(6, 0, tax_ids)]
        if DRY_RUN:
            print(f"  = fixture exists (id={tmpl_id}); would reconcile {drift or 'nothing'}")
            # Read-only dry run: skip the variant default_code write + apply_stock
            # below (they mutate). Mirrors the create branch's early return so
            # DRY_RUN never touches Odoo, per the docstring's "Dry run (read-only)".
            return
        elif drift:
            call(models, uid, "product.template", "write", [[tmpl_id], drift], ctx)
            print(f"  ~ reconciled fixture (id={tmpl_id}) fields: {list(drift)}")
        else:
            print(f"  = fixture converged (id={tmpl_id}); nothing to reconcile")
    elif DRY_RUN:
        print(
            f"  + WOULD CREATE template {sku!r} ({spec['name']!r}) "
            f"[{spec['format_value']}-only, tier={spec['shipping_tier']}] @ ${E2E_PRICE:.2f}"
        )
        return
    else:
        vals: dict[str, Any] = {
            "name": spec["name"],
            "default_code": sku,
            "list_price": E2E_PRICE,
            "company_id": company_id,
            "type": "consu",
            "is_storable": True,
            "is_published": True,
            "sale_ok": True,
            "purchase_ok": True,
            "grove_shipping_tier": spec["shipping_tier"],
            "taxes_id": [(6, 0, tax_ids)],
            # One Format value only. No Cultivar axis -> exactly one variant, so
            # the detail page's default selection IS the in-stock variant and
            # the CTA renders "Add to Cart" on first paint.
            "attribute_line_ids": [(0, 0, {"attribute_id": format_attr, "value_ids": [(6, 0, [format_value_id])]})],
            "description_sale": (
                f"Automated QA E2E test fixture ({spec['key']}, GOL-1148/GOL-1154). "
                f"Guarantees one in-stock {spec['format_value'].lower()} product so the "
                "Playwright checkout suite can add a purchasable line. Safe to archive "
                "when the E2E fixture is retired."
            ),
        }
        if spec["tree_length"] is not None:
            vals["grove_tree_length"] = spec["tree_length"]
        if categ_id:
            vals["categ_id"] = categ_id
        if spec.get("gated"):
            # MUST be in the same create as is_published=True: create() runs the
            # publish gate on the finished record, so a two-step "create then
            # fill" would be refused on step one.
            vals.update(listing_gate_content(spec["name"]))
        tmpl_id = call(models, uid, "product.template", "create", [vals], ctx)
        print(f"  + created template {sku} -> id={tmpl_id}")

    variants = call(
        models,
        uid,
        "product.product",
        "search_read",
        [[("product_tmpl_id", "=", tmpl_id)]],
        {"fields": ["id", "display_name", "default_code"]},
    )
    if len(variants) != 1:
        fail(
            f"Expected exactly 1 variant for the {spec['format_value']}-only fixture, found {len(variants)}: {variants}"
        )
    variant = variants[0]
    variant_id = variant["id"]
    # The single variant's default_code MUST equal the fixture SKU: on a one-
    # variant template Odoo's ``product.template.default_code`` is a related
    # mirror of the variant's, and that template field is what this script
    # searches on to stay idempotent. Renaming the variant would move the
    # template's code too and fork a duplicate on the next run.
    if variant["default_code"] != sku:
        call(models, uid, "product.product", "write", [[variant_id], {"default_code": sku}])
        print(f"  ~ variant {variant_id} default_code -> {sku}")
    else:
        print(f"  = variant {variant_id} default_code {sku} ok")

    apply_stock(models, uid, ctx, variant_id, stock_location_id, float(qty))

    on_hand = call(models, uid, "product.product", "read", [[variant_id], ["qty_available"]])[0]["qty_available"]
    print(f"  stock: variant {sku} on hand = {on_hand} @ location {stock_location_id}")
    if on_hand < 1:
        fail(f"Post-apply on-hand is {on_hand}; fixture would still render 'Sold out'")
    print(
        f"  Done: {spec['key']} template id={tmpl_id}, variant id={variant_id} "
        f"({on_hand} on hand). /shop/{tmpl_id} should render an enabled 'Add to Cart'."
    )
    return tmpl_id, variant_id


def verify_plant_tiers(tmpl_id: int, variant_id: int) -> None:
    """Prove the plant fixture earns a tier, through the real storefront BFF.

    The GOL-2463 done-criterion. ``qualifyingUnits == 0`` means the fixture is
    still not counted as a plant (wrong category, module not upgraded, tier feed
    scoped elsewhere) — that is a FAILURE, not a warning, because the volume-tier
    specs would then be asserting against a silently non-qualifying cart.
    """
    print(f"\n── Volume-tier probe ── POST {E2E_TIERS_URL} (qty {E2E_TIERS_QTY})")
    try:
        body = probe_volume_tiers(E2E_TIERS_URL, tmpl_id, variant_id, E2E_TIERS_QTY)
    except Exception as exc:  # noqa: BLE001 — any failure here must be loud
        fail(f"tier probe against {E2E_TIERS_URL} failed: {exc}")
    units = body.get("qualifyingUnits")
    print(f"  qualifyingUnits={units}  tiers={body.get('tiers')}")
    if not units:
        fail(
            f"tier probe returned qualifyingUnits={units!r} for {E2E_TIERS_QTY} units of variant "
            f"{variant_id}: the fixture is still NOT a qualifying plant (GOL-2463)."
        )
    print(f"  OK: {E2E_TIERS_QTY} units counted as {units} qualifying units.")


def main() -> None:
    only = os.getenv("FIXTURE", "").strip().lower()
    specs = [f for f in FIXTURES if not only or f["key"] == only]
    if only and not specs:
        fail(f"FIXTURE={only!r} matches no fixture; choose one of {[f['key'] for f in FIXTURES]}")
    print(
        f"Target: {ODOO_URL} db={ODOO_DB} company={COMPANY_NAME}  "
        f"fixtures={[f['sku'] for f in specs]} qty={E2E_QTY} price=${E2E_PRICE:.2f}  "
        f"DRY_RUN={'yes' if DRY_RUN else 'NO — LIVE'}"
    )
    guard_environment()
    models, uid = authenticate()

    company_ids = call(models, uid, "res.company", "search", [[("name", "=", COMPANY_NAME)]], {"limit": 1})
    if not company_ids:
        fail(f"Company '{COMPANY_NAME}' not found")
    company_id = company_ids[0]
    ctx = {"context": {"allowed_company_ids": [company_id], "company_id": company_id}}

    tax_ids = resolve_sale_taxes(models, uid, company_id)

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
    stock_location_id = wh[0]["lot_stock_id"][0]

    print("\n── Format axis ──")
    format_attr = find_or_create(
        models,
        uid,
        "product.attribute",
        [("name", "=", FORMAT_ATTR)],
        {"name": FORMAT_ATTR, "display_type": "radio", "create_variant": "always"},
        FORMAT_ATTR,
    )

    seeded: dict[str, tuple[int, int]] = {}
    for spec in specs:
        categ_id = None
        if spec.get("categ_xmlid"):
            print(f"\n── Plant category for {spec['key']} ──")
            categ_id = resolve_plant_categ(models, uid, spec["categ_xmlid"])
        ids = seed_fixture(models, uid, ctx, company_id, tax_ids, stock_location_id, format_attr, spec, categ_id)
        if ids:
            seeded[spec["key"]] = ids

    print(f"\nDone. Seeded {len(specs)} fixture(s): {[f['sku'] for f in specs]}")
    for key, (tmpl_id, variant_id) in seeded.items():
        # Printed so the ids can be pasted straight into the e2e spec / issue.
        print(f"  ids: {key} templateId={tmpl_id} variantId={variant_id}")

    if E2E_TIERS_URL and "plant" in seeded:
        verify_plant_tiers(*seeded["plant"])
    elif E2E_TIERS_URL:
        print("E2E_TIERS_URL set but the plant fixture was not seeded this run — skipping the tier probe.")


if __name__ == "__main__":
    main()
