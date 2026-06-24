#!/usr/bin/env python3
"""Bind the WV 6% state + 1% municipal sales tax in Odoo via XML-RPC.

This is the *run-now* path for an already-running production database — it
applies the same fix as the grove_headless post_init_hook / migration without
needing a module redeploy. Prefer the module upgrade (`-u grove_headless`) for
fresh environments; use this when you want to fix the live DB immediately.

For every company it:
  1. Finds-or-creates "WV State Sales Tax 6%" + "WV Municipal Tax 1%" (sale).
  2. Finds-or-creates the combined "WV Sales Tax 7%" group tax.
  3. Sets the company default sale tax (ir.default product.template.taxes_id
     + best-effort res.company.account_sale_tax_id).
  4. Retrofits existing sale-able products onto the WV group tax.

Idempotent: re-running finds existing taxes by name + company and only fills
in what is missing.

Usage:
    ODOO_PASSWORD=... python3 setup_wv_taxes.py
"""

from __future__ import annotations

import os
import sys
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "Goldberry")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

WV_STATE_NAME = "WV State Sales Tax 6%"
WV_MUNI_NAME = "WV Municipal Tax 1%"
WV_GROUP_NAME = "WV Sales Tax 7%"


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


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


def find_tax(models, uid, company_id, name, amount_type):
    ids = call(
        models,
        uid,
        "account.tax",
        "search",
        [
            [
                ("name", "=", name),
                ("company_id", "=", company_id),
                ("type_tax_use", "=", "sale"),
                ("amount_type", "=", amount_type),
            ]
        ],
        {"limit": 1},
    )
    return ids[0] if ids else None


def ensure_component(models, uid, company_id, name, amount, description):
    existing = find_tax(models, uid, company_id, name, "percent")
    if existing:
        print(f"  SKIP {name} — exists (id={existing})")
        return existing
    new_id = call(
        models,
        uid,
        "account.tax",
        "create",
        [
            {
                "name": name,
                "amount": amount,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "company_id": company_id,
                "description": description,
            }
        ],
    )
    print(f"  CREATE {name} → account.tax id={new_id}")
    return new_id


def ensure_group(models, uid, company_id, child_ids):
    existing = find_tax(models, uid, company_id, WV_GROUP_NAME, "group")
    if existing:
        call(models, uid, "account.tax", "write", [[existing], {"children_tax_ids": [(6, 0, child_ids)]}])
        print(f"  SKIP {WV_GROUP_NAME} — exists (id={existing}), children synced")
        return existing
    new_id = call(
        models,
        uid,
        "account.tax",
        "create",
        [
            {
                "name": WV_GROUP_NAME,
                "amount_type": "group",
                "type_tax_use": "sale",
                "company_id": company_id,
                "description": "WV 7%",
                "children_tax_ids": [(6, 0, child_ids)],
            }
        ],
    )
    print(f"  CREATE {WV_GROUP_NAME} → account.tax id={new_id}")
    return new_id


def set_company_default(models, uid, company_id, group_id):
    call(models, uid, "ir.default", "set", ["product.template", "taxes_id", [group_id]], {"company_id": company_id})
    print(f"  SET ir.default product.template.taxes_id = [{group_id}]")
    try:
        call(models, uid, "res.company", "write", [[company_id], {"account_sale_tax_id": group_id}])
        print(f"  SET res.company.account_sale_tax_id = {group_id}")
    except xmlrpc.client.Fault as exc:
        print(
            f"  NOTE could not set account_sale_tax_id ({exc.faultString.splitlines()[-1]}); ir.default still applies"
        )


def retrofit_products(models, uid, company_id, group_id):
    tmpl_ids = call(
        models, uid, "product.template", "search", [[("sale_ok", "=", True), ("company_id", "in", [company_id, False])]]
    )
    changed = 0
    for tmpl_id in tmpl_ids:
        rec = call(models, uid, "product.template", "read", [[tmpl_id], ["taxes_id"]])[0]
        current = rec["taxes_id"]
        if current == [group_id]:
            continue
        # Keep taxes scoped to other companies; replace this company's sale taxes.
        if current:
            taxes = call(models, uid, "account.tax", "read", [current, ["company_id"]])
            foreign = [t["id"] for t in taxes if t["company_id"] and t["company_id"][0] != company_id]
        else:
            foreign = []
        call(models, uid, "product.template", "write", [[tmpl_id], {"taxes_id": [(6, 0, foreign + [group_id])]}])
        changed += 1
    print(f"  RETROFIT {changed} product template(s) onto WV group tax")


def main():
    models, uid = authenticate()
    company_ids = call(models, uid, "res.company", "search", [[]])
    companies = call(models, uid, "res.company", "read", [company_ids, ["name"]])
    print(f"Applying WV sales tax to {len(companies)} companies:\n")
    for company in companies:
        cid, name = company["id"], company["name"]
        print(f"== {name} (id={cid}) ==")
        state_id = ensure_component(models, uid, cid, WV_STATE_NAME, 6.0, "WV 6%")
        muni_id = ensure_component(models, uid, cid, WV_MUNI_NAME, 1.0, "Muni 1%")
        group_id = ensure_group(models, uid, cid, [state_id, muni_id])
        set_company_default(models, uid, cid, group_id)
        retrofit_products(models, uid, cid, group_id)
        print()
    print("Done.")


if __name__ == "__main__":
    main()
