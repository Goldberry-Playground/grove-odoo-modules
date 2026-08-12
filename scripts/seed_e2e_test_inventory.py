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

The ``Reserve`` (bareroot preorder) and ``Coming soon`` states the other specs
need are ALREADY present in the real catalog, so these fixtures only add the two
missing "enabled Add to Cart" states (pickup + shippable).

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

Safety: QA-only, dry-run by DEFAULT
-----------------------------------
This script publishes ``is_published`` + ``sale_ok`` "AAA ..." products that
sort FIRST in ``/shop`` and are genuinely buyable/shippable — running it against
a live storefront drops fake purchasable trees at the top of the real grid
(GOL-1310). Two guards prevent that:

* **Dry run is the DEFAULT** (opt-out, unlike the sibling seed scripts): live
  mode requires an explicit ``DRY_RUN=0`` (``DRY_RUN=1``/unset both stay
  read-only).
* **Live mode refuses a non-QA target**: the ``ODOO_URL`` host must be a known
  QA/local host AND ``ODOO_DB`` must be a known QA DB, else the script exits
  non-zero before authenticating. Override only with the explicit
  ``--force-i-know-this-is-not-qa`` flag (you accept publishing test products to
  that target).

Usage
-----
    # Dry run (read-only, DEFAULT): resolves company/warehouse/axis, reports the
    # plan for BOTH fixtures. No DRY_RUN needed — read-only unless DRY_RUN=0.
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com \\
    ODOO_DB=odoo \\
    ODOO_USER=josh@goldberrygrove.farm \\
    ODOO_PASSWORD=<admin-or-api-key> \\
    python3 scripts/seed_e2e_test_inventory.py

    # Live: DRY_RUN=0 -> creates/reconciles both fixtures and applies stock.
    # Refused unless the target is a known QA host + DB (see Safety above).
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com ODOO_DB=odoo \\
    ODOO_USER=... ODOO_PASSWORD=... \\
    DRY_RUN=0 python3 scripts/seed_e2e_test_inventory.py

    # Seed only one fixture: FIXTURE=potted (or FIXTURE=bareroot).

Knobs (env, all optional):
    FIXTURE           default "" (both)      "potted" | "bareroot" to seed one
    E2E_POTTED_SKU    default "E2E-POTTED-INSTOCK"
    E2E_BAREROOT_SKU  default "E2E-BAREROOT-INSTOCK"
    E2E_PRICE         default "42.00"        list_price (USD), both fixtures
    E2E_QTY           default "50"           on-hand target, both fixtures
    E2E_TREE_LENGTH   default "20"           grove_tree_length for the bareroot
                                             (shippable) fixture: 16|20|32|46

Exit codes: 0 ok, 1 auth/data failure or a refused non-QA live target (fails loudly).
"""

from __future__ import annotations

import json as _json
import os
import sys
import urllib.request as _ureq
import xmlrpc.client
from typing import Any
from urllib.parse import urlparse


def _env_flag(name: str, default: bool) -> bool:
    """Truthy env parse (``1/true/yes/on``); returns ``default`` if unset."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
# QA DB is ``odoo``. NOT the prod-style "Goldberry" — a prod-shaped default is
# how a stray localhost tunnel published buyable fixtures to a live store (GOL-1310).
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
# Dry run is the DEFAULT (opt-out): live mode requires an explicit DRY_RUN=0.
# DRY_RUN unset OR DRY_RUN=1 both stay read-only. This diverges from the sibling
# seed scripts on purpose — this one publishes buyable storefront products.
DRY_RUN = _env_flag("DRY_RUN", True)

# Live mode is refused unless the target is provably QA: URL host in the QA/local
# allowlist AND DB in the QA-DB allowlist. Override with FORCE_NON_QA_FLAG only.
FORCE_NON_QA_FLAG = "--force-i-know-this-is-not-qa"
QA_URL_HOSTS = {"localhost", "127.0.0.1", "odoo.qa.gatheringatthegrove.com"}
QA_DBS = {"odoo"}

COMPANY_NAME = "At The Grove Nursery"
SALE_TAXES = ["WV State Sales Tax 6%", "WV Municipal Tax 1%"]
FORMAT_ATTR = "Format"

E2E_PRICE = float(os.getenv("E2E_PRICE", "42.00"))
E2E_QTY = int(os.getenv("E2E_QTY", "50"))
# grove_tree_length is a selection on product.template; only these ship (16|20|32|46).
E2E_TREE_LENGTH = os.getenv("E2E_TREE_LENGTH", "20")

# The fixtures — the single source of truth the E2E specs assert against.
# Each is a one-Format-value template so its default (only) variant is the
# in-stock line and the CTA renders "Add to Cart" on first paint.
FIXTURES: list[dict[str, Any]] = [
    {
        "key": "potted",  # GOL-1148 — pickup-only leg
        "sku": os.getenv("E2E_POTTED_SKU", "E2E-POTTED-INSTOCK"),
        "name": "AAA QA E2E Potted Tree (automated test fixture)",
        "format_value": "Potted",
        "shipping_tier": "potted",  # farm-pickup only (GOL-1114) — ship submit 400s
        "tree_length": None,  # irrelevant for pickup; leave unset
    },
    {
        "key": "bareroot",  # GOL-1154 — shippable happy-path leg
        "sku": os.getenv("E2E_BAREROOT_SKU", "E2E-BAREROOT-INSTOCK"),
        "name": "AAA QA E2E Bareroot Tree (automated test fixture)",
        "format_value": "Bareroot",
        "shipping_tier": "bareroot",  # genuinely shippable -> Box Engine sizes a box
        "tree_length": E2E_TREE_LENGTH,  # required so shipping isn't $0 (breaker trips otherwise)
    },
]


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def assert_qa_target_or_die(force: bool) -> None:
    """Refuse a LIVE seed unless the target is provably QA (GOL-1310).

    Requires ODOO_URL host in ``QA_URL_HOSTS`` **and** ODOO_DB in ``QA_DBS``.
    Anything else exits non-zero (this script publishes buyable "AAA ..."
    products to ``/shop``) unless the operator passes ``FORCE_NON_QA_FLAG`` to
    explicitly accept the target. No-op in DRY_RUN — nothing is written there.
    """
    host = (urlparse(ODOO_URL).hostname or "").lower()
    problems = []
    if host not in QA_URL_HOSTS:
        problems.append(f"host {host!r} not in QA allowlist {sorted(QA_URL_HOSTS)}")
    if ODOO_DB not in QA_DBS:
        problems.append(f"db {ODOO_DB!r} not in QA allowlist {sorted(QA_DBS)}")
    if not problems:
        return
    detail = "; ".join(problems)
    if force:
        print(
            f"WARNING: live seed target is NOT provably QA ({detail}); proceeding "
            f"because {FORCE_NON_QA_FLAG} was passed.",
            file=sys.stderr,
        )
        return
    fail(
        f"Refusing LIVE seed against a non-QA target: {detail}. This script "
        "publishes buyable 'AAA ...' test products that sort first in /shop — run "
        f"it only against QA. Set DRY_RUN=1 to preview, or pass {FORCE_NON_QA_FLAG} "
        "to override (you accept publishing test products to this target)."
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


def seed_fixture(models, uid, ctx, company_id, tax_ids, stock_location_id, format_attr, spec) -> None:
    """Create or reconcile one single-Format fixture template + its stock."""
    sku = spec["sku"]
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
        cur = call(models, uid, "product.template", "read", [[tmpl_id], list(want) + ["taxes_id"]])[0]
        drift = {k: v for k, v in want.items() if cur.get(k) != v}
        # Sale taxes are a m2m; reconcile them to the resolved set if they differ
        # (an existing fixture created outside this script may lack them).
        if sorted(cur.get("taxes_id", [])) != sorted(tax_ids):
            drift["taxes_id"] = [(6, 0, tax_ids)]
        if DRY_RUN:
            print(f"  = fixture exists (id={tmpl_id}); would reconcile {drift or 'nothing'}")
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

    apply_stock(models, uid, ctx, variant_id, stock_location_id, float(E2E_QTY))

    on_hand = call(models, uid, "product.product", "read", [[variant_id], ["qty_available"]])[0]["qty_available"]
    print(f"  stock: variant {sku} on hand = {on_hand} @ location {stock_location_id}")
    if on_hand < 1:
        fail(f"Post-apply on-hand is {on_hand}; fixture would still render 'Sold out'")
    print(
        f"  Done: {spec['key']} template id={tmpl_id}, variant id={variant_id} "
        f"({on_hand} on hand). /shop/{tmpl_id} should render an enabled 'Add to Cart'."
    )


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
    if not DRY_RUN:
        assert_qa_target_or_die(force=FORCE_NON_QA_FLAG in sys.argv[1:])
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

    for spec in specs:
        seed_fixture(models, uid, ctx, company_id, tax_ids, stock_location_id, format_attr, spec)

    print(f"\nDone. Seeded {len(specs)} fixture(s): {[f['sku'] for f in specs]}")


if __name__ == "__main__":
    main()
