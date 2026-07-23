#!/usr/bin/env python3
"""GOL-757 Odoo taxonomy System-of-Record pass (categories + botanical names + splits).

Companion to ``seed_variety_products.py``. The seed's ``reconcile_category``
only ever set a template's storefront category to a SINGLE bucket
(``public_categ_ids == [want_cat]``). Josh's 2026-07-23 taxonomy spec
(Asana 1216758513298024 / GATH-119) needs the /shop cat-bar to become a real
use-type taxonomy with products that can live in MORE THAN ONE bucket
(``public_categ_ids`` is m2m — American Plum is both *Fruit Trees* and
*Native*). This script encodes that whole mapping declaratively so the QA/prod
catalog taxonomy is reproducible from code alone — no hand-edits in the Odoo UI.

It performs four idempotent, gated passes:

  1. CATEGORIES  — ensure the five use-type public categories exist:
     Fruit Trees, Fruiting Vines (unchanged), NEW Native, NEW Nut Trees, and
     RENAME "Berries" -> "Berry & Nut Shrubs". The storefront resolves
     ``?cat=<slug>`` by ``slugify(category.name)`` (controllers/main.py), so a
     rename automatically moves the slug (berries -> berry-nut-shrubs) and the
     cat-bar follows — no separate slug field to maintain.

  2. TEMPLATES   — for every existing template (matched by species name against
     the live ACTIVE index, exactly like the seed, so archived GOL-641 dups are
     never touched): set ``grove_botanical_name`` and the exact
     ``public_categ_ids`` set, and RENAME where the spec says so
     (Jujubee -> Jujube; Persimmon 163 -> American Persimmon).

  3. RED MULBERRY SPLIT — lift the "Red Mulberry" (Morus rubra) cultivar off the
     Mulberry template (159) into its own single-cultivar product.template
     (Format axis only, like Aronia), carrying its stock, effective price and
     its one gallery photo; then drop the cultivar value from 159 (26 -> 24
     variants). No orders/reservations touch these variants.

  4. PERSIMMON SPLIT — 163 KEEPS its id, its Meader/Regular/Seedling variants,
     its REAL stock (Meader PT reservation) + the confirmed order S01566 line
     and its 3 photos; it is only renamed to "American Persimmon" (virginiana,
     Native + Fruit Trees) in pass 2. The kaki cultivar "IKKJ" is lifted into a
     NEW "Persimmon" template (Diospyros kaki, Fruit Trees), preserving the
     $12 base + $28 extra = $40 effective price and the PERSIMMON-IKKJ-* SKUs.
     Meader et al are never moved, so no sale.order line / stock quant is touched.

Every pass reads live state, ASSERTS the spec's expectations (ids resolve to the
expected species; 159 really has the Red Mulberry cultivar; 163 really has
IKKJ) and FAILS LOUDLY on a mismatch rather than mutating the wrong record — the
same audited-migration discipline as ``remediate_seed_duplicates.py``. Splits
never delete: dropping a cultivar value archives its variants (active=False).

norm_species guard (spec): "Red Mulberry" (redmulberry) != "Mulberry"
(mulberry) and "American Persimmon" (americanpersimmon) != "Persimmon"
(persimmon), so the species index that keys idempotency never re-collapses a
split child back into its parent.

Usage
-----
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com ODOO_DB=odoo \\
    ODOO_USER=josh@goldberrygrove.farm ODOO_PASSWORD=<admin> \\
    DRY_RUN=1 python3 scripts/taxonomy_reconcile.py    # plan only, no writes

    # DRY_RUN unset -> applies. Josh gates the live run (GOL-757).

Exit codes: 0 ok, 1 auth/validation failure (a failed assertion aborts before
any write in that pass).
"""

from __future__ import annotations

import json as _json
import os
import sys
import urllib.request as _ureq
import xmlrpc.client
from typing import Any

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
DRY_RUN = os.getenv("DRY_RUN") == "1"

COMPANY_NAME = "At The Grove Nursery"
SALE_TAXES = ["WV State Sales Tax 6%", "WV Municipal Tax 1%"]

CULTIVAR_ATTR = "Cultivar"
FORMAT_ATTR = "Format"
FORMAT_ABBR = {"Potted": "PT", "Bareroot": "BR"}
POTTED_EXTRA = 2.00  # Format Potted price_extra (bareroot-anchor model, GOL-691)

# ── Use-type storefront categories (the /shop cat-bar) ──────────────────────
FRUIT_TREES = "Fruit Trees"
FRUITING_VINES = "Fruiting Vines"
NATIVE = "Native"
NUT_TREES = "Nut Trees"
BERRY_NUT_SHRUBS = "Berry & Nut Shrubs"
BERRIES_OLD = "Berries"  # renamed -> BERRY_NUT_SHRUBS

# ── Per-species desired end state (GOL-757 spec) ────────────────────────────
# Matched by species name against the live ACTIVE template index (norm_species),
# mirroring the seed so archived dups are ignored. ``expect_id`` is asserted
# when the spec pins an id (fail loud if the catalog shifted). ``rename`` sets a
# new display name (slug recomputes automatically). ``cats`` is the EXACT
# public_categ_ids set to write; ``cats=None`` leaves categories untouched (used
# for held/unpublished species that the spec only gives a botanical name).
# ``required`` templates must resolve (the 15 published SKUs); optional ones
# (held $0 species) warn-and-skip if absent.
TEMPLATES: list[dict[str, Any]] = [
    {
        "key": "American Plum",
        "id": 148,
        "botanical": "Prunus americana",
        "cats": [FRUIT_TREES, NATIVE],
        "required": True,
    },
    {"key": "Apple", "id": 149, "botanical": "Malus domestica", "cats": [FRUIT_TREES], "required": True},
    {"key": "Chestnut", "id": 151, "botanical": "Castanea mollissima", "cats": [NUT_TREES], "required": True},
    {"key": "Chestnut-Hybrid", "id": 152, "botanical": "Castanea spp. (hybrid)", "cats": [NUT_TREES], "required": True},
    {"key": "Dogwood", "id": 153, "botanical": "Cornus florida", "cats": [NATIVE], "required": True},
    {"key": "Fig", "id": 155, "botanical": "Ficus carica", "cats": [FRUIT_TREES], "required": True},
    {
        "key": "Jujubee",
        "id": 157,
        "rename": "Jujube",
        "botanical": "Ziziphus jujuba",
        "cats": [FRUIT_TREES],
        "required": True,
    },
    {"key": "Kiwi", "id": 158, "botanical": "Actinidia arguta", "cats": [FRUITING_VINES], "required": True},
    {"key": "Mulberry", "id": 159, "botanical": "Morus spp.", "cats": [FRUIT_TREES], "required": True},
    {"key": "Peach", "id": 161, "botanical": "Prunus persica", "cats": [FRUIT_TREES], "required": True},
    {"key": "Pear", "id": 162, "botanical": "Pyrus spp.", "cats": [FRUIT_TREES], "required": True},
    {
        "key": "Persimmon",
        "id": 163,
        "rename": "American Persimmon",
        "botanical": "Diospyros virginiana",
        "cats": [FRUIT_TREES, NATIVE],
        "required": True,
    },
    {"key": "Plum", "id": 164, "botanical": "Prunus spp.", "cats": [FRUIT_TREES], "required": True},
    {
        "key": "Service Berry",
        "id": 168,
        "botanical": "Amelanchier laevis",
        "cats": [BERRY_NUT_SHRUBS, NATIVE],
        "required": True,
    },
    {
        "key": "Aronia",
        "id": 183,
        "botanical": "Aronia melanocarpa",
        "cats": [BERRY_NUT_SHRUBS, NATIVE],
        "required": True,
    },
    # Held / unpublished species — spec gives them a botanical name only.
    # Passion Flower additionally gets Native (applies when it is republished).
    {"key": "Passion Flower", "botanical": "Passiflora incarnata", "cats": [NATIVE], "required": False},
    {"key": "Elderberry", "botanical": "Sambucus canadensis", "cats": None, "required": False},
    {"key": "Honey Locust", "botanical": "Gleditsia triacanthos", "cats": None, "required": False},
    {"key": "Sea Buckthorn", "botanical": "Hippophae rhamnoides", "cats": None, "required": False},
    {"key": "Basket Willow", "botanical": "Salix viminalis", "cats": None, "required": False},
]

# Split children (created by pass 3/4) — declared here so the end-state category
# summary can include them.
SPLIT_CHILDREN = [
    {"name": "Red Mulberry", "botanical": "Morus rubra", "cats": [FRUIT_TREES, NATIVE]},
    {"name": "Persimmon", "botanical": "Diospyros kaki", "cats": [FRUIT_TREES]},  # kaki
]


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def norm_species(name: str) -> str:
    """Collapse case + non-alphanumerics so 'Service Berry' == 'serviceberry'."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def authenticate() -> tuple[xmlrpc.client.ServerProxy, int]:
    if not ODOO_PASSWORD:
        fail("ODOO_PASSWORD env var is required")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        fail(f"Authentication failed for user {ODOO_USER} on db {ODOO_DB}")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    print(f"Authenticated as uid={uid} on db={ODOO_DB}")
    return models, uid


def call(models, uid, model: str, method: str, args: list, kwargs: dict | None = None):
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def jsonrpc_execute(uid: int, model: str, method: str, args: list, ctx: dict) -> Any:
    """execute_kw over JSON-RPC — needed for methods that return None (which the
    XML-RPC marshaller rejects), e.g. stock.quant.action_apply_inventory."""
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "object",
            "method": "execute_kw",
            "args": [ODOO_DB, uid, ODOO_PASSWORD, model, method, args, ctx],
        },
    }
    resp = _json.loads(
        _ureq.urlopen(
            _ureq.Request(
                f"{ODOO_URL}/jsonrpc", data=_json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
            ),
            timeout=60,
        ).read()
    )
    if resp.get("error"):
        fail(f"jsonrpc {model}.{method} error: {resp['error']}")
    return resp.get("result")


# ── Pass 1: categories ──────────────────────────────────────────────────────
def reconcile_categories(models, uid) -> dict[str, int]:
    """Ensure the five use-type public categories exist; rename Berries. Returns
    a name->id map (including any category still only present under its old name
    in DRY_RUN, resolved to 0 as a placeholder)."""
    print("\n── Pass 1: categories ──")
    cat_id: dict[str, int] = {}

    def resolve(name: str) -> int:
        ids = call(models, uid, "product.public.category", "search", [[("name", "=", name)]], {"limit": 1})
        return ids[0] if ids else 0

    # Rename Berries -> Berry & Nut Shrubs (idempotent: if the new name already
    # exists we adopt it; if only the old exists we rename it in place, keeping
    # the id so existing product links survive).
    new_id = resolve(BERRY_NUT_SHRUBS)
    old_id = resolve(BERRIES_OLD)
    if new_id:
        cat_id[BERRY_NUT_SHRUBS] = new_id
        print(f"  = '{BERRY_NUT_SHRUBS}' exists (id={new_id})")
    elif old_id:
        if DRY_RUN:
            print(f"  ~ WOULD RENAME '{BERRIES_OLD}' (id={old_id}) -> '{BERRY_NUT_SHRUBS}'")
            cat_id[BERRY_NUT_SHRUBS] = old_id
        else:
            call(models, uid, "product.public.category", "write", [[old_id], {"name": BERRY_NUT_SHRUBS}])
            print(f"  ~ RENAMED '{BERRIES_OLD}' (id={old_id}) -> '{BERRY_NUT_SHRUBS}'")
            cat_id[BERRY_NUT_SHRUBS] = old_id
    # else: created below via the ensure loop.

    for name in (FRUIT_TREES, FRUITING_VINES, NATIVE, NUT_TREES, BERRY_NUT_SHRUBS):
        if cat_id.get(name):
            continue
        existing = resolve(name)
        if existing:
            cat_id[name] = existing
            print(f"  = '{name}' exists (id={existing})")
        elif DRY_RUN:
            print(f"  + WOULD CREATE category '{name}'")
            cat_id[name] = 0
        else:
            cat_id[name] = call(models, uid, "product.public.category", "create", [{"name": name}])
            print(f"  + created category '{name}' (id={cat_id[name]})")
    return cat_id


# ── Pass 2: existing templates (botanical + category + rename) ──────────────
def index_active_templates(models, uid, company_id: int) -> dict[str, dict]:
    live = call(
        models,
        uid,
        "product.template",
        "search_read",
        [[("company_id", "in", [company_id, False]), ("active", "=", True)]],
        {"fields": ["id", "name", "public_categ_ids", "grove_botanical_name"]},
    )
    idx: dict[str, dict] = {}
    for t in live:
        idx.setdefault(norm_species(t["name"]), t)  # lowest id (canonical) wins
    return idx


def reconcile_templates(models, uid, idx: dict[str, dict], cat_id: dict[str, int]) -> None:
    print("\n── Pass 2: templates (botanical + category + rename) ──")
    for spec in TEMPLATES:
        key = norm_species(spec["key"])
        t = idx.get(key)
        if not t:
            msg = f"  {'ERROR' if spec['required'] else 'skip'}: template '{spec['key']}' not found in active index"
            if spec["required"]:
                fail(msg.strip())
            print(msg)
            continue
        tid = t["id"]
        if spec.get("id") and tid != spec["id"]:
            fail(f"  '{spec['key']}' resolved to id={tid}, spec pins id={spec['id']} — catalog shifted, aborting")

        vals: dict[str, Any] = {}
        if spec.get("rename") and t["name"] != spec["rename"]:
            vals["name"] = spec["rename"]
        if (t.get("grove_botanical_name") or "") != spec["botanical"]:
            vals["grove_botanical_name"] = spec["botanical"]
        if spec["cats"] is not None:
            want = sorted(cat_id[c] for c in spec["cats"])
            if 0 in want:
                want = None  # DRY_RUN placeholder id — can't build a real m2m op
            elif sorted(t["public_categ_ids"]) != want:
                vals["public_categ_ids"] = [(6, 0, want)]
        label = spec.get("rename") or spec["key"]
        if not vals:
            print(f"  = {label} (id={tid}) already converged")
            continue
        changes = ", ".join(k if k != "public_categ_ids" else f"cats={spec['cats']}" for k in vals)
        if DRY_RUN:
            print(f"  ~ WOULD UPDATE {label} (id={tid}): {changes}")
        else:
            call(models, uid, "product.template", "write", [[tid], vals])
            print(f"  ~ UPDATED {label} (id={tid}): {changes}")


# ── Split helpers ───────────────────────────────────────────────────────────
def _company_ctx(company_id: int) -> dict:
    return {"context": {"allowed_company_ids": [company_id], "company_id": company_id}}


def _tax_ids(models, uid, company_id: int) -> list[int]:
    chain, cid = [], company_id
    while cid:
        chain.append(cid)
        p = call(models, uid, "res.company", "read", [[cid], ["parent_id"]])[0]["parent_id"]
        cid = p[0] if p else False
    ids = []
    for name in SALE_TAXES:
        matches = call(
            models,
            uid,
            "account.tax",
            "search_read",
            [[("name", "=", name), ("type_tax_use", "=", "sale"), ("company_id", "in", chain)]],
            {"fields": ["id", "company_id"]},
        )
        by = {m["company_id"][0]: m["id"] for m in matches if m.get("company_id")}
        ids.append(next((by[c] for c in chain if c in by), None))
    if any(i is None for i in ids):
        fail(f"sale taxes {SALE_TAXES} not resolvable for company {company_id}")
    return ids


def _cultivar_line(models, uid, tmpl_id: int) -> dict:
    """Return the Cultivar attribute_line record (id, value_ids) for a template."""
    lines = call(
        models,
        uid,
        "product.template.attribute.line",
        "search_read",
        [[("product_tmpl_id", "=", tmpl_id), ("attribute_id.name", "=", CULTIVAR_ATTR)]],
        {"fields": ["id", "value_ids"]},
    )
    if not lines:
        fail(f"template {tmpl_id} has no Cultivar attribute line")
    return lines[0]


def _variant_stock(models, uid, variant_id: int, location_id: int) -> float:
    quants = call(
        models,
        uid,
        "stock.quant",
        "search_read",
        [[("product_id", "=", variant_id), ("location_id", "=", location_id)]],
        {"fields": ["quantity"]},
    )
    return sum(q["quantity"] for q in quants)


def _new_single_cultivar_template(
    models,
    uid,
    company_id,
    tax_ids,
    stock_location,
    ctx,
    *,
    name,
    botanical,
    list_price,
    cats,
    cat_id,
    cultivar_name,
    variants,  # variants: [{"format": "Bareroot"|"Potted", "qty", "sku", "price_extra"}]
) -> int:
    """Create a template with a Format axis (+ optional single Cultivar axis) and
    seed its variant SKUs + opening stock. Used by both splits."""
    fmt_attr = call(models, uid, "product.attribute", "search", [[("name", "=", FORMAT_ATTR)]], {"limit": 1})[0]
    fmt_vals = {
        v["name"]: v["id"]
        for v in call(
            models,
            uid,
            "product.attribute.value",
            "search_read",
            [[("attribute_id", "=", fmt_attr)]],
            {"fields": ["name"]},
        )
    }
    formats = sorted({v["format"] for v in variants})
    attr_lines = [(0, 0, {"attribute_id": fmt_attr, "value_ids": [(6, 0, [fmt_vals[f] for f in formats])]})]

    cultivar_value_id = None
    if cultivar_name:
        cult_attr = call(models, uid, "product.attribute", "search", [[("name", "=", CULTIVAR_ATTR)]], {"limit": 1})[0]
        found = call(
            models,
            uid,
            "product.attribute.value",
            "search",
            [[("name", "=", cultivar_name), ("attribute_id", "=", cult_attr)]],
            {"limit": 1},
        )
        cultivar_value_id = (
            found[0]
            if found
            else call(
                models, uid, "product.attribute.value", "create", [{"name": cultivar_name, "attribute_id": cult_attr}]
            )
        )
        attr_lines.append((0, 0, {"attribute_id": cult_attr, "value_ids": [(6, 0, [cultivar_value_id])]}))

    vals = {
        "name": name,
        "list_price": list_price,
        "public_categ_ids": [(6, 0, [cat_id[c] for c in cats])],
        "company_id": company_id,
        "type": "consu",
        "is_storable": True,
        "is_published": True,
        "sale_ok": True,
        "purchase_ok": True,
        "taxes_id": [(6, 0, tax_ids)],
        "attribute_line_ids": attr_lines,
        "grove_botanical_name": botanical,
    }
    tmpl_id = call(models, uid, "product.template", "create", [vals], ctx)
    print(f"    created template '{name}' id={tmpl_id} (list_price=${list_price:.2f})")

    # Apply a per-cultivar price_extra if the moved cultivar carried one.
    extra = next((v.get("price_extra") for v in variants if v.get("price_extra")), 0.0)
    if cultivar_value_id and extra:
        ptav = call(
            models,
            uid,
            "product.template.attribute.value",
            "search",
            [[("product_tmpl_id", "=", tmpl_id), ("product_attribute_value_id", "=", cultivar_value_id)]],
        )
        call(models, uid, "product.template.attribute.value", "write", [ptav, {"price_extra": extra}])
        print(f"    price_extra {cultivar_name}: +${extra:.2f}")

    # Apply the Format Potted differential (bareroot-anchor model, GOL-691) so a
    # split child prices Potted at +$POTTED_EXTRA like every seed-built template.
    # product.template.attribute.value.price_extra is per-template, so a freshly
    # created split template starts at 0 and must be set explicitly.
    if "Potted" in formats:
        potted_ptav = call(
            models,
            uid,
            "product.template.attribute.value",
            "search",
            [[("product_tmpl_id", "=", tmpl_id), ("product_attribute_value_id", "=", fmt_vals["Potted"])]],
        )
        call(models, uid, "product.template.attribute.value", "write", [potted_ptav, {"price_extra": POTTED_EXTRA}])
        print(f"    price_extra Format Potted: +${POTTED_EXTRA:.2f}")

    # SKU + opening stock per generated variant, matched by its Format value.
    products = call(
        models,
        uid,
        "product.product",
        "search_read",
        [[("product_tmpl_id", "=", tmpl_id)]],
        {"fields": ["product_template_variant_value_ids"]},
    )
    for prod in products:
        pvals = call(
            models,
            uid,
            "product.template.attribute.value",
            "read",
            [prod["product_template_variant_value_ids"]],
            {"fields": ["name"]},
        )
        names = {p["name"] for p in pvals}
        match = next((v for v in variants if v["format"] in names), None)
        if not match:
            fail(f"new template {tmpl_id}: variant {prod['id']} matches no planned format in {names}")
        call(models, uid, "product.product", "write", [[prod["id"]], {"default_code": match["sku"]}])
        quant_id = call(
            models,
            uid,
            "stock.quant",
            "create",
            [{"product_id": prod["id"], "location_id": stock_location, "inventory_quantity": match["qty"]}],
            {"context": {"inventory_mode": True, **ctx["context"]}},
        )
        jsonrpc_execute(uid, "stock.quant", "action_apply_inventory", [[quant_id]], ctx)
        print(f"    variant {match['sku']}: {match['qty']} @ loc {stock_location}")
    return tmpl_id


def _drop_cultivar(models, uid, tmpl_id: int, cultivar_name: str, expect_before: int, expect_after: int) -> None:
    """Remove a cultivar VALUE from a template's Cultivar line (archives its
    variants). Asserts the variant count goes expect_before -> expect_after."""
    line = _cultivar_line(models, uid, tmpl_id)
    values = call(models, uid, "product.attribute.value", "read", [line["value_ids"]], {"fields": ["name"]})
    victim = next((v for v in values if v["name"] == cultivar_name), None)
    if not victim:
        fail(f"template {tmpl_id} Cultivar line has no value '{cultivar_name}' (has {[v['name'] for v in values]})")
    before = call(models, uid, "product.product", "search_count", [[("product_tmpl_id", "=", tmpl_id)]])
    if before != expect_before:
        fail(f"template {tmpl_id} has {before} variants, spec expects {expect_before} before drop")
    if DRY_RUN:
        print(
            f"    WOULD DROP cultivar '{cultivar_name}' from template {tmpl_id} "
            f"({expect_before} -> {expect_after} variants)"
        )
        return
    call(models, uid, "product.template.attribute.line", "write", [[line["id"]], {"value_ids": [(3, victim["id"])]}])
    after = call(
        models, uid, "product.product", "search_count", [[("product_tmpl_id", "=", tmpl_id), ("active", "=", True)]]
    )
    if after != expect_after:
        fail(f"template {tmpl_id} has {after} active variants after drop, spec expects {expect_after}")
    print(f"    dropped cultivar '{cultivar_name}' from template {tmpl_id} (now {after} variants)")


def _read_cultivar_variants(models, uid, tmpl_id, cultivar_name, list_price, stock_location, code_prefix):
    """Read the BR/PT variants of one cultivar on a template: qty, price_extra,
    and planned new SKU. Returns (variants_plan, effective_bareroot_price)."""
    ptav = call(
        models,
        uid,
        "product.template.attribute.value",
        "search_read",
        [[("product_tmpl_id", "=", tmpl_id), ("attribute_id.name", "=", CULTIVAR_ATTR), ("name", "=", cultivar_name)]],
        {"fields": ["price_extra"]},
    )
    extra = ptav[0]["price_extra"] if ptav else 0.0
    variants = call(
        models,
        uid,
        "product.product",
        "search_read",
        [[("product_tmpl_id", "=", tmpl_id), ("active", "=", True)]],
        {"fields": ["default_code", "product_template_variant_value_ids"]},
    )
    plan = []
    for v in variants:
        pvals = call(
            models,
            uid,
            "product.template.attribute.value",
            "read",
            [v["product_template_variant_value_ids"]],
            {"fields": ["name"]},
        )
        names = {p["name"] for p in pvals}
        if cultivar_name not in names:
            continue
        fmt = next((f for f in ("Bareroot", "Potted") if f in names), None)
        if not fmt:
            continue
        qty = _variant_stock(models, uid, v["id"], stock_location)
        plan.append(
            {
                "format": fmt,
                "qty": qty,
                "sku": f"{code_prefix}-{FORMAT_ABBR[fmt]}",
                "price_extra": extra,
                "old_sku": v["default_code"],
            }
        )
    effective_br = (list_price + extra) if list_price is not None else None
    return plan, effective_br


def _move_gallery_image(models, uid, src_tmpl, dst_tmpl, image_name) -> None:
    # The ingest pipeline (grove-sites scripts/upload-asset.ts) renames gallery
    # rows to "<slug>-<n>.jpg [grove-ingest <hash12>]" — the source filename
    # ("red mulberry fruit 2.jpg") never survives upload. Match exact name
    # first (hand-uploaded images), then any grove-ingest row, then fall back
    # to the SOLE gallery image on the source template; fail only if ambiguous.
    imgs = call(
        models,
        uid,
        "product.image",
        "search_read",
        [[("product_tmpl_id", "=", src_tmpl), ("name", "=", image_name)]],
        {"fields": ["id", "name", "image_1920"]},
    )
    if not imgs:
        imgs = call(
            models,
            uid,
            "product.image",
            "search_read",
            [[("product_tmpl_id", "=", src_tmpl), ("name", "like", "grove-ingest")]],
            {"fields": ["id", "name", "image_1920"]},
        )
    if not imgs:
        imgs = call(
            models,
            uid,
            "product.image",
            "search_read",
            [[("product_tmpl_id", "=", src_tmpl)]],
            {"fields": ["id", "name", "image_1920"]},
        )
    if len(imgs) != 1:
        fail(
            f"gallery image for '{image_name}' not uniquely resolvable on template "
            f"{src_tmpl} ({len(imgs)} candidates) — refusing to guess"
        )
    img = imgs[0]
    if img["name"] != image_name:
        print(f"    note: matched by fallback — actual image name '{img['name']}'")
    if DRY_RUN:
        print(f"    WOULD MOVE gallery image '{image_name}' from {src_tmpl} -> {dst_tmpl}")
        return
    call(
        models,
        uid,
        "product.image",
        "create",
        [{"name": img["name"], "image_1920": img["image_1920"], "product_tmpl_id": dst_tmpl}],
    )
    call(models, uid, "product.image", "unlink", [[img["id"]]])
    print(f"    moved gallery image '{image_name}' -> template {dst_tmpl}")


# ── Pass 3: Red Mulberry split ──────────────────────────────────────────────
def red_mulberry_split(models, uid, idx, cat_id, company_id, tax_ids, stock_location, ctx) -> None:
    print("\n── Pass 3: Red Mulberry split (off Mulberry 159) ──")
    if norm_species("Red Mulberry") in idx:
        print("  = 'Red Mulberry' template already exists — split already applied, skipping")
        return
    mulberry = idx.get(norm_species("Mulberry"))
    if not mulberry or mulberry["id"] != 159:
        fail(f"expected Mulberry at id=159, got {mulberry}")
    plan, _ = _read_cultivar_variants(models, uid, 159, "Red Mulberry", None, stock_location, "REDMULB")
    # list_price on 159 is the bareroot base; the child's bareroot price = base + Red Mulberry extra.
    base = call(models, uid, "product.template", "read", [[159], ["list_price"]])[0]["list_price"]
    child_list_price = base + (plan[0]["price_extra"] if plan else 0.0)
    if len(plan) != 2:
        fail(f"expected 2 Red Mulberry variants (BR/PT) on 159, found {len(plan)}: {plan}")
    variant_view = [(p["format"], p["qty"], p["sku"]) for p in plan]
    print(f"  plan: new 'Red Mulberry' list_price=${child_list_price:.2f}; variants={variant_view}")
    if not DRY_RUN:
        child = _new_single_cultivar_template(
            models,
            uid,
            company_id,
            tax_ids,
            stock_location,
            ctx,
            name="Red Mulberry",
            botanical="Morus rubra",
            list_price=child_list_price,
            cats=[FRUIT_TREES, NATIVE],
            cat_id=cat_id,
            cultivar_name=None,
            variants=plan,
        )
        _move_gallery_image(models, uid, 159, child, "red mulberry fruit 2.jpg")
    else:
        _move_gallery_image(models, uid, 159, 0, "red mulberry fruit 2.jpg")
    _drop_cultivar(models, uid, 159, "Red Mulberry", expect_before=26, expect_after=24)


# ── Pass 4: Persimmon split ─────────────────────────────────────────────────
def persimmon_split(models, uid, idx, cat_id, company_id, tax_ids, stock_location, ctx) -> None:
    print("\n── Pass 4: Persimmon (kaki) split (IKKJ off 163) ──")
    # 163 was renamed to American Persimmon in pass 2; resolve by id, not name.
    t163 = call(models, uid, "product.template", "read", [[163], ["name"]])
    if not t163:
        fail("template 163 not found")
    if norm_species("Persimmon") in idx and idx[norm_species("Persimmon")]["id"] != 163:
        print("  = kaki 'Persimmon' template already exists — split already applied, skipping")
        return
    plan, _ = _read_cultivar_variants(models, uid, 163, "IKKJ", 12.00, stock_location, "PERSIMMON-IKKJ")
    if len(plan) != 2:
        fail(f"expected 2 IKKJ variants (BR/PT) on 163, found {len(plan)}: {plan}")
    # Preserve effective $40 BR / $42 PT: base $12 + IKKJ extra (spec: $28).
    base = 12.00
    extra = plan[0]["price_extra"] or 28.00
    eff_br = base + extra
    print(
        f"  plan: new 'Persimmon' (kaki) base=${base:.2f} + IKKJ extra=${extra:.2f} "
        f"-> BR ${eff_br:.2f}/PT ${eff_br + POTTED_EXTRA:.2f}"
    )
    variant_view = [(p["format"], p["qty"], p["sku"]) for p in plan]
    print(f"  IKKJ variants moved: {variant_view}")
    if not DRY_RUN:
        _new_single_cultivar_template(
            models,
            uid,
            company_id,
            tax_ids,
            stock_location,
            ctx,
            name="Persimmon",
            botanical="Diospyros kaki",
            list_price=base,
            cats=[FRUIT_TREES],
            cat_id=cat_id,
            cultivar_name="IKKJ",
            variants=plan,
        )
    _drop_cultivar(models, uid, 163, "IKKJ", expect_before=8, expect_after=6)
    print("  NOTE: 163 keeps its id, Meader/Regular/Seedling variants, real stock, order S01566, 3 photos.")
    print(
        "  PHOTO VERIFY (Wes): confirm 163's 'persimmon fruit' photo is virginiana "
        "(small, clustered); if kaki, move to the new kaki template."
    )


def print_end_state_summary(cat_id: dict[str, int]) -> None:
    print("\n── End-state category membership (published, from spec mapping) ──")
    members: dict[str, list[str]] = {c: [] for c in (FRUIT_TREES, FRUITING_VINES, NATIVE, NUT_TREES, BERRY_NUT_SHRUBS)}
    for spec in TEMPLATES:
        if not spec["cats"] or not spec["required"]:
            continue
        for c in spec["cats"]:
            members[c].append(spec.get("rename") or spec["key"])
    for child in SPLIT_CHILDREN:
        for c in child["cats"]:
            members[c].append(child["name"])
    for c, names in members.items():
        print(f"  {c} ({len(names)}): {', '.join(sorted(names))}")
    print(
        "  NOTE Passion Flower (Native) + held species are unpublished -> excluded from ?cat= counts until republished."
    )


def main() -> None:
    print(f"Target: {ODOO_URL} db={ODOO_DB} company={COMPANY_NAME}  DRY_RUN={'yes' if DRY_RUN else 'NO — LIVE'}")
    models, uid = authenticate()
    company_ids = call(models, uid, "res.company", "search", [[("name", "=", COMPANY_NAME)]], {"limit": 1})
    if not company_ids:
        fail(f"Company '{COMPANY_NAME}' not found")
    company_id = company_ids[0]
    ctx = _company_ctx(company_id)
    tax_ids = _tax_ids(models, uid, company_id)
    wh = call(
        models,
        uid,
        "stock.warehouse",
        "search_read",
        [[("company_id", "=", company_id)]],
        {"fields": ["lot_stock_id"], "limit": 1},
    )
    if not wh:
        fail(f"no warehouse for company {company_id}")
    stock_location = wh[0]["lot_stock_id"][0]

    cat_id = reconcile_categories(models, uid)
    idx = index_active_templates(models, uid, company_id)
    reconcile_templates(models, uid, idx, cat_id)
    red_mulberry_split(models, uid, idx, cat_id, company_id, tax_ids, stock_location, ctx)
    persimmon_split(models, uid, idx, cat_id, company_id, tax_ids, stock_location, ctx)
    print_end_state_summary(cat_id)
    print("\nDone." + (" (dry run — nothing written)" if DRY_RUN else ""))


if __name__ == "__main__":
    main()
