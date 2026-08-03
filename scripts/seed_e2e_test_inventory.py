#!/usr/bin/env python3
"""Seed the QA Playwright E2E **test-inventory fixture** (GOL-1148).

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

What this fixture guarantees
----------------------------
A single **Potted-ONLY** template with stock on hand, so its *default* (only)
variant renders **"Add to Cart"** on first paint with no dropdown interaction:

  * ``sale_ok = True`` + ``website_published = True``  -> appears in ``/shop``,
    purchasable (not a "coming soon" placeholder).
  * one ``Format = Potted`` value, no Bareroot sibling  -> default selection is
    the in-stock potted variant -> ``buyStateFor`` mode ``in-stock`` -> CTA
    "Add to Cart" (grove-sites lib/buy-state.ts).
  * ``grove_shipping_tier`` default "potted"           -> exercises the potted
    pickup-fulfillment path (GOL-1057/1114), which is what the happy-pay specs
    want.

The ``Reserve`` (bareroot preorder) and ``Coming soon`` states the other specs
need are ALREADY present in the real catalog, so this fixture only adds the one
missing state.

Determinism
-----------
* Matched by its own ``default_code`` (``E2E_SKU``), so a converged fixture is a
  no-op re-run. On an existing template it **reconciles** the fields the specs
  depend on (published / sale_ok / list_price / format axis) rather than
  forking a duplicate.
* Named ``AAA ...`` by default so it sorts FIRST under the ``/shop`` grid's
  ``name asc`` order and lands well within the helper's first-24 scan window.
* Given NO ``public_categ_ids``, so it never inflates a ``?cat=<slug>`` facet
  count (keeps the catalog-browse specs' facet targets honest); it still shows
  in the unfiltered grid the helper scans.
* Stock is set idempotently: the variant's quant is looked up and its
  ``inventory_quantity`` is **written to a fixed target** (not blind-created),
  so a re-run converges to ``E2E_QTY`` on hand regardless of prior test churn.

Coordinate the exact product identity / price / stock level with Ada (E2E spec
fixture design, GOL-1074) via the env knobs below — the defaults are sensible
but the SKU/name/price/qty are the single source of truth the specs can assert
against.

Usage
-----
    # Dry run (read-only): resolves company/warehouse/axis, reports the plan.
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com \\
    ODOO_DB=odoo \\
    ODOO_USER=josh@goldberrygrove.farm \\
    ODOO_PASSWORD=<admin-or-api-key> \\
    DRY_RUN=1 python3 scripts/seed_e2e_test_inventory.py

    # Live: DRY_RUN unset -> creates/reconciles the fixture and applies stock.

Knobs (env, all optional):
    E2E_SKU   default "E2E-POTTED-INSTOCK"   the fixture's default_code
    E2E_NAME  default "AAA QA E2E Potted Tree (automated test fixture)"
    E2E_PRICE default "42.00"                list_price (USD)
    E2E_QTY   default "50"                   on-hand target for the potted variant

Exit codes: 0 ok, 1 auth/data failure (fails loudly).
"""

from __future__ import annotations

import json as _json
import os
import sys
import urllib.request as _ureq
import xmlrpc.client
from typing import Any

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "Goldberry")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
DRY_RUN = os.getenv("DRY_RUN") == "1"

COMPANY_NAME = "At The Grove Nursery"
SALE_TAXES = ["WV State Sales Tax 6%", "WV Municipal Tax 1%"]
FORMAT_ATTR = "Format"
FORMAT_VALUE = "Potted"

# Fixture identity — the single source of truth the E2E specs assert against.
# Overridable so Ada can pin exact values from the spec design (GOL-1074).
E2E_SKU = os.getenv("E2E_SKU", "E2E-POTTED-INSTOCK")
E2E_NAME = os.getenv("E2E_NAME", "AAA QA E2E Potted Tree (automated test fixture)")
E2E_PRICE = float(os.getenv("E2E_PRICE", "42.00"))
E2E_QTY = int(os.getenv("E2E_QTY", "50"))


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


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
    then apply. ``action_apply_inventory`` returns None, which the XML-RPC
    marshaller rejects, so it goes over JSON-RPC (mirrors seed_variety_products).
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


def main() -> None:
    print(
        f"Target: {ODOO_URL} db={ODOO_DB} company={COMPANY_NAME}  "
        f"fixture={E2E_SKU!r} qty={E2E_QTY} price=${E2E_PRICE:.2f}  "
        f"DRY_RUN={'yes' if DRY_RUN else 'NO — LIVE'}"
    )
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
    format_value_id = find_or_create(
        models,
        uid,
        "product.attribute.value",
        [("name", "=", FORMAT_VALUE), ("attribute_id", "=", format_attr)],
        {"name": FORMAT_VALUE, "attribute_id": format_attr},
        f"{FORMAT_ATTR}:{FORMAT_VALUE}",
    )

    print("\n── Fixture template ──")
    # Fields the E2E specs depend on; reconciled on an existing fixture so a
    # re-run converges even if a prior run (or a manual poke) drifted them.
    want = {
        "name": E2E_NAME,
        "list_price": E2E_PRICE,
        "sale_ok": True,
        "purchase_ok": True,
        "is_published": True,
    }
    existing = call(
        models,
        uid,
        "product.template",
        "search",
        [[("default_code", "=", E2E_SKU), ("company_id", "in", [company_id, False])]],
        {"limit": 1},
    )
    if existing:
        tmpl_id = existing[0]
        cur = call(models, uid, "product.template", "read", [[tmpl_id], list(want)])[0]
        drift = {k: v for k, v in want.items() if cur.get(k) != v}
        if DRY_RUN:
            print(f"  = fixture exists (id={tmpl_id}); would reconcile {drift or 'nothing'}")
        elif drift:
            call(models, uid, "product.template", "write", [[tmpl_id], drift], ctx)
            print(f"  ~ reconciled fixture (id={tmpl_id}) fields: {drift}")
        else:
            print(f"  = fixture converged (id={tmpl_id}); nothing to reconcile")
    elif DRY_RUN:
        print(f"  + WOULD CREATE template {E2E_SKU!r} ({E2E_NAME!r}) [Potted-only] @ ${E2E_PRICE:.2f}")
        print("\nDone. (dry run — nothing written)")
        return
    else:
        vals: dict[str, Any] = {
            "name": E2E_NAME,
            "default_code": E2E_SKU,
            "list_price": E2E_PRICE,
            "company_id": company_id,
            "type": "consu",
            "is_storable": True,
            "is_published": True,
            "sale_ok": True,
            "purchase_ok": True,
            "taxes_id": [(6, 0, tax_ids)],
            # One Format value only (Potted). No Cultivar axis -> exactly one
            # variant, so the detail page's default selection IS the in-stock
            # potted variant and the CTA renders "Add to Cart" on first paint.
            "attribute_line_ids": [(0, 0, {"attribute_id": format_attr, "value_ids": [(6, 0, [format_value_id])]})],
            "description_sale": (
                "Automated QA E2E test fixture (GOL-1148). Guarantees one in-stock "
                "potted product so the Playwright checkout suite can add a "
                "purchasable line. Safe to archive when the E2E fixture is retired."
            ),
        }
        tmpl_id = call(models, uid, "product.template", "create", [vals], ctx)
        print(f"  + created template {E2E_SKU} -> id={tmpl_id}")

    print("\n── Variant + stock ──")
    variants = call(
        models,
        uid,
        "product.product",
        "search_read",
        [[("product_tmpl_id", "=", tmpl_id)]],
        {"fields": ["id", "display_name", "default_code"]},
    )
    if len(variants) != 1:
        fail(f"Expected exactly 1 variant for the Potted-only fixture, found {len(variants)}: {variants}")
    variant = variants[0]
    variant_id = variant["id"]
    # The single variant's default_code MUST equal E2E_SKU: on a one-variant
    # template Odoo's ``product.template.default_code`` is a related mirror of
    # the variant's, and that template field is what this script searches on to
    # stay idempotent. Renaming the variant (e.g. a "-PT" suffix) would move the
    # template's code too and fork a duplicate on the next run.
    if variant["default_code"] != E2E_SKU:
        call(models, uid, "product.product", "write", [[variant_id], {"default_code": E2E_SKU}])
        print(f"  ~ variant {variant_id} default_code -> {E2E_SKU}")
    else:
        print(f"  = variant {variant_id} default_code {E2E_SKU} ok")

    apply_stock(models, uid, ctx, variant_id, stock_location_id, float(E2E_QTY))

    on_hand = call(models, uid, "product.product", "read", [[variant_id], ["qty_available"]])[0]["qty_available"]
    print(f"  stock: variant {E2E_SKU} on hand = {on_hand} @ location {stock_location_id}")
    if on_hand < 1:
        fail(f"Post-apply on-hand is {on_hand}; fixture would still render 'Sold out'")

    print(
        f"\nDone. Fixture template id={tmpl_id}, variant id={variant_id} "
        f"({on_hand} on hand). The /shop/{tmpl_id} detail page should render an "
        f"enabled 'Add to Cart' on first paint."
    )


if __name__ == "__main__":
    main()
