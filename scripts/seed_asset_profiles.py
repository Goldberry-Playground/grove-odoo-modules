#!/usr/bin/env python3
"""Seed OCA asset profiles (account.asset.profile) for the farm and the nursery via XML-RPC.

Two profiles per company, straight-line by year, on accounts 1500 (asset), 1590 (accumulated
depreciation) and 6140 (depreciation expense), journal MISC of that company:
    "Machinery and equipment, 7 years"
    "Land improvements, 15 years"
Idempotent by (name, company). --dry-run prints the plan only.

Usage:
    ODOO_URL=... ODOO_DB=... ODOO_USER=... ODOO_PASSWORD=... \\
        python3 seed_asset_profiles.py [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "Goldberry")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

COMPANY_NAMES = {"farm": "Goldberry Grove Farm", "nursery": "At The Grove Nursery"}

JOURNAL_CODE = "MISC"
ASSET_CODE = "1500"
DEPRECIATION_CODE = "1590"
EXPENSE_CODE = "6140"

PROFILE_SPECS: list[dict] = [
    {"name": "Machinery and equipment, 7 years", "years": 7},
    {"name": "Land improvements, 15 years", "years": 15},
]

# account.asset.profile fields this seed may emit (checked against OCA account_asset_management).
ASSET_PROFILE_FIELDS = frozenset(
    {
        "name",
        "company_id",
        "journal_id",
        "account_asset_id",
        "account_depreciation_id",
        "account_expense_depreciation_id",
        "method",
        "method_time",
        "method_number",
        "method_period",
    }
)


# ------------------------------------------------------------------- pure
def profile_vals(spec: dict, accounts: dict, journal_id: int, company_id: int) -> dict:
    """account.asset.profile create vals. accounts maps account code -> account id."""
    return {
        "name": spec["name"],
        "company_id": company_id,
        "journal_id": journal_id,
        "account_asset_id": accounts[ASSET_CODE],
        "account_depreciation_id": accounts[DEPRECIATION_CODE],
        "account_expense_depreciation_id": accounts[EXPENSE_CODE],
        "method": "linear",
        "method_time": "year",
        "method_number": spec["years"],
        "method_period": "year",
    }


# --------------------------------------------------------------- plumbing
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


def find_one(models, uid, model, domain, context=None):
    kwargs = {"limit": 1}
    if context:
        kwargs["context"] = context
    ids = call(models, uid, model, "search", [domain], kwargs)
    return ids[0] if ids else None


def write_or_plan(models, uid, dry_run, model, method, args, kwargs=None, label=""):
    """The ONLY path for writes. In dry-run it prints the plan and returns None."""
    if dry_run:
        print(f"  PLAN {label or f'{model}.{method}'}")
        return None
    return call(models, uid, model, method, args, kwargs)


def ctx(company_id: int, **extra) -> dict:
    return {"allowed_company_ids": [company_id], **extra}


def account_by_code(models, uid, company_id, code):
    return find_one(
        models,
        uid,
        "account.account",
        [("code", "=", code), ("company_ids", "in", [company_id])],
        context=ctx(company_id),
    )


def company_id_by_name(models, uid, name):
    cid = find_one(models, uid, "res.company", [("name", "=", name)])
    if not cid:
        fail(f"company '{name}' not found")
    return cid


# ------------------------------------------------------------------ steps
def seed_company(models, uid, args, key):
    name = COMPANY_NAMES[key]
    company_id = company_id_by_name(models, uid, name)
    accounts = {}
    for code in (ASSET_CODE, DEPRECIATION_CODE, EXPENSE_CODE):
        account = account_by_code(models, uid, company_id, code)
        if not account:
            fail(f"account {code} not found in {name} (run setup_books.py first)")
        accounts[code] = account
    journal_id = find_one(
        models,
        uid,
        "account.journal",
        [("code", "=", JOURNAL_CODE), ("company_id", "=", company_id)],
        context=ctx(company_id),
    )
    if not journal_id:
        fail(f"journal {JOURNAL_CODE} not found in {name} (run setup_books.py first)")
    for spec in PROFILE_SPECS:
        existing = find_one(
            models,
            uid,
            "account.asset.profile",
            [("name", "=", spec["name"]), ("company_id", "=", company_id)],
            context=ctx(company_id),
        )
        if existing:
            print(f"  SKIP {spec['name']} - exists (id={existing})")
            continue
        print(f"  CREATE {spec['name']}")
        write_or_plan(
            models,
            uid,
            args.dry_run,
            "account.asset.profile",
            "create",
            [profile_vals(spec, accounts, journal_id, company_id)],
            {"context": ctx(company_id)},
            label=spec["name"],
        )


def run(models, uid, args):
    if args.dry_run:
        print("*** DRY RUN (dry-run: no writes) ***")
    for key in ("farm", "nursery"):
        print(f"\n== Asset profiles ({key}) ==")
        seed_company(models, uid, args, key)
    print("\nDone.")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true", help="print the plan; perform no writes")
    return p.parse_args(argv)


def main():
    args = parse_args()
    models, uid = authenticate()
    run(models, uid, args)


if __name__ == "__main__":
    main()
