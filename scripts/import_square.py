#!/usr/bin/env python3
"""Import a Square catalog export + orders-history export into Odoo, as VARIANTS.

Structure produced (v2, 2026-07-07):
  - One product.template per Square Item Name; Square Variation Names become
    values of a shared "Cultivar" attribute, so each template carries its
    cultivars as Odoo variants (own SKU, own stock, own photos each).
    Adding a cultivar later = adding one attribute value on the product
    (Odoo UI) or just selling it in Square (this importer auto-extends).
  - Items that only ever had a "Regular" variation stay simple products.
  - ALIASES normalizes Square typos; MANUAL_VARIANTS forces known cultivars
    that the exports miss.
  - Every plant product also carries a "Format" attribute (Potted /
    Bareroot): current Square stock lands on the Potted variants, Bareroot
    variants start at 0 (bareroot quantities arrive ~October). The
    grove_headless potting-batch model moves stock between formats.
  - SKUs are generated deterministically per variant:
    <ITEM>-<CULTIVAR>-<FMT> (e.g. APPL-AB-PT for Apple / Amish Black /
    Potted), editable in Odoo afterwards.

Phases (same safety order as v1):
  1. products (templates + variants + SKUs + per-variation price extras)
  2. orders (confirmed sale.orders, original dates, deliveries cancelled)
  3. inventory (absolute catalog quantities per variant -- the stock truth)

Idempotent: templates by (name, company), orders by client_order_ref,
attribute values by name. --wipe first removes previously imported orders
and products for a clean rebuild (QA workflow).

Usage:
    ODOO_PASSWORD=... python3 import_square.py \
        --catalog ~/Downloads/MLZ..._catalog.csv \
        --orders-dir ~/Downloads/orders-extract \
        --company "At The Grove Nursery" [--wipe]
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import sys
import xmlrpc.client
from collections import defaultdict
from typing import Any

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

WALKIN_PARTNER = "Square POS Walk-in"
CUSTOM_SALE_PRODUCT = "Square custom sale"
CULTIVAR_ATTRIBUTE = "Cultivar"
FORMAT_ATTRIBUTE = "Format"
FORMATS = ["Potted", "Bareroot"]
# Current physical stock is all potted (verified with Josh 2026-07-07);
# bareroot quantities arrive ~October and start at 0.
CURRENT_STOCK_FORMAT = "Potted"
FORMAT_SKU = {"Potted": "PT", "Bareroot": "BR"}
# Non-plant items don't get the Format attribute
NON_PLANT = {"Sticker", "Pot"}

# Square data hygiene: typo/alias -> canonical Item Name (verified with Josh
# 2026-07-07; "Jujubee" is the intentional spelling).
ALIASES = {
    "Chesnut": "Chestnut",
    "Fig treee": "Fig",
    "Elderberries": "Elderberry",
    "Jujube": "Jujubee",
}

# Cultivars sold before the export window or otherwise missing from the
# files -- forced onto their product at import time.
MANUAL_VARIANTS = {
    "Jujubee": ["Sugar Cane", "Honey Jar"],
}


def canonical(item: str) -> str:
    item = (item or "").strip()
    return ALIASES.get(item, item)


def norm_variation(v: str) -> str:
    v = (v or "").strip()
    return "" if not v or v.lower() == "regular" else v


def connect():
    if not ODOO_PASSWORD:
        sys.exit("Set ODOO_PASSWORD (and optionally ODOO_URL/ODOO_DB/ODOO_USER)")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        sys.exit("Authentication failed")
    return uid, xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


class Client:
    """execute_kw wrapper pinning company context; act() tolerates the
    Odoo 19 XML-RPC quirk where action methods execute fine server-side
    but fail marshalling their None return value."""

    def __init__(self, uid, models, company_id):
        self.uid, self.models, self.company_id = uid, models, company_id

    def call(self, model: str, method: str, args: list, kw: dict | None = None) -> Any:
        kw = dict(kw or {})
        ctx = dict(kw.get("context") or {})
        ctx.setdefault("allowed_company_ids", [self.company_id])
        ctx.setdefault("company_id", self.company_id)
        kw["context"] = ctx
        return self.models.execute_kw(ODOO_DB, self.uid, ODOO_PASSWORD, model, method, args, kw)

    def act(self, model: str, method: str, args: list) -> None:
        try:
            self.call(model, method, args)
        except xmlrpc.client.Fault as e:
            if "cannot marshal None" not in str(e):
                raise


# --------------------------------------------------------------------------
# SKU generation: deterministic, collision-free within one import run
# --------------------------------------------------------------------------
def _letters(s: str) -> str:
    return re.sub(r"[^A-Z]", "", s.upper())


def build_sku_codes(names: list[str], width: int) -> dict[str, str]:
    """name -> short unique code. Prefix of letters, lengthened on collision;
    numeric suffix as last resort. Deterministic: input sorted."""
    out: dict[str, str] = {}
    used: set[str] = set()
    for name in sorted(set(names)):
        base = _letters(name) or "X"
        code = base[:width]
        w = width
        while code in used and w < len(base):
            w += 1
            code = base[:w]
        n = 2
        while code in used:
            code = f"{base[:width]}{n}"
            n += 1
        used.add(code)
        out[name] = code
    return out


def build_cultivar_codes(cults: list[str]) -> dict[str, str]:
    """Per-template cultivar codes, deduped (Amish Black vs Arkansas Black
    both yield initials AB -- collisions widen to two letters per word,
    then a numeric suffix)."""

    def candidates(name: str):
        words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
        if len(words) >= 2:
            yield "".join(w[0].upper() for w in words)
            yield "".join(w[:2].upper() for w in words)
        else:
            yield _letters(name)[:3] or "X"
            yield _letters(name)[:4] or "X"

    out: dict[str, str] = {}
    used: set[str] = set()
    for name in sorted(set(cults)):
        code = None
        for cand in candidates(name):
            if cand not in used:
                code = cand
                break
        if code is None:
            base = next(candidates(name))
            n = 2
            while f"{base}{n}" in used:
                n += 1
            code = f"{base}{n}"
        used.add(code)
        out[name] = code
    return out


# --------------------------------------------------------------------------
# Source parsing
# --------------------------------------------------------------------------
def read_catalog(path: str) -> dict[str, dict[str, dict]]:
    """{item: {variation('' = simple): {price, qty}}}"""
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            item = canonical(row.get("Item Name"))
            if not item or (row.get("Archived") or "").strip().upper() == "Y":
                continue
            var = norm_variation(row.get("Variation Name"))
            try:
                price = float(row.get("Price") or 0)
            except ValueError:
                price = 0.0
            try:
                qty = float(row.get("Current Quantity Goldberry Grove") or 0)
            except ValueError:
                qty = 0.0
            out[item][var] = {
                "price": price,
                "qty": qty,
                "description": (row.get("Description") or "").strip(),
                "category": (row.get("Categories") or "").strip(),
                "sku": (row.get("SKU") or "").strip(),
            }
    return out


def order_groups(orders_dir: str):
    """Yield one list of line rows per Square order (grouping: consecutive
    rows sharing the order-level columns -- Square omits POS order ids)."""
    for path in sorted(glob.glob(os.path.join(orders_dir, "*.csv"))):
        group: list[dict] = []
        key = None
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if not (row.get("Order Date") or "").strip():
                    continue
                k = (
                    row.get("Order", ""),
                    row["Order Date"],
                    row.get("Order Subtotal", ""),
                    row.get("Order Tax Total", ""),
                    row.get("Order Total", ""),
                    row.get("Recipient Name", ""),
                    row.get("Recipient Email", ""),
                )
                if k != key and group:
                    yield group
                    group = []
                key = k
                group.append(row)
        if group:
            yield group


def history_variations(orders_dir: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for group in order_groups(orders_dir):
        for row in group:
            item = canonical(row.get("Item Name"))
            if item:
                out[item].add(norm_variation(row.get("Item Variation")))
    return out


# --------------------------------------------------------------------------
# Wipe (QA rebuild)
# --------------------------------------------------------------------------
def wipe(c: Client) -> None:
    orders = c.call("sale.order", "search", [[("client_order_ref", "like", "SQUARE-")]])
    if orders:
        c.act("sale.order", "action_cancel", [orders])
        c.call("sale.order", "unlink", [orders])
    print(f"wipe: {len(orders)} orders removed")
    quants = c.call(
        "stock.quant",
        "search",
        [[("company_id", "=", c.company_id), ("location_id.usage", "=", "internal")]],
    )
    if quants:
        c.call("stock.quant", "write", [quants, {"inventory_quantity": 0}])
        c.act("stock.quant", "action_apply_inventory", [quants])
    print(f"wipe: {len(quants)} quants zeroed")
    tmpls = c.call("product.template", "search", [[("company_id", "=", c.company_id)]])
    deleted = archived = 0
    for t in tmpls:
        try:
            c.call("product.template", "unlink", [[t]])
            deleted += 1
        except xmlrpc.client.Fault:
            # referenced by stock moves etc. -- archive + free up the name
            name = c.call("product.template", "read", [[t], ["name"]])[0]["name"]
            c.call("product.template", "write", [[t], {"active": False, "name": f"[OLD] {name}"}])
            archived += 1
    print(f"wipe: {deleted} templates deleted, {archived} archived")


# --------------------------------------------------------------------------
# Product creation
# --------------------------------------------------------------------------
class Catalog:
    """Created structure: (item, cultivar, format) -> product.product id."""

    def __init__(self):
        self.variant: dict[tuple[str, str, str], int] = {}
        self.template: dict[str, int] = {}


def ensure_attribute(c: Client, name: str) -> int:
    a = c.call("product.attribute", "search", [[("name", "=", name)]], {"limit": 1})
    if a:
        return a[0]
    return c.call(
        "product.attribute",
        "create",
        [{"name": name, "create_variant": "always", "display_type": "select"}],
    )


def ensure_attr_values(c: Client, attr_id: int, names: list[str], cache: dict) -> dict[str, int]:
    out = {}
    for n in names:
        if n not in cache:
            v = c.call(
                "product.attribute.value",
                "search",
                [[("attribute_id", "=", attr_id), ("name", "=", n)]],
                {"limit": 1},
            )
            cache[n] = (
                v[0] if v else c.call("product.attribute.value", "create", [{"attribute_id": attr_id, "name": n}])
            )
        out[n] = cache[n]
    return out


def get_or_create_category(c: Client, name: str, cache: dict) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    if name not in cache:
        found = c.call("product.category", "search", [[("name", "=", name)]], {"limit": 1})
        cache[name] = found[0] if found else c.call("product.category", "create", [{"name": name}])
    return cache[name]


def variant_map(c: Client, tmpl_id: int) -> dict[tuple[str, str], int]:
    """(cultivar, format) -> product.product id; '' where an axis is absent."""
    # product_template_attribute_value_ids includes single-value attribute
    # lines; the similarly-named product_template_VARIANT_value_ids excludes
    # them (display-name field) and silently broke every one-cultivar
    # product on the first v2 run.
    variants = c.call(
        "product.product",
        "search_read",
        [[("product_tmpl_id", "=", tmpl_id)]],
        {"fields": ["product_template_attribute_value_ids"]},
    )
    out = {}
    for v in variants:
        ptav_ids = v["product_template_attribute_value_ids"]
        cult = fmt = ""
        if ptav_ids:
            ptavs = c.call(
                "product.template.attribute.value",
                "read",
                [ptav_ids, ["name", "attribute_id"]],
            )
            for pv in ptavs:
                if pv["attribute_id"][1] == FORMAT_ATTRIBUTE:
                    fmt = pv["name"]
                else:
                    cult = pv["name"]
        out[(cult, fmt)] = v["id"]
    return out


def import_products(c: Client, catalog, history, website_id: int, item_codes) -> Catalog:
    result = Catalog()
    cult_attr = ensure_attribute(c, CULTIVAR_ATTRIBUTE)
    fmt_attr = ensure_attribute(c, FORMAT_ATTRIBUTE)
    val_cache: dict = {}
    fmt_cache: dict = {}
    fmt_ids = ensure_attr_values(c, fmt_attr, FORMATS, fmt_cache)
    cat_cache: dict = {}

    # union of cultivars per item across catalog + history + manual
    cultivars: dict[str, set[str]] = defaultdict(set)
    for item, vars_ in catalog.items():
        cultivars[item] |= {v for v in vars_ if v}
    for item, vars_ in history.items():
        cultivars[item] |= {v for v in vars_ if v}
    for item, extra in MANUAL_VARIANTS.items():
        cultivars[canonical(item)] |= set(extra)

    all_items = sorted(set(catalog) | set(history) | {canonical(i) for i in MANUAL_VARIANTS})
    made = 0
    for item in all_items:
        cat_rows = catalog.get(item, {})
        cults = sorted(cultivars.get(item, set()))
        # base price: the most common variation price, else the Regular row's
        prices = [d["price"] for d in cat_rows.values() if d["price"] > 0]
        base_price = min(prices) if prices else 0.0
        meta = cat_rows.get("") or (next(iter(cat_rows.values())) if cat_rows else {})
        vals: dict[str, Any] = {
            "name": item,
            "type": "consu",
            "is_storable": True,
            "list_price": base_price,
            "company_id": c.company_id,
            "website_id": website_id,
            "is_published": True,
        }
        cid = get_or_create_category(c, meta.get("category", ""), cat_cache)
        if cid:
            vals["categ_id"] = cid
        if meta.get("description"):
            vals["description_sale"] = meta["description"]

        existing = c.call(
            "product.template",
            "search",
            [[("name", "=", item), ("company_id", "=", c.company_id)]],
            {"limit": 1},
        )
        is_plant = item not in NON_PLANT
        lines = []
        names: list[str] = []
        if cults:
            # keep a Regular value only when the sources actually have
            # Regular-variation rows for a multi-cultivar item (stock/sales
            # need somewhere to land; rename or merge later in the UI)
            has_regular = "" in cat_rows or "" in history.get(item, set())
            names = (["Regular"] if has_regular else []) + cults
            ids = ensure_attr_values(c, cult_attr, names, val_cache)
            lines.append((0, 0, {"attribute_id": cult_attr, "value_ids": [(6, 0, [ids[n] for n in names])]}))
        if is_plant:
            lines.append(
                (
                    0,
                    0,
                    {"attribute_id": fmt_attr, "value_ids": [(6, 0, [fmt_ids[f] for f in FORMATS])]},
                )
            )
        if existing:
            tmpl = existing[0]
            # DIFF the attribute lines -- re-sending (0,0,...) create commands
            # on an existing template APPENDS duplicate lines and Odoo then
            # tries to generate the cartesian square of all combinations
            # ("variants above allowed limit").
            if lines:
                ex = c.call(
                    "product.template.attribute.line",
                    "search_read",
                    [[("product_tmpl_id", "=", tmpl)]],
                    {"fields": ["attribute_id", "value_ids"]},
                )
                by_attr = {e["attribute_id"][0]: e for e in ex}
                cmds = []
                for _, _, lv in lines:
                    want = set(lv["value_ids"][0][2])
                    cur = by_attr.get(lv["attribute_id"])
                    if cur is None:
                        cmds.append((0, 0, lv))
                    elif set(cur["value_ids"]) != want:
                        cmds.append((1, cur["id"], {"value_ids": [(6, 0, sorted(want))]}))
                if cmds:
                    c.call("product.template", "write", [[tmpl], {"attribute_line_ids": cmds}])
        else:
            if lines:
                vals["attribute_line_ids"] = lines
            tmpl = c.call("product.template", "create", [vals])
            made += 1
        vmap = variant_map(c, tmpl)
        code = item_codes[item]
        cult_codes = build_cultivar_codes([n for n in names if n != "Regular"])
        cult_names = names or [""]
        fmt_names = FORMATS if is_plant else [""]
        for cult in cult_names:
            key_cult = "" if cult in ("", "Regular") else cult
            for fmt in fmt_names:
                var_id = vmap.get((cult if cult != "" else "", fmt))
                if not var_id:
                    continue
                result.variant[(item, key_cult, fmt)] = var_id
                parts = [code]
                if key_cult or cult == "Regular":
                    parts.append(cult_codes[cult] if cult != "Regular" else "REG")
                if fmt:
                    parts.append(FORMAT_SKU[fmt])
                c.call("product.product", "write", [[var_id], {"default_code": "-".join(parts)}])
            # per-cultivar price difference -> price_extra on the template
            # attribute value (format carries no price difference)
            row = cat_rows.get(key_cult)
            if cult and row and row["price"] > 0 and row["price"] != base_price:
                ptav = c.call(
                    "product.template.attribute.value",
                    "search",
                    [
                        [
                            ("product_tmpl_id", "=", tmpl),
                            ("name", "=", cult),
                            ("attribute_id", "=", cult_attr),
                        ]
                    ],
                    {"limit": 1},
                )
                if ptav:
                    c.call(
                        "product.template.attribute.value",
                        "write",
                        [ptav, {"price_extra": row["price"] - base_price}],
                    )
        result.template[item] = tmpl

    n_variants = len(result.variant)
    print(f"products: {len(all_items)} templates ({made} new), {n_variants} variants mapped")
    return result


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------
def get_or_create_partner(c: Client, group: list[dict], cache: dict) -> int:
    name = (group[0].get("Recipient Name") or "").strip()
    email = (group[0].get("Recipient Email") or "").strip()
    if not email and "@" in (group[0].get("Order Name") or ""):
        email = group[0]["Order Name"].strip()
    label = name or email or WALKIN_PARTNER
    if label in cache:
        return cache[label]
    domain = [("email", "=", email)] if email else [("name", "=", label)]
    found = c.call("res.partner", "search", [domain], {"limit": 1})
    if found:
        cache[label] = found[0]
    else:
        vals = {"name": label, "company_type": "person"}
        if email:
            vals["email"] = email
        cache[label] = c.call("res.partner", "create", [vals])
    return cache[label]


def import_orders(c: Client, orders_dir: str, cat: Catalog, warehouse_id: int, website_id: int):
    partner_cache: dict = {}
    svc = c.call(
        "product.template",
        "search_read",
        [[("name", "=", CUSTOM_SALE_PRODUCT), ("company_id", "=", c.company_id)]],
        {"fields": ["product_variant_id"], "limit": 1},
    )
    if svc:
        custom_var = svc[0]["product_variant_id"][0]
    else:
        t = c.call(
            "product.template",
            "create",
            [{"name": CUSTOM_SALE_PRODUCT, "type": "service", "company_id": c.company_id}],
        )
        custom_var = c.call("product.template", "read", [[t], ["product_variant_id"]])[0]["product_variant_id"][0]

    made = skipped = 0
    for idx, group in enumerate(order_groups(orders_dir)):
        first = group[0]
        ref = f"SQUARE-{first['Order Date'].replace('/', '')}-{first.get('Order Total', '0')}-{idx}"
        if c.call("sale.order", "search", [[("client_order_ref", "=", ref)]], {"limit": 1}):
            skipped += 1
            continue
        lines = []
        for row in group:
            item = canonical(row.get("Item Name"))
            qty = float(row.get("Item Quantity") or 1)
            price = float(row.get("Item Price") or 0)
            if item:
                cult = norm_variation(row.get("Item Variation"))
                var = (
                    cat.variant.get((item, cult, CURRENT_STOCK_FORMAT))
                    or cat.variant.get((item, cult, ""))
                    or cat.variant.get((item, "", CURRENT_STOCK_FORMAT))
                    or cat.variant.get((item, "", ""))
                )
                if not var:
                    print(f"  WARN no variant for {item!r}/{cult!r} -- using custom-sale line")
                    var = custom_var
            else:
                var = custom_var
            lines.append((0, 0, {"product_id": var, "product_uom_qty": qty, "price_unit": price}))
        order_id = c.call(
            "sale.order",
            "create",
            [
                {
                    "partner_id": get_or_create_partner(c, group, partner_cache),
                    "company_id": c.company_id,
                    "warehouse_id": warehouse_id,
                    "website_id": website_id,
                    "user_id": c.uid,
                    "date_order": first["Order Date"].replace("/", "-") + " 12:00:00",
                    "client_order_ref": ref,
                    "order_line": lines,
                }
            ],
        )
        c.act("sale.order", "action_confirm", [[order_id]])
        c.call(
            "sale.order",
            "write",
            [[order_id], {"date_order": first["Order Date"].replace("/", "-") + " 12:00:00"}],
        )
        pickings = c.call("stock.picking", "search", [[("sale_id", "=", order_id), ("state", "!=", "cancel")]])
        if pickings:
            c.act("stock.picking", "action_cancel", [pickings])
        made += 1
    print(f"orders: {made} imported, {skipped} already present")


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------
def apply_inventory(c: Client, catalog, cat: Catalog, warehouse_id: int) -> None:
    loc = c.call("stock.warehouse", "read", [[warehouse_id], ["lot_stock_id"]])[0]["lot_stock_id"][0]
    applied = negative = 0
    for item, vars_ in catalog.items():
        for cult, row in vars_.items():
            qty = row["qty"]
            if qty == 0:
                continue
            if qty < 0:
                print(f"  SKIP negative (oversold in Square): {item}/{cult or 'Regular'} = {qty}")
                negative += 1
                continue
            var = cat.variant.get((item, cult, CURRENT_STOCK_FORMAT)) or cat.variant.get((item, cult, ""))
            if not var:
                print(f"  WARN no variant for stock row {item}/{cult or 'Regular'}")
                continue
            quant = c.call(
                "stock.quant",
                "search",
                [[("product_id", "=", var), ("location_id", "=", loc)]],
                {"limit": 1},
            )
            if quant:
                c.call("stock.quant", "write", [quant, {"inventory_quantity": qty}])
                c.act("stock.quant", "action_apply_inventory", [quant])
            else:
                qid = c.call(
                    "stock.quant",
                    "create",
                    [{"product_id": var, "location_id": loc, "inventory_quantity": qty}],
                )
                c.act("stock.quant", "action_apply_inventory", [[qid]])
            applied += 1
    print(f"inventory: {applied} variant quantities applied, {negative} negative skipped")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--orders-dir")
    ap.add_argument("--company", default="At The Grove Nursery")
    ap.add_argument("--wipe", action="store_true", help="remove previously imported orders/products first")
    args = ap.parse_args()

    uid, models = connect()
    boot = Client(uid, models, 1)
    comp = boot.call("res.company", "search", [[("name", "=", args.company)]], {"limit": 1})
    if not comp:
        sys.exit(f"company not found: {args.company}")
    c = Client(uid, models, comp[0])
    site = c.call("website", "search", [[("company_id", "=", c.company_id)]], {"limit": 1})
    wh = c.call("stock.warehouse", "search", [[("company_id", "=", c.company_id)]], {"limit": 1})
    if not site or not wh:
        sys.exit("company is missing its website or warehouse -- run the grove_headless bootstrap first")
    print(f"target: company={comp[0]} website={site[0]} warehouse={wh[0]} ({args.company})")

    if args.wipe:
        wipe(c)

    catalog = read_catalog(args.catalog)
    history = history_variations(args.orders_dir) if args.orders_dir else {}
    all_items = sorted(set(catalog) | set(history) | {canonical(i) for i in MANUAL_VARIANTS})
    item_codes = build_sku_codes(all_items, 4)

    cat = import_products(c, catalog, history, site[0], item_codes)
    if args.orders_dir:
        import_orders(c, args.orders_dir, cat, wh[0], site[0])
    apply_inventory(c, catalog, cat, wh[0])
    print("done.")


if __name__ == "__main__":
    main()
