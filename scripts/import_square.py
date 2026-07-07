#!/usr/bin/env python3
"""Import a Square catalog export + orders-history export into Odoo.

Source files (Square Dashboard exports):
  - catalog:  <MERCHANT>_catalog-YYYY-MM-DD-HHMM.csv  (one row per variation,
              per-location "Current Quantity <Location>" columns)
  - orders:   orders-<from>-<to>.csv (one row per ORDER LINE; Square omits the
              Order id for POS sales, so lines are grouped by consecutive rows
              sharing date/subtotal/tax/total/recipient within one file)

What it does, in order:
  1. Products: one product.template per (Item Name, Variation) under the
     target company, published to that company's website, category
     found-or-created from the Square category.
  2. Orders: one confirmed sale.order per group with original dates; the
     auto-created delivery pickings are CANCELLED so historical orders never
     touch stock. Customers: real partner when the export has a name/email,
     else a shared "Square POS Walk-in" partner. Custom-amount sales (no item
     name) map to a "Square custom sale" service product.
  3. Inventory: absolute quantities from the catalog's current-quantity
     column applied as an inventory adjustment in the company warehouse —
     AFTER orders, so the adjustment is the single source of stock truth.
     Negative Square quantities (oversold) are logged and skipped.

Idempotent: products match on (name, company); orders match on
client_order_ref; re-applying the same absolute quantities is a no-op.

Usage:
    ODOO_PASSWORD=... python3 import_square.py \
        --catalog ~/Downloads/MLZ..._catalog-2026-07-05-1153.csv \
        --orders-dir ~/Downloads/orders-extract \
        --company "At The Grove Nursery"
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
import xmlrpc.client
from typing import Any

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

WALKIN_PARTNER = "Square POS Walk-in"
CUSTOM_SALE_PRODUCT = "Square custom sale"


def connect():
    if not ODOO_PASSWORD:
        sys.exit("Set ODOO_PASSWORD (and optionally ODOO_URL/ODOO_DB/ODOO_USER)")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        sys.exit("Authentication failed")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


class Client:
    """Thin execute_kw wrapper pinning company context on every call."""

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
        """Call an action method that returns None.

        Odoo executes the action, then fails to MARSHAL the None return
        value ("cannot marshal None unless allow_none is enabled") -- the
        work is done server-side; only the response serialization dies.
        Swallow exactly that fault.
        """
        try:
            self.call(model, method, args)
        except xmlrpc.client.Fault as e:
            if "cannot marshal None" not in str(e):
                raise


def product_display_name(item: str, variation: str) -> str:
    variation = (variation or "").strip()
    if variation and variation.lower() != "regular":
        return f"{item.strip()} - {variation}"
    return item.strip()


def get_or_create_category(c: Client, name: str, cache: dict) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    if name not in cache:
        found = c.call("product.category", "search", [[("name", "=", name)]], {"limit": 1})
        cache[name] = found[0] if found else c.call("product.category", "create", [{"name": name}])
    return cache[name]


def get_or_create_product(c: Client, name: str, vals: dict, cache: dict) -> tuple[int, int]:
    """Return (template_id, variant_id) for a product, creating it if absent."""
    if name in cache:
        return cache[name]
    found = c.call(
        "product.template",
        "search_read",
        [[("name", "=", name), ("company_id", "in", [c.company_id, False])]],
        {"fields": ["product_variant_id"], "limit": 1},
    )
    if found:
        tmpl = found[0]["id"]
        var = found[0]["product_variant_id"][0]
    else:
        tmpl = c.call("product.template", "create", [dict(vals, name=name)])
        var = c.call("product.template", "read", [[tmpl], ["product_variant_id"]])[0]["product_variant_id"][0]
    cache[name] = (tmpl, var)
    return tmpl, var


def import_catalog(c: Client, path: str, website_id: int, qty_column: str) -> dict[str, tuple[int, int, float]]:
    """Create/refresh products. Returns name -> (template, variant, square_qty)."""
    out: dict[str, tuple[int, int, float]] = {}
    cat_cache: dict = {}
    prod_cache: dict = {}
    created = skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            item = (row.get("Item Name") or "").strip()
            if not item:
                continue
            if (row.get("Archived") or "").strip().upper() == "Y":
                skipped += 1
                continue
            name = product_display_name(item, row.get("Variation Name", ""))
            try:
                price = float(row.get("Price") or 0)
            except ValueError:
                price = 0.0
            vals = {
                "type": "consu",
                "is_storable": True,
                "list_price": price,
                "company_id": c.company_id,
                "website_id": website_id,
                "is_published": True,
                "categ_id": get_or_create_category(c, row.get("Categories", ""), cat_cache),
                "description_sale": (row.get("Description") or "").strip() or False,
                "default_code": (row.get("SKU") or "").strip() or False,
            }
            # XML-RPC cannot marshal None; drop empty optionals entirely
            vals = {k: v for k, v in vals.items() if k == "company_id" or (v is not False and v is not None)}
            before = len(prod_cache)
            tmpl, var = get_or_create_product(c, name, vals, prod_cache)
            created += 1 if len(prod_cache) > before else 0
            try:
                qty = float(row.get(qty_column) or 0)
            except ValueError:
                qty = 0.0
            out[name] = (tmpl, var, qty)
    print(f"catalog: {len(out)} rows processed ({created} ensured, {skipped} archived skipped)")
    return out


def order_groups(orders_dir: str):
    """Yield lists of raw line rows, one list per Square order."""
    for path in sorted(glob.glob(os.path.join(orders_dir, "*.csv"))):
        group: list[dict] = []
        key = None
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if not (row.get("Order Date") or "").strip():
                    continue
                # Square omits Order ids for POS sales; consecutive lines of
                # one order share the order-level columns verbatim.
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


def get_or_create_partner(c: Client, group: list[dict], cache: dict) -> int:
    name = (group[0].get("Recipient Name") or "").strip()
    email = (group[0].get("Recipient Email") or "").strip()
    # Square web orders sometimes put the email in "Order Name"
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


def import_orders(c: Client, orders_dir: str, products: dict, warehouse_id: int, website_id: int) -> None:
    prod_cache: dict = {}
    partner_cache: dict = {}
    _, custom_var = get_or_create_product(
        c,
        CUSTOM_SALE_PRODUCT,
        {"type": "service", "company_id": c.company_id, "list_price": 0.0},
        prod_cache,
    )
    made = skipped = 0
    for idx, group in enumerate(order_groups(orders_dir)):
        first = group[0]
        ref = f"SQUARE-{first['Order Date'].replace('/', '')}-{first.get('Order Total', '0')}-{idx}"
        if c.call("sale.order", "search", [[("client_order_ref", "=", ref)]], {"limit": 1}):
            skipped += 1
            continue
        lines = []
        for row in group:
            item = (row.get("Item Name") or "").strip()
            qty = float(row.get("Item Quantity") or 1)
            price = float(row.get("Item Price") or 0)
            if item:
                name = product_display_name(item, row.get("Item Variation", ""))
                if name in products:
                    var = products[name][1]
                else:
                    # sold item absent from the current catalog export --
                    # create unpublished so history imports losslessly
                    _, var = get_or_create_product(
                        c,
                        name,
                        {"type": "consu", "is_storable": True, "company_id": c.company_id, "list_price": price},
                        prod_cache,
                    )
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
                    "date_order": first["Order Date"].replace("/", "-") + " 12:00:00",
                    "client_order_ref": ref,
                    "order_line": lines,
                }
            ],
        )
        c.act("sale.order", "action_confirm", [[order_id]])
        # confirm resets date_order to now -- restore the historical date
        c.call(
            "sale.order",
            "write",
            [[order_id], {"date_order": first["Order Date"].replace("/", "-") + " 12:00:00"}],
        )
        # cancel the delivery so history never moves stock; the catalog
        # quantity adjustment that runs after this is the stock truth
        pickings = c.call("stock.picking", "search", [[("sale_id", "=", order_id), ("state", "!=", "cancel")]])
        if pickings:
            c.act("stock.picking", "action_cancel", [pickings])
        made += 1
    print(f"orders: {made} imported, {skipped} already present")


def apply_inventory(c: Client, products: dict, warehouse_id: int) -> None:
    loc = c.call("stock.warehouse", "read", [[warehouse_id], ["lot_stock_id"]])[0]["lot_stock_id"][0]
    applied = negative = zero = 0
    for name, (_tmpl, var, qty) in products.items():
        if qty < 0:
            print(f"  SKIP negative (oversold in Square): {name} = {qty}")
            negative += 1
            continue
        if qty == 0:
            zero += 1
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
    print(f"inventory: {applied} quantities applied, {zero} zero-qty skipped, {negative} negative skipped")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", required=True, help="Square catalog CSV export")
    ap.add_argument("--orders-dir", help="dir of Square orders-*.csv (omit to skip orders)")
    ap.add_argument("--company", default="At The Grove Nursery")
    ap.add_argument(
        "--qty-column",
        default="Current Quantity Goldberry Grove",
        help="catalog column holding current stock for the location being imported",
    )
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

    products = import_catalog(c, args.catalog, site[0], args.qty_column)
    if args.orders_dir:
        import_orders(c, args.orders_dir, products, wh[0], site[0])
    apply_inventory(c, products, wh[0])
    print("done.")


if __name__ == "__main__":
    main()
