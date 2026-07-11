#!/usr/bin/env python3
"""Stand up the in-person POS channels in Odoo via XML-RPC.

This is the *run-now* path for an already-running database — it applies the
same configuration as the grove_headless post_init_hook / migration without a
module redeploy. Prefer the module upgrade (`-u grove_headless`) for fresh
environments; use this to configure the live DB immediately.

For the Goldberry Grove Farm company it:
  1. Finds-or-creates the CSH1 (cash) / CARD / CHCK (bank) payment journals.
  2. Finds-or-creates the Cash / Card / Check pos.payment.method records,
     each bound to its journal (cash-vs-bank is derived from the journal).
  3. Finds-or-creates the "Farmer's Market" and "Direct to Nursery" sales teams.
  4. Finds-or-creates the "Farmer's Market" and "Nursery Counter" pos.config
     records, wiring the three payment methods + the matching sales team.

WV 7% tax is applied via product taxes (see setup_wv_taxes.py), not the POS
config — POS lines inherit each product's taxes_id, already defaulted to the
"WV Sales Tax 7%" group. Run setup_wv_taxes.py first if the tax is not yet
bound.

Idempotent: re-running finds existing records by natural key (journal code /
name + company) and only fills in what is missing.

Usage:
    ODOO_PASSWORD=... python3 setup_pos.py
"""

from __future__ import annotations

import os
import sys
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "Goldberry")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

POS_COMPANY_NAME = "Goldberry Grove Farm"

# (journal code, journal name, journal type)
JOURNAL_SPECS = [
    ("CSH1", "Cash", "cash"),
    ("CARD", "Card", "bank"),
    ("CHCK", "Check", "bank"),
]

# (payment method label, journal code)
PAYMENT_METHOD_SPECS = [
    ("Cash", "CSH1"),
    ("Card", "CARD"),
    ("Check", "CHCK"),
]

# (pos.config name, crm.team name)
CONFIG_SPECS = [
    ("Farmer's Market", "Farmer's Market"),
    ("Nursery Counter", "Direct to Nursery"),
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


def find_one(models, uid, model, domain):
    ids = call(models, uid, model, "search", [domain], {"limit": 1})
    return ids[0] if ids else None


def ensure_journal(models, uid, company_id, code, name, jtype):
    domain = [("code", "=", code), ("company_id", "=", company_id)]
    existing = find_one(models, uid, "account.journal", domain)
    if existing:
        print(f"  SKIP journal {code} — exists (id={existing})")
        return existing
    vals = {"name": name, "code": code, "type": jtype, "company_id": company_id}
    new_id = call(models, uid, "account.journal", "create", [vals])
    print(f"  CREATE journal {code} → account.journal id={new_id}")
    return new_id


def ensure_team(models, uid, company_id, name):
    domain = [("name", "=", name), ("company_id", "=", company_id)]
    existing = find_one(models, uid, "crm.team", domain)
    if existing:
        print(f"  SKIP team {name!r} — exists (id={existing})")
        return existing
    new_id = call(models, uid, "crm.team", "create", [{"name": name, "company_id": company_id}])
    print(f"  CREATE team {name!r} → crm.team id={new_id}")
    return new_id


def ensure_payment_method(models, uid, company_id, label, journal_id):
    domain = [("name", "=", label), ("company_id", "=", company_id)]
    existing = find_one(models, uid, "pos.payment.method", domain)
    if existing:
        call(models, uid, "pos.payment.method", "write", [[existing], {"journal_id": journal_id}])
        print(f"  SKIP payment method {label!r} — exists (id={existing}), journal synced")
        return existing
    vals = {"name": label, "company_id": company_id, "journal_id": journal_id}
    new_id = call(models, uid, "pos.payment.method", "create", [vals])
    print(f"  CREATE payment method {label!r} → pos.payment.method id={new_id}")
    return new_id


def ensure_config(models, uid, company_id, name, method_ids, team_id):
    domain = [("name", "=", name), ("company_id", "=", company_id)]
    existing = find_one(models, uid, "pos.config", domain)
    vals = {"payment_method_ids": [(6, 0, method_ids)], "crm_team_id": team_id}
    if existing:
        call(models, uid, "pos.config", "write", [[existing], vals])
        print(f"  SKIP pos.config {name!r} — exists (id={existing}), methods + team synced")
        return existing
    new_id = call(models, uid, "pos.config", "create", [{"name": name, "company_id": company_id, **vals}])
    print(f"  CREATE pos.config {name!r} → pos.config id={new_id}")
    return new_id


def main():
    models, uid = authenticate()
    company_id = lookup_id(models, uid, "res.company", [("name", "=", POS_COMPANY_NAME)], POS_COMPANY_NAME)
    print(f"Target company_id={company_id} ({POS_COMPANY_NAME})\n")

    print("Payment journals:")
    journals = {code: ensure_journal(models, uid, company_id, code, name, jtype) for code, name, jtype in JOURNAL_SPECS}

    print("\nPOS payment methods:")
    method_ids = [
        ensure_payment_method(models, uid, company_id, label, journals[code])
        for label, code in PAYMENT_METHOD_SPECS
    ]

    print("\nPOS configs (channels):")
    for config_name, team_name in CONFIG_SPECS:
        team_id = ensure_team(models, uid, company_id, team_name)
        ensure_config(models, uid, company_id, config_name, method_ids, team_id)

    print("\nDone.")


if __name__ == "__main__":
    main()
