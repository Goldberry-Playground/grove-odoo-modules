#!/usr/bin/env python3
"""Book Square-era monthly income summaries into the nursery via XML-RPC.

Input CSV columns: month,gross,fees (dollars; month is YYYY-MM). Each row becomes one posted
entry in the nursery's SQR (Square clearing) journal dated the last day of the month:
    Dr SQR default account  gross - fees
    Dr 6321                 fees
    Cr 4020                 gross
Idempotent by ref "SQUARE YYYY-MM" in the nursery company. --dry-run prints the plan only.

Usage:
    ODOO_URL=... ODOO_DB=... ODOO_USER=... ODOO_PASSWORD=... \\
        python3 import_square_summary.py square_months.csv [--dry-run]
"""

from __future__ import annotations

import argparse
import calendar
import csv
import math
import os
import sys
import xmlrpc.client
from datetime import datetime

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "Goldberry")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

COMPANY_NAMES = {"farm": "Goldberry Grove Farm", "nursery": "At The Grove Nursery"}


# ------------------------------------------------------------------- pure
def _money(text) -> float:
    return float(str(text).replace("$", "").replace(",", "").strip())


def parse_rows(lines) -> list[tuple[str, float, float]]:
    """Parse month,gross,fees rows (header required) into (month, gross, fees); month comes back as YYYY-MM."""
    out = []
    for n, r in enumerate(csv.DictReader(lines), start=2):
        try:
            # canonical YYYY-MM ("2026-1" -> "2026-01") so the "SQUARE YYYY-MM" ref stays stable across runs
            month = datetime.strptime((r["month"] or "").strip(), "%Y-%m").strftime("%Y-%m")
            gross, fees = _money(r["gross"]), _money(r["fees"])
            if not (math.isfinite(gross) and math.isfinite(fees)):
                raise ValueError("non-finite amount")
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError(f"row {n}: expected month YYYY-MM and finite numeric gross/fees, got {dict(r)}") from None
        if gross < 0 or fees < 0 or fees > gross:
            raise ValueError(f"row {n} ({month}): need 0 <= fees <= gross, got gross={gross} fees={fees}")
        out.append((month, gross, fees))
    return out


def summary_entry(month: str, gross: float, fees: float, journal_default_account_code_lookup=None) -> dict:
    """Nursery account.move vals for one month, lines expressed with account codes.

    "SQR" as a line account_code means the SQR journal's default account; pass a lookup
    callable (journal code -> account code) to substitute a concrete code instead.
    """
    if fees > gross:
        raise ValueError(f"{month}: fees {fees} exceed gross {gross}")
    year, mon = (int(part) for part in month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    sqr = journal_default_account_code_lookup("SQR") if journal_default_account_code_lookup else "SQR"
    net = round(gross - fees, 2)
    return {
        "company": "nursery",
        "journal": "SQR",
        "move_type": "entry",
        "date": f"{year:04d}-{mon:02d}-{last_day:02d}",
        "ref": f"SQUARE {month}",
        "lines": [
            {"account_code": sqr, "debit": net, "credit": 0.0, "name": f"Square net deposits {month}"},
            {"account_code": "6321", "debit": round(fees, 2), "credit": 0.0, "name": f"Square fees {month}"},
            {"account_code": "4020", "debit": 0.0, "credit": round(gross, 2), "name": f"Square gross sales {month}"},
        ],
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


def ensure_posted(models, uid, args, company_id, move_id, label):
    """Post a move found in draft (e.g. a previous run died before action_post)."""
    state = call(models, uid, "account.move", "read", [[move_id], ["state"]], {"context": ctx(company_id)})[0]["state"]
    if state == "draft":
        print(f"  POST draft {label}")
        write_or_plan(
            models, uid, args.dry_run, "account.move", "action_post", [[move_id]], {"context": ctx(company_id)}
        )
    else:
        print(f"  SKIP {label} - already {state}")


# ------------------------------------------------------------------ steps
def book_month(models, uid, args, company_id, month, gross, fees):
    entry = summary_entry(month, gross, fees)
    context = ctx(company_id)
    existing = find_one(
        models, uid, "account.move", [("ref", "=", entry["ref"]), ("company_id", "=", company_id)], context=context
    )
    if existing:
        ensure_posted(models, uid, args, company_id, existing, entry["ref"])
        return
    journal_id = find_one(
        models, uid, "account.journal", [("code", "=", "SQR"), ("company_id", "=", company_id)], context=context
    )
    if not journal_id:
        fail("nursery journal SQR not found (run setup_books.py first)")
    journal = call(
        models, uid, "account.journal", "read", [[journal_id], ["default_account_id"]], {"context": context}
    )[0]
    sqr_account = journal["default_account_id"][0] if journal.get("default_account_id") else None
    if not sqr_account:
        fail("SQR journal has no default account")
    line_cmds = []
    for line in entry["lines"]:
        account = (
            sqr_account
            if line["account_code"] == "SQR"
            else account_by_code(models, uid, company_id, line["account_code"])
        )
        if not account:
            fail(f"account {line['account_code']} not found in nursery")
        line_cmds.append(
            (0, 0, {"account_id": account, "debit": line["debit"], "credit": line["credit"], "name": line["name"]})
        )
    vals = {
        "move_type": "entry",
        "journal_id": journal_id,
        "date": entry["date"],
        "ref": entry["ref"],
        "company_id": company_id,
        "line_ids": line_cmds,
    }
    print(f"  CREATE {entry['ref']} gross={gross} fees={fees}")
    move_id = write_or_plan(
        models, uid, args.dry_run, "account.move", "create", [vals], {"context": context}, label=entry["ref"]
    )
    if move_id:
        write_or_plan(models, uid, args.dry_run, "account.move", "action_post", [[move_id]], {"context": context})


def run(models, uid, args):
    if args.dry_run:
        print("*** DRY RUN (dry-run: no writes) ***")
    company_id = company_id_by_name(models, uid, COMPANY_NAMES["nursery"])
    with open(args.csv_file, newline="", encoding="utf-8-sig") as fh:
        try:
            rows = parse_rows(fh)
        except ValueError as exc:
            fail(str(exc))
    print(f"{len(rows)} months")
    for month, gross, fees in rows:
        book_month(models, uid, args, company_id, month, gross, fees)
    print("\nDone.")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("csv_file")
    p.add_argument("--dry-run", action="store_true", help="print the plan; perform no writes")
    return p.parse_args(argv)


def main():
    args = parse_args()
    models, uid = authenticate()
    run(models, uid, args)


if __name__ == "__main__":
    main()
