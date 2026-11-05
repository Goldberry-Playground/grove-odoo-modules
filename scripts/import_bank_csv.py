#!/usr/bin/env python3
"""Backfill a bank CSV into Odoo month by month via XML-RPC (OCA sheet importer).

Each month of the file is imported as its own statement through the wizard
`account.statement.import` with the mapping "Shared bank CSV" (created or reused by name).
A synthetic `unique_import_id` column is appended to every row and mapped to the
mapping's transaction id, so re-importing the same file creates no duplicate lines
(the OCA importer skips lines whose unique_import_id already exists on the journal).

The id is the bank's own id column when `--id-col` is given and the row has a value,
otherwise sha1(date|amount|description|row index) with the row's 0-based position in
the original file, so the same file always yields the same ids.

Every write goes through write_or_plan(); --dry-run prints the plan and only reads.

Usage:
    ODOO_URL=... ODOO_DB=... ODOO_USER=... ODOO_PASSWORD=... \\
        python3 import_bank_csv.py bank.csv --date-col Date --amount-col Amount --desc-col Description \\
        [--id-col "Transaction ID"] [--partner-col Payee] [--date-fmt %m/%d/%Y] \\
        [--debit-col Debit --credit-col Credit] [--from 2026-01 --to 2026-10] \\
        [--journal-code BNK1] [--company "Goldberry Grove Farm"] [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import os
import sys
import tempfile
import xmlrpc.client
from datetime import datetime

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "Goldberry")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

MAPPING_NAME = "Shared bank CSV"
ID_COLUMN = "unique_import_id"


# ------------------------------------------------------------------- pure
def _as_cols(cols) -> list[str]:
    if cols is None:
        return []
    return [cols] if isinstance(cols, str) else [c for c in cols if c]


def unique_import_id(row: dict, index: int, id_col: str | None, date_col=None, amount_col=None, desc_col=None) -> str:
    """The bank's id when present, else sha1(date|amount|description|index).

    amount_col may be one column or several (debit/credit). With no column names the
    row's values stand in for date/amount/description.
    """
    if id_col:
        bank_id = (row.get(id_col) or "").strip()
        if bank_id:
            return bank_id
    if date_col or amount_col or desc_col:
        date = (row.get(date_col) or "") if date_col else ""
        amount = "/".join(row.get(c) or "" for c in _as_cols(amount_col))
        desc = (row.get(desc_col) or "") if desc_col else ""
        parts = [date, amount, desc, str(index)]
    else:
        parts = ["/".join(str(v) for v in row.values()), "", "", str(index)]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def month_key(row: dict, date_col: str, date_fmt: str) -> str:
    return datetime.strptime(row[date_col].strip(), date_fmt).strftime("%Y-%m")


def split_by_month(rows: list[dict], date_col: str, date_fmt: str) -> dict[str, list[dict]]:
    """Group rows by YYYY-MM, preserving file order inside each month."""
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(month_key(row, date_col, date_fmt), []).append(row)
    return out


def select_months(months: list[str], start: str | None, end: str | None) -> list[str]:
    """Inclusive YYYY-MM bounds; YYYY-MM strings compare lexically."""
    return [m for m in months if (not start or m >= start) and (not end or m <= end)]


def mapping_vals(args) -> dict:
    """account.statement.import.sheet.mapping vals from the CLI column names."""
    single = bool(args.amount_col)
    split = bool(args.debit_col and args.credit_col)
    if single == split:
        raise ValueError("give either --amount-col or both --debit-col and --credit-col")
    vals = {
        "name": MAPPING_NAME,
        "float_thousands_sep": "comma",
        "float_decimal_sep": "dot",
        "file_encoding": "utf-8",
        "delimiter": "comma",
        "quotechar": '"',
        "timestamp_format": args.date_fmt,
        "timestamp_column": args.date_col,
        "description_column": args.desc_col,
        "transaction_id_column": ID_COLUMN,
    }
    if single:
        vals["amount_type"] = "simple_value"
        vals["amount_column"] = args.amount_col
    else:
        vals["amount_type"] = "distinct_credit_debit"
        vals["amount_debit_column"] = args.debit_col
        vals["amount_credit_column"] = args.credit_col
    if args.partner_col:
        vals["partner_name_column"] = args.partner_col
    return vals


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


# ------------------------------------------------------------------ steps
def ensure_mapping(models, uid, args):
    vals = mapping_vals(args)
    existing = find_one(models, uid, "account.statement.import.sheet.mapping", [("name", "=", MAPPING_NAME)])
    if existing:
        print(f"  SKIP mapping '{MAPPING_NAME}' - exists (id={existing})")
        return existing
    print(f"  CREATE mapping '{MAPPING_NAME}'")
    return write_or_plan(
        models, uid, args.dry_run, "account.statement.import.sheet.mapping", "create", [vals], label="mapping create"
    )


def read_rows(path: str) -> tuple[list[str], list[dict]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def month_file(fieldnames: list[str], rows: list[dict], directory: str, month: str) -> str:
    path = os.path.join(directory, f"bank-{month}.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames + [ID_COLUMN], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def count_lines(models, uid, company_id, journal_id, ids):
    if not ids:
        return 0
    return call(
        models,
        uid,
        "account.bank.statement.line",
        "search_count",
        [[("journal_id", "=", journal_id), ("unique_import_id", "in", ids)]],
        {"context": {"allowed_company_ids": [company_id]}},
    )


def run(models, uid, args):
    if args.dry_run:
        print("*** DRY RUN (dry-run: no writes) ***")
    company_id = find_one(models, uid, "res.company", [("name", "=", args.company)])
    if not company_id:
        fail(f"company '{args.company}' not found")
    context = {"allowed_company_ids": [company_id]}
    journal_id = find_one(
        models,
        uid,
        "account.journal",
        [("code", "=", args.journal_code), ("company_id", "=", company_id)],
        context=context,
    )
    if not journal_id:
        fail(f"journal {args.journal_code} not found in {args.company}")

    fieldnames, rows = read_rows(args.csv_file)
    amount_cols = [args.amount_col] if args.amount_col else [args.debit_col, args.credit_col]
    for i, row in enumerate(rows):
        row[ID_COLUMN] = unique_import_id(row, i, args.id_col, args.date_col, amount_cols, args.desc_col)
    by_month = split_by_month(rows, args.date_col, args.date_fmt)
    months = select_months(sorted(by_month), args.from_month, args.to_month)
    print(f"{len(rows)} rows, {len(by_month)} months in file; importing {len(months)}")

    print("\n== Mapping ==")
    mapping_id = ensure_mapping(models, uid, args)

    print("\n== Import ==")
    import_ctx = {
        "allowed_company_ids": [company_id],
        "journal_id": journal_id,
        "active_model": "account.journal",
        "active_id": journal_id,
    }
    with tempfile.TemporaryDirectory(prefix="bank-csv-") as tmp:
        for month in months:
            month_rows = by_month[month]
            ids = [r[ID_COLUMN] for r in month_rows]
            if args.dry_run:
                print(f"  PLAN {month}: {len(month_rows)} rows")
                continue
            before = count_lines(models, uid, company_id, journal_id, ids)
            path = month_file(fieldnames, month_rows, tmp, month)
            with open(path, "rb") as fh:
                payload = base64.b64encode(fh.read()).decode("ascii")
            wizard = call(
                models,
                uid,
                "account.statement.import",
                "create",
                [
                    {
                        "statement_file": payload,
                        "statement_filename": os.path.basename(path),
                        "sheet_mapping_id": mapping_id,
                    }
                ],
                {"context": import_ctx},
            )
            try:
                call(models, uid, "account.statement.import", "import_file_button", [[wizard]], {"context": import_ctx})
            except xmlrpc.client.Fault as exc:
                # OCA refuses a file whose transactions are all already imported: nothing new, not a failure.
                if "already imported" not in str(exc.faultString):
                    raise
            after = count_lines(models, uid, company_id, journal_id, ids)
            print(f"{month}: {after} lines ({after - before} new)")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("csv_file")
    p.add_argument("--date-col", required=True)
    p.add_argument("--amount-col")
    p.add_argument("--debit-col")
    p.add_argument("--credit-col")
    p.add_argument("--desc-col", required=True)
    p.add_argument("--id-col")
    p.add_argument("--partner-col")
    p.add_argument("--date-fmt", default="%m/%d/%Y")
    p.add_argument("--from", dest="from_month", metavar="YYYY-MM")
    p.add_argument("--to", dest="to_month", metavar="YYYY-MM")
    p.add_argument("--journal-code", default="BNK1")
    p.add_argument("--company", default="Goldberry Grove Farm")
    p.add_argument("--dry-run", action="store_true", help="print the plan; perform no writes")
    return p.parse_args(argv)


def main():
    args = parse_args()
    try:
        mapping_vals(args)
    except ValueError as exc:
        fail(str(exc))
    models, uid = authenticate()
    run(models, uid, args)


if __name__ == "__main__":
    main()
