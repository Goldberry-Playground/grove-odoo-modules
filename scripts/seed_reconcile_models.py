#!/usr/bin/env python3
"""Seed manual-trigger reconcile models for the shared bank via XML-RPC.

Models are suggestions only (trigger=manual); nothing auto-posts. Each books 100% to one
account, with an Enterprise analytic distribution where the spec names one. Farm models
cover the shared bank; nursery models cover the Stripe and Square clearing journals.
Idempotent by (name, company). --dry-run prints the plan only.

Usage:
    ODOO_URL=... ODOO_DB=... ODOO_USER=... ODOO_PASSWORD=... \\
        python3 seed_reconcile_models.py [--dry-run]
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


STRIPE_NOTE = "two Stripe accounts share this descriptor; match the amount to a payout in each Stripe journal"

MODEL_SPECS: list[dict] = [
    {
        "company": "farm",
        "name": "Stripe payout (nursery)",
        "name_suffix": "(check amount against both Stripe journals)",
        "match_label": "contains",
        "match_label_param": "STRIPE",
        "trigger": "manual",
        "account_code": "1350",
        "enterprise": "nursery",
        "amount": "in",
        "note": STRIPE_NOTE,
    },
    {
        "company": "farm",
        "name": "Square deposit",
        "match_label": "contains",
        "match_label_param": "SQUARE",
        "trigger": "manual",
        "account_code": "1350",
        "enterprise": "nursery",
        "note": "Square deposit into the shared bank; settles the nursery's Square clearing",
    },
    {
        "company": "farm",
        "name": "Venmo cashout",
        "match_label": "contains",
        "match_label_param": "VENMO",
        "trigger": "manual",
        "account_code": "1350",
        "enterprise": "nursery",
        "note": "Venmo cashout into the shared bank; settles the nursery's Venmo clearing",
    },
    {
        "company": "farm",
        "name": "Owner draw",
        "match_label": "contains",
        "match_label_param": "TRANSFER TO",
        "trigger": "manual",
        "account_code": "3100",
        "enterprise": "personal",
        "amount": "out",
        "note": "Transfer out to the owner; not a farm expense",
    },
    {
        "company": "nursery",
        "name": "Stripe payout to shared bank",
        "match_label": "contains",
        "match_label_param": "payout",
        "trigger": "manual",
        "account_code": "1350",
        "note": "Stripe payout that landed in the farm's shared bank; due to the farm via intercompany",
    },
    {
        "company": "nursery",
        "name": "Square transfer to bank",
        "match_label": "contains",
        "match_label_param": "transfer",
        "trigger": "manual",
        "account_code": "1350",
        "note": "Square transfer that landed in the farm's shared bank; due to the farm via intercompany",
    },
]

NATURE = {"in": "amount_received", "out": "amount_paid", None: "both"}


# ------------------------------------------------------------------- pure
def model_name(spec: dict) -> str:
    return f"{spec['name']} {spec['name_suffix']}" if spec.get("name_suffix") else spec["name"]


def model_vals(spec: dict, company_accounts: dict, enterprise_ids: dict) -> dict:
    """account.reconcile.model create vals.

    company_accounts maps account code -> account id, plus "company_id" -> the company id.
    enterprise_ids maps "nursery"/"personal" -> analytic account id.
    """
    line = {
        "account_id": company_accounts[spec["account_code"]],
        "amount_type": "percentage",
        "amount_string": "100",
        "label": spec["note"],
    }
    if spec.get("enterprise"):
        line["analytic_distribution"] = {str(enterprise_ids[spec["enterprise"]]): 100}
    return {
        "name": model_name(spec),
        "rule_type": "writeoff_suggestion",
        "trigger": spec["trigger"],
        "match_label": spec["match_label"],
        "match_label_param": spec["match_label_param"],
        "match_nature": NATURE[spec.get("amount")],
        "company_id": company_accounts["company_id"],
        "line_ids": [(0, 0, line)],
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
def enterprise_ids_for(models, uid, company_id) -> dict:
    plan = find_one(models, uid, "account.analytic.plan", [("name", "=", "Enterprise")])
    if not plan:
        return {}
    rows = call(
        models,
        uid,
        "account.analytic.account",
        "search_read",
        [[("plan_id", "=", plan), ("company_id", "in", [company_id, False])]],
        {"fields": ["name"], "context": ctx(company_id)},
    )
    return {r["name"].lower(): r["id"] for r in rows}


def seed_company(models, uid, args, key, specs):
    company_id = company_id_by_name(models, uid, COMPANY_NAMES[key])
    accounts = {"company_id": company_id}
    for code in sorted({s["account_code"] for s in specs}):
        account = account_by_code(models, uid, company_id, code)
        if not account:
            fail(f"account {code} not found in {COMPANY_NAMES[key]} (run setup_books.py first)")
        accounts[code] = account
    enterprises = enterprise_ids_for(models, uid, company_id)
    for spec in specs:
        if spec.get("enterprise") and spec["enterprise"] not in enterprises:
            fail(f"Enterprise analytic account '{spec['enterprise']}' not found for {COMPANY_NAMES[key]}")
        vals = model_vals(spec, accounts, enterprises)
        existing = find_one(
            models,
            uid,
            "account.reconcile.model",
            [("name", "=", vals["name"]), ("company_id", "=", company_id)],
            context=ctx(company_id),
        )
        if existing:
            print(f"  SKIP {vals['name']} - exists (id={existing})")
            continue
        print(f"  CREATE {vals['name']} (-> {spec['account_code']})")
        write_or_plan(
            models,
            uid,
            args.dry_run,
            "account.reconcile.model",
            "create",
            [vals],
            {"context": ctx(company_id)},
            label=vals["name"],
        )


def run(models, uid, args):
    if args.dry_run:
        print("*** DRY RUN (dry-run: no writes) ***")
    for key in ("farm", "nursery"):
        print(f"\n== Reconcile models ({key}) ==")
        seed_company(models, uid, args, key, [s for s in MODEL_SPECS if s["company"] == key])
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
