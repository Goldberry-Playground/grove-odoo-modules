#!/usr/bin/env python3
"""Seed Goldberry payment journals into Odoo via XML-RPC.

Idempotent: re-running skips journals whose `code` already exists.

Usage:
    ODOO_PASSWORD=... python3 seed_payment_journals.py
"""

from __future__ import annotations

import os
import sys
import xmlrpc.client
from typing import Any

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "Goldberry")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

# Odoo journal types: 'sale', 'purchase', 'cash', 'bank', 'credit', 'general'
JOURNALS: list[dict[str, Any]] = [
    {
        "name": "Cash",
        "code": "CSH1",
        "type": "cash",
        "note": "Walk-in cash receipts (farmer's market, nursery counter)",
    },
    {
        "name": "Card",
        "code": "CARD",
        "type": "bank",
        "note": "POS card payments — settles to bank via processor",
    },
    {
        "name": "Check",
        "code": "CHCK",
        "type": "bank",
        "note": "Customer checks — deposited to bank",
    },
    {
        "name": "Online Payment",
        "code": "ONLN",
        "type": "bank",
        "note": "Website payment gateway (Stripe/etc) settlements",
    },
    {
        "name": "Invoice (Net 30)",
        "code": "INV2",
        "type": "sale",
        "note": "Net-30 invoicing for landscapers and wholesale accounts",
    },
]


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


def lookup_id(models, uid, model, domain, label):
    ids = call(models, uid, model, "search", [domain], {"limit": 1})
    if not ids:
        fail(f"Could not find {model} matching {domain} ({label})")
    return ids[0]


def seed_journal(models, uid, company_id, journal):
    code = journal["code"]
    existing = call(
        models, uid, "account.journal", "search", [[("code", "=", code), ("company_id", "=", company_id)]], {"limit": 1}
    )
    if existing:
        print(f"  SKIP {code} ({journal['name']}) — already exists (id={existing[0]})")
        return

    vals = {
        "name": journal["name"],
        "code": code,
        "type": journal["type"],
        "company_id": company_id,
    }
    new_id = call(models, uid, "account.journal", "create", [vals])
    print(f"  CREATE {code} → account.journal id={new_id} ({journal['name']}) — {journal['note']}")


def main():
    models, uid = authenticate()
    company_id = lookup_id(models, uid, "res.company", [("name", "=", "Goldberry Grove Farm")], "Goldberry Grove Farm")
    print(f"Target company_id={company_id} (Goldberry Grove Farm)\n")
    print(f"Seeding {len(JOURNALS)} payment journals:")
    for journal in JOURNALS:
        seed_journal(models, uid, company_id, journal)
    print("\nDone.")


if __name__ == "__main__":
    main()
