#!/usr/bin/env python3
"""Seed Goldberry sales teams (channels) into Odoo via XML-RPC.

Each team represents a sales channel for revenue reporting and pipeline tracking.
Idempotent: re-running skips teams that already exist by name + company.

Usage:
    ODOO_PASSWORD=... python3 seed_sales_teams.py
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

TEAMS: list[dict[str, Any]] = [
    {
        "name": "Farmer's Market",
        "sequence": 10,
        "note": "Saturday farmer's market sales — portable POS, cash + card",
    },
    {
        "name": "Direct to Nursery",
        "sequence": 20,
        "note": "Walk-in retail at nursery — full POS terminal, all payment methods",
    },
    {
        "name": "Online",
        "sequence": 30,
        "note": "Website orders via headless storefront (goldberrygrove.farm)",
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


def seed_team(models, uid, company_id, team):
    name = team["name"]
    existing = call(
        models, uid, "crm.team", "search", [[("name", "=", name), ("company_id", "=", company_id)]], {"limit": 1}
    )
    if existing:
        print(f"  SKIP {name} — already exists (id={existing[0]})")
        return

    vals = {
        "name": name,
        "sequence": team["sequence"],
        "company_id": company_id,
    }
    new_id = call(models, uid, "crm.team", "create", [vals])
    print(f"  CREATE {name} → crm.team id={new_id} — {team['note']}")


def main():
    models, uid = authenticate()
    company_id = lookup_id(models, uid, "res.company", [("name", "=", "Goldberry Grove Farm")], "Goldberry Grove Farm")
    print(f"Target company_id={company_id} (Goldberry Grove Farm)\n")
    print(f"Seeding {len(TEAMS)} sales teams:")
    for team in TEAMS:
        seed_team(models, uid, company_id, team)
    print("\nDone.")


if __name__ == "__main__":
    main()
