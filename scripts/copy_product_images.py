#!/usr/bin/env python3
"""Copy product template images from one Odoo (SOURCE) to another (DEST) by SKU.

Built for GOL-2020 (stage the 11 "coming soon" species' QA photos into PROD after
``seed_coming_soon_products.py PUBLISHED=0`` has created the pictureless records),
but deliberately generalised to an ARBITRARY SKU set so the same tool serves the
QA -> prod photo migration wanted for the existing catalog (GOL-1329).

What it copies, per resolved template:
  * the template hero (``product.template.image_1920``), and
  * every gallery row (``product.image`` — the extra-media carousel), by name.

Matching (SKU only — never name, never id; ids differ between environments)
--------------------------------------------------------------------------
A "SKU" is resolved to exactly ONE template in each env, trying in order:
  1. ``product.template.default_code == SKU``  (single-variant products, whose
     code mirrors up to the template), then
  2. ``product.product.default_code == SKU``   (a variant's own code), then
  3. ``product.product.default_code =like "SKU-%"``  (SKU is a base/species code
     and the variants are ``SKU-PT`` / ``SKU-BR`` etc.).
The union of candidate template ids must be exactly one, or the SKU is reported
as ambiguous/missing and SKIPPED (never guessed). The coming-soon 11 are
multi-variant, so their template ``default_code`` is False and they resolve via
rule 3 using their BASE codes (``CHE``, ``BLKCHERRY``, ...) — which is why the
default SKU set below is the seed script's ``code`` values, NOT its ``TREE-*``
``sku`` values (those exist on no record).

Idempotency & safety
--------------------
  * NEVER clobbers a hero already present in DEST — if the dest template already
    has ``image_1920``, the hero is SKIPPED (re-runs converge; a better/newer
    prod image is preserved). Use ``FORCE_HERO=1`` to overwrite deliberately.
  * Gallery rows are matched by name; an existing dest row of the same name is
    left untouched, only missing rows are created. No duplicates on re-run.
  * ``DRY_RUN=1`` reports what WOULD be copied and writes nothing (consistent
    with the other seed scripts).
  * The two ``AAA QA E2E`` fixtures (``E2E-BAREROOT-INSTOCK`` /
    ``E2E-POTTED-INSTOCK``) are HARD-EXCLUDED from any SKU set — they are
    ``sale_ok=True`` at $42 and must never be propagated toward prod.

Per-SKU report: COPIED / SKIPPED (dest already has it) / MISSING (no image in
source, or template not found in one side).

Usage
-----
    # Dry run: coming-soon 11, QA -> prod, report only.
    DRY_RUN=1 \\
    SRC_ODOO_URL=https://odoo.qa.gatheringatthegrove.com SRC_ODOO_DB=odoo \\
    SRC_ODOO_USER=josh@goldberrygrove.farm SRC_ODOO_PASSWORD=<qa-key> \\
    DST_ODOO_URL=https://odoo.prod... DST_ODOO_DB=odoo \\
    DST_ODOO_USER=josh@goldberrygrove.farm DST_ODOO_PASSWORD=<prod-key> \\
    python3 scripts/copy_product_images.py

    # Live: drop DRY_RUN. Arbitrary set: SKUS=APPL,PERS,ARONIA (comma-separated).

Exit codes: 0 ok (even with per-SKU MISSING — those are reported, not fatal),
1 auth/config failure or an unexpected write error.
"""

from __future__ import annotations

import os
import sys
import xmlrpc.client

DRY_RUN = os.getenv("DRY_RUN") == "1"
FORCE_HERO = os.getenv("FORCE_HERO") == "1"

# Source (read) — defaults to QA, the canonical photo origin.
SRC_URL = os.getenv("SRC_ODOO_URL", "https://odoo.qa.gatheringatthegrove.com")
SRC_DB = os.getenv("SRC_ODOO_DB", "odoo")
SRC_USER = os.getenv("SRC_ODOO_USER", "josh@goldberrygrove.farm")
SRC_PASSWORD = os.getenv("SRC_ODOO_PASSWORD")

# Dest (write) — no default host: writing to the wrong Odoo is the failure mode
# to avoid, so DEST must be given explicitly.
DST_URL = os.getenv("DST_ODOO_URL")
DST_DB = os.getenv("DST_ODOO_DB", "odoo")
DST_USER = os.getenv("DST_ODOO_USER", "josh@goldberrygrove.farm")
DST_PASSWORD = os.getenv("DST_ODOO_PASSWORD")

COMPANY_NAME = os.getenv("GROVE_COMPANY_NAME", "At The Grove Nursery")

# AAA QA E2E fixtures — never propagate these toward prod (sale_ok=True @ $42).
BLOCKED_SKUS = {"E2E-BAREROOT-INSTOCK", "E2E-POTTED-INSTOCK"}


def _default_skus() -> list[str]:
    """The GOL-2020 coming-soon set: the seed script's BASE ``code`` values.

    These resolve to the multi-variant templates via the ``SKU-%`` variant-prefix
    rule (variants are ``CHE-PT`` / ``CHE-BR`` ...). Importing keeps a single
    source of truth for the eleven and their codes.
    """
    from seed_coming_soon_products import PRODUCTS  # local, same scripts/ dir

    return [p["code"] for p in PRODUCTS]


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


class Odoo:
    """Thin XML-RPC handle so SRC and DEST calls stay unambiguous."""

    def __init__(self, label: str, url: str, db: str, user: str, password: str):
        self.label = label
        self.url = url
        self.db = db
        self.uid_pw = password
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        self.uid = common.authenticate(db, user, password, {})
        if not self.uid:
            fail(f"[{label}] authentication failed for {user} on db {db} @ {url}")
        self.models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        print(f"[{label}] authenticated uid={self.uid} db={db} @ {url}")

    def call(self, model: str, method: str, args: list, kwargs: dict | None = None):
        return self.models.execute_kw(self.db, self.uid, self.uid_pw, model, method, args, kwargs or {})


def resolve_template(odoo: Odoo, sku: str, company_id: int) -> int | None:
    """Resolve a SKU to exactly one template id, or None (0 / ambiguous). SKU only."""
    comp = [("company_id", "in", [company_id, False])]
    tids: set[int] = set(odoo.call("product.template", "search", [[("default_code", "=", sku)] + comp]))
    for dom in ([("default_code", "=", sku)], [("default_code", "=like", f"{sku}-%")]):
        variants = odoo.call("product.product", "search_read", [dom + comp], {"fields": ["product_tmpl_id"]})
        for v in variants:
            if v["product_tmpl_id"]:
                tids.add(v["product_tmpl_id"][0])
    if len(tids) == 1:
        return next(iter(tids))
    if not tids:
        print(f"  MISSING  {sku}: no template found in [{odoo.label}]")
    else:
        print(f"  AMBIGUOUS {sku}: resolves to {sorted(tids)} in [{odoo.label}] — refusing to guess")
    return None


def company_id_of(odoo: Odoo) -> int:
    ids = odoo.call("res.company", "search", [[("name", "=", COMPANY_NAME)]], {"limit": 1})
    if not ids:
        fail(f"[{odoo.label}] company '{COMPANY_NAME}' not found")
    return ids[0]


def copy_hero(src: Odoo, dst: Odoo, sku: str, src_tmpl: int, dst_tmpl: int, dst_ctx: dict) -> str:
    src_img = src.call("product.template", "read", [[src_tmpl]], {"fields": ["image_1920"]})[0]["image_1920"]
    if not src_img:
        return f"  MISSING  {sku}: hero — source template {src_tmpl} has no image_1920"
    dst_img = dst.call("product.template", "read", [[dst_tmpl]], {"fields": ["image_1920"]})[0]["image_1920"]
    if dst_img and not FORCE_HERO:
        return f"  SKIP     {sku}: hero — dest template {dst_tmpl} already has an image (not clobbering)"
    verb = "OVERWRITE" if (dst_img and FORCE_HERO) else "COPY"
    if DRY_RUN:
        return f"  WOULD {verb} {sku}: hero -> dest template {dst_tmpl}"
    dst.call("product.template", "write", [[dst_tmpl], {"image_1920": src_img}], dst_ctx)
    return f"  {verb:9} {sku}: hero -> dest template {dst_tmpl}"


def copy_gallery(src: Odoo, dst: Odoo, sku: str, src_tmpl: int, dst_tmpl: int, dst_ctx: dict) -> list[str]:
    src_rows = src.call(
        "product.image", "search_read", [[("product_tmpl_id", "=", src_tmpl)]], {"fields": ["name", "image_1920"]}
    )
    if not src_rows:
        return []
    existing = {
        r["name"]
        for r in dst.call("product.image", "search_read", [[("product_tmpl_id", "=", dst_tmpl)]], {"fields": ["name"]})
    }
    out: list[str] = []
    for row in src_rows:
        name = row["name"]
        if name in existing:
            out.append(f"  SKIP     {sku}: gallery '{name}' — dest already has a row of this name")
            continue
        if not row["image_1920"]:
            out.append(f"  MISSING  {sku}: gallery '{name}' — source row has no image_1920")
            continue
        if DRY_RUN:
            out.append(f"  WOULD COPY {sku}: gallery '{name}' -> dest template {dst_tmpl}")
            continue
        dst.call(
            "product.image",
            "create",
            [{"name": name, "image_1920": row["image_1920"], "product_tmpl_id": dst_tmpl}],
            dst_ctx,
        )
        out.append(f"  COPY      {sku}: gallery '{name}' -> dest template {dst_tmpl}")
    return out


def main() -> None:
    if not SRC_PASSWORD:
        fail("SRC_ODOO_PASSWORD is required")
    if not DST_URL:
        fail("DST_ODOO_URL is required (no default — refuse to guess the write target)")
    if not DST_PASSWORD:
        fail("DST_ODOO_PASSWORD is required")

    raw = os.getenv("SKUS", "")
    skus = [s.strip() for s in raw.split(",") if s.strip()] if raw else _default_skus()
    blocked = [s for s in skus if s in BLOCKED_SKUS]
    skus = [s for s in skus if s not in BLOCKED_SKUS]
    if blocked:
        print(f"Excluding blocked fixture SKUs (never propagated to prod): {blocked}")
    if not skus:
        fail("No SKUs to process after exclusions")

    print(
        f"Copy images  SRC=[{SRC_URL}] -> DST=[{DST_URL}]  "
        f"company={COMPANY_NAME}  {len(skus)} SKUs  "
        f"FORCE_HERO={'yes' if FORCE_HERO else 'no'}  DRY_RUN={'yes' if DRY_RUN else 'NO — LIVE'}"
    )
    print(f"SKUs: {', '.join(skus)}\n")

    src = Odoo("SRC", SRC_URL, SRC_DB, SRC_USER, SRC_PASSWORD)
    dst = Odoo("DST", DST_URL, DST_DB, DST_USER, DST_PASSWORD)
    src_company = company_id_of(src)
    dst_company = company_id_of(dst)
    dst_ctx = {"context": {"allowed_company_ids": [dst_company], "company_id": dst_company}}

    print("\n── Copy ──")
    n_copied = n_skipped = n_missing = 0
    for sku in skus:
        src_tmpl = resolve_template(src, sku, src_company)
        dst_tmpl = resolve_template(dst, sku, dst_company)
        if src_tmpl is None or dst_tmpl is None:
            # resolve_template already printed the reason; count as missing and move on.
            n_missing += 1
            continue
        lines = [copy_hero(src, dst, sku, src_tmpl, dst_tmpl, dst_ctx)]
        lines += copy_gallery(src, dst, sku, src_tmpl, dst_tmpl, dst_ctx)
        for ln in lines:
            print(ln)
            tag = ln.strip().split(None, 1)[0]
            if tag in ("COPY", "OVERWRITE", "WOULD"):
                n_copied += 1
            elif tag == "SKIP":
                n_skipped += 1
            elif tag in ("MISSING", "AMBIGUOUS"):
                n_missing += 1

    print(
        f"\nDone. copied={n_copied} skipped={n_skipped} missing/ambiguous={n_missing}"
        + (" (dry run — nothing written)" if DRY_RUN else "")
    )


if __name__ == "__main__":
    main()
