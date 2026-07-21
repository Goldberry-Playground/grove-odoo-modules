#!/usr/bin/env python3
"""Remediate the GOL-641 seed-run duplication.

The first ``seed_variety_products.py`` run keyed idempotency on its own SKUs
(``VINE-KIWI``, ``SHRUB-FIG`` ...) which never equal the live template codes
(``KIWI-GH``, ``FIG-AJ`` ...), so it created *parallel* templates 178-182
instead of extending the canonical originals. The ``grove_slug`` collision
auto-suffixed the dups (``fig-179`` etc) and the shop now shows doubles.

This one-shot migration merges each duplicate's cultivars into the CANONICAL
original, then archives the duplicate. Originals win every merge — they carry
the Format axis, the real stock (persimmon 163 has Meader Potted qty 33) and
the un-suffixed ``grove_slug`` the frontend + photo-sheets key on.

Merge map (Josh, Asana 1216758513298024 — he gates the writes):

    dup id  ->  canonical original       species
    178         158                       Kiwi
    179         155                       Fig
    180         162                       Pear
    181         163  (real stock!)        Persimmon
    182         168  'Service Berry'      Serviceberry
    183         --   keep as-is           Aronia (genuinely new; no original)

Plus (correction on Asana 1216653849836945): add cultivar "Mount Royal" to
Plum (164) at price_extra $0, then archive the orphan template 166 "royal".

Idempotency: re-running is safe. A cultivar value already present on the
original is skipped; an already-archived dup is skipped; Mount Royal is added
only if missing. The script never deletes — it archives (``active=False``).

The IDs are hard-coded on purpose: this is a single, audited remediation, and
the script asserts each id resolves to the expected species name so it fails
loudly if the catalog has shifted rather than mutating the wrong template.

Usage
-----
    ODOO_URL=http://localhost:8069 ODOO_DB=odoo \\
    ODOO_USER=josh@goldberrygrove.farm ODOO_PASSWORD=<admin> \\
    DRY_RUN=1 python3 scripts/remediate_seed_duplicates.py   # plan only
    # DRY_RUN unset -> applies the merge + archives.

Exit codes: 0 ok, 1 auth/validation failure (a mismatched id aborts before any
write).
"""

from __future__ import annotations

import os
import sys
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
DRY_RUN = os.getenv("DRY_RUN") == "1"

COMPANY_NAME = "At The Grove Nursery"
CULTIVAR_ATTR = "Cultivar"
FORMAT_ABBR = {"Potted": "PT", "Bareroot": "BR"}

# (dup_id, original_id, expected species name fragment on BOTH, sku species code)
MERGE_MAP = [
    (178, 158, "kiwi", "KIWI"),
    (179, 155, "fig", "FIG"),
    (180, 162, "pear", "PEAR"),
    (181, 163, "persimmon", "PERSIMMON"),
    (182, 168, "serviceberry", "SERVICEBERRY"),  # original is 'Service Berry'
]
# Mount Royal correction.
PLUM_ID = 164
MOUNT_ROYAL = "Mount Royal"
ROYAL_ORPHAN_ID = 166


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def norm(name: str) -> str:
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


def call(models, uid, model, method, args, kwargs=None):
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


class Odoo:
    def __init__(self, models, uid, company_id):
        self.m = models
        self.uid = uid
        self.company_id = company_id
        self.ctx = {"allowed_company_ids": [company_id], "company_id": company_id}
        # Cultivar attribute + main stock location, resolved once.
        attr = call(models, uid, "product.attribute", "search", [[("name", "=", CULTIVAR_ATTR)]], {"limit": 1})
        if not attr:
            fail(f"'{CULTIVAR_ATTR}' attribute not found")
        self.cultivar_attr = attr[0]
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
        self.stock_location_id = wh[0]["lot_stock_id"][0]

    def c(self, model, method, args, kwargs=None):
        return call(self.m, self.uid, model, method, args, kwargs)

    def template(self, tid: int) -> dict:
        rows = self.c(
            "product.template",
            "read",
            [[tid]],
            {
                "fields": ["id", "name", "active", "default_code", "attribute_line_ids"],
                "context": {"active_test": False},
            },
        )
        if not rows:
            fail(f"template id={tid} not found")
        return rows[0]

    def cultivar_line(self, tid: int) -> int | None:
        lines = self.c(
            "product.template.attribute.line",
            "search",
            [[("product_tmpl_id", "=", tid), ("attribute_id", "=", self.cultivar_attr)]],
            {},
        )
        return lines[0] if lines else None

    def cultivar_values_on(self, tid: int) -> dict[str, dict]:
        """name -> {value_id, ptav_id, price_extra} for the template's Cultivar values."""
        ptavs = self.c(
            "product.template.attribute.value",
            "search_read",
            [[("product_tmpl_id", "=", tid), ("attribute_id", "=", self.cultivar_attr)]],
            {"fields": ["id", "name", "price_extra", "product_attribute_value_id"], "context": {"active_test": False}},
        )
        out = {}
        for p in ptavs:
            out[norm(p["name"])] = {
                "display": p["name"],
                "ptav_id": p["id"],
                "price_extra": p["price_extra"],
                "value_id": p["product_attribute_value_id"][0],
            }
        return out

    def variant_qty(self, tid: int, cultivar_norm: str) -> tuple[int, str]:
        """Opening on-hand qty and Format for the dup's variant of this cultivar."""
        variants = self.c(
            "product.product",
            "search_read",
            [[("product_tmpl_id", "=", tid)]],
            {"fields": ["id", "default_code", "product_template_variant_value_ids"], "context": {"active_test": False}},
        )
        for v in variants:
            names = self.c(
                "product.template.attribute.value",
                "read",
                [v["product_template_variant_value_ids"]],
                {"fields": ["name", "attribute_id"]},
            )
            cult = next((n["name"] for n in names if n["attribute_id"][0] == self.cultivar_attr), None)
            fmt = next((n["name"] for n in names if n["attribute_id"][0] != self.cultivar_attr), "Potted")
            if cult and norm(cult) == cultivar_norm:
                quant = self.c(
                    "stock.quant",
                    "search_read",
                    [[("product_id", "=", v["id"]), ("location_id", "=", self.stock_location_id)]],
                    {"fields": ["quantity"]},
                )
                qty = int(sum(q["quantity"] for q in quant))
                return qty, fmt
        return 0, "Potted"

    def ensure_value(self, name: str) -> int:
        ids = self.c(
            "product.attribute.value",
            "search",
            [[("name", "=", name), ("attribute_id", "=", self.cultivar_attr)]],
            {"limit": 1},
        )
        if ids:
            return ids[0]
        if DRY_RUN:
            print(f"      + WOULD CREATE Cultivar value '{name}'")
            return 0
        return self.c("product.attribute.value", "create", [{"name": name, "attribute_id": self.cultivar_attr}])


def add_cultivar(o: Odoo, original_id: int, name: str, price_extra: float, qty: int, sku_prefix: str) -> None:
    """Append one cultivar value to the original's Cultivar line, set price_extra,
    opening stock and per-variant SKU. Idempotent: skips if already present."""
    have = o.cultivar_values_on(original_id)
    if norm(name) in have:
        print(f"      = '{name}' already on original (id={original_id}); skipped")
        return
    line = o.cultivar_line(original_id)
    if line is None:
        fail(f"original {original_id} has no Cultivar attribute line; manual review needed")
    if DRY_RUN:
        print(f"      + WOULD ADD '{name}' price_extra=+${price_extra:.2f} qty={qty} to template {original_id}")
        return
    value_id = o.ensure_value(name)
    o.c("product.template.attribute.line", "write", [[line], {"value_ids": [(4, value_id)]}])
    # New ptav now exists on the original — set its price delta.
    ptav = o.c(
        "product.template.attribute.value",
        "search",
        [[("product_tmpl_id", "=", original_id), ("product_attribute_value_id", "=", value_id)]],
        {"limit": 1},
    )
    if ptav and price_extra:
        o.c("product.template.attribute.value", "write", [ptav, {"price_extra": price_extra}])
    # New variant(s) for this cultivar: set SKU + opening quant on the Potted one.
    variants = o.c(
        "product.product",
        "search_read",
        [[("product_tmpl_id", "=", original_id)]],
        {"fields": ["id", "product_template_variant_value_ids"]},
    )
    for v in variants:
        names = o.c(
            "product.template.attribute.value",
            "read",
            [v["product_template_variant_value_ids"]],
            {"fields": ["name", "attribute_id"]},
        )
        cult = next((n["name"] for n in names if n["attribute_id"][0] == o.cultivar_attr), None)
        fmt = next((n["name"] for n in names if n["attribute_id"][0] != o.cultivar_attr), "Potted")
        if not cult or norm(cult) != norm(name):
            continue
        abbr = FORMAT_ABBR.get(fmt, "PT")
        code = "-".join([sku_prefix, name.split()[0][:3].upper(), abbr])
        o.c("product.product", "write", [[v["id"]], {"default_code": code}])
        if fmt == "Potted" and qty:
            quant_id = o.c(
                "stock.quant",
                "create",
                [{"product_id": v["id"], "location_id": o.stock_location_id, "inventory_quantity": qty}],
                {"context": {"inventory_mode": True, **o.ctx}},
            )
            o.c("stock.quant", "action_apply_inventory", [[quant_id]], {"context": o.ctx})
        print(f"      + added '{name}' -> variant {code} ({fmt}) qty={qty if fmt == 'Potted' else 0}")


def archive(o: Odoo, tid: int, label: str) -> None:
    t = o.template(tid)
    if not t["active"]:
        print(f"    = template {tid} ({label}) already archived")
        return
    if DRY_RUN:
        print(f"    + WOULD ARCHIVE template {tid} ({t['name']})")
        return
    o.c("product.template", "write", [[tid], {"active": False}])
    print(f"    + archived template {tid} ({t['name']})")


def main() -> None:
    print(f"Target: {ODOO_URL} db={ODOO_DB}  DRY_RUN={'yes' if DRY_RUN else 'NO — LIVE'}")
    models, uid = authenticate()
    cids = call(models, uid, "res.company", "search", [[("name", "=", COMPANY_NAME)]], {"limit": 1})
    if not cids:
        fail(f"Company '{COMPANY_NAME}' not found")
    o = Odoo(models, uid, cids[0])

    print("\n── Validate merge map ──")
    for dup_id, orig_id, species, _ in MERGE_MAP:
        d, t = o.template(dup_id), o.template(orig_id)
        if species not in norm(d["name"]):
            fail(f"dup {dup_id} name '{d['name']}' != expected species '{species}'")
        if species not in norm(t["name"]):
            fail(f"original {orig_id} name '{t['name']}' != expected species '{species}'")
        print(f"  ok  dup {dup_id} '{d['name']}' -> original {orig_id} '{t['name']}'")
    plum = o.template(PLUM_ID)
    if "plum" not in norm(plum["name"]):
        fail(f"template {PLUM_ID} '{plum['name']}' is not Plum")
    print(f"  ok  Plum {PLUM_ID} '{plum['name']}'; orphan {ROYAL_ORPHAN_ID} '{o.template(ROYAL_ORPHAN_ID)['name']}'")

    print("\n── Merge duplicates into originals ──")
    for dup_id, orig_id, species, sku in MERGE_MAP:
        print(f"  {species}: dup {dup_id} -> original {orig_id}")
        dup_cults = o.cultivar_values_on(dup_id)
        if not dup_cults:
            print("      (dup has no Cultivar values — Format-only; nothing to merge)")
        for cnorm, info in dup_cults.items():
            qty, fmt = o.variant_qty(dup_id, cnorm)
            add_cultivar(o, orig_id, info["display"], info["price_extra"], qty, sku)
        archive(o, dup_id, species)

    print("\n── Mount Royal correction ──")
    add_cultivar(o, PLUM_ID, MOUNT_ROYAL, 0.0, 0, "PLUM")
    archive(o, ROYAL_ORPHAN_ID, "royal orphan")

    print("\nDone." + (" (dry run — nothing written)" if DRY_RUN else ""))


if __name__ == "__main__":
    main()
