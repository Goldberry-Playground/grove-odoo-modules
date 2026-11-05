#!/usr/bin/env python3
"""Book the annual farm-to-nursery rent through XML-RPC, settled via intercompany 1350.

Four documents (all posted):
  * farm customer invoice to the nursery's partner, line on 4080 "Other farm income"
    (the CPA may move it to another Schedule F line), ref "RENT {period}"
  * nursery vendor bill from the farm's partner, line on 6245 (line 24b), same ref
  * farm MISC settlement  Dr 1350 / Cr receivable   (ref "RENT {period} settlement")
  * nursery MISC settlement  Dr payable / Cr 1350
Each settlement's receivable/payable line is reconciled with its document's. Dated {period}-12-31.
Idempotent by ref per company. The amount comes from Josh; the script refuses 0.

Usage:
    ODOO_URL=... ODOO_DB=... ODOO_USER=... ODOO_PASSWORD=... \\
        python3 book_rent.py --period 2026 --amount "$RENT_AMOUNT" [--dry-run]
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


# ------------------------------------------------------------------- pure
def rent_documents(period: str, amount: float) -> dict:
    """Pure val dicts for the four rent documents. Codes are resolved to ids by the script.

    "RECEIVABLE" / "PAYABLE" mean the document's receivable / payable line account.
    partner_company names the company whose res.partner is the counterparty.
    """
    if amount <= 0:
        raise ValueError("rent amount must be greater than zero")
    date = f"{period}-12-31"
    ref = f"RENT {period}"
    amount = round(amount, 2)

    def doc_line(code, name):
        return (0, 0, {"account_code": code, "name": name, "quantity": 1, "price_unit": amount, "tax_ids": []})

    def settle_line(code, debit, credit, partner_company=None):
        vals = {"account_code": code, "debit": debit, "credit": credit, "name": f"Rent {period} settlement"}
        if partner_company:
            vals["partner_company"] = partner_company
        return (0, 0, vals)

    return {
        "farm_invoice": {
            "company": "farm",
            "move_type": "out_invoice",
            "partner_company": "nursery",
            "invoice_date": date,
            "date": date,
            "ref": ref,
            "invoice_line_ids": [doc_line("4080", f"Rent {period}")],
        },
        "nursery_bill": {
            "company": "nursery",
            "move_type": "in_invoice",
            "partner_company": "farm",
            "invoice_date": date,
            "date": date,
            "ref": ref,
            "invoice_line_ids": [doc_line("6245", f"Rent {period}")],
        },
        "farm_settlement": {
            "company": "farm",
            "journal": "MISC",
            "move_type": "entry",
            "date": date,
            "ref": f"{ref} settlement",
            "line_ids": [
                settle_line("1350", amount, 0.0),
                settle_line("RECEIVABLE", 0.0, amount, partner_company="nursery"),
            ],
        },
        "nursery_settlement": {
            "company": "nursery",
            "journal": "MISC",
            "move_type": "entry",
            "date": date,
            "ref": f"{ref} settlement",
            "line_ids": [
                settle_line("PAYABLE", amount, 0.0, partner_company="farm"),
                settle_line("1350", 0.0, amount),
            ],
        },
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
def existing_move(models, uid, company_id, ref, move_type):
    return find_one(
        models,
        uid,
        "account.move",
        [("ref", "=", ref), ("company_id", "=", company_id), ("move_type", "=", move_type)],
        context=ctx(company_id),
    )


def resolve_document(models, uid, company_id, doc, partners):
    """Turn a document spec into create vals (account codes -> ids, partner placeholder -> id)."""
    vals = {k: v for k, v in doc.items() if k not in ("company", "partner_company")}
    vals["company_id"] = company_id
    vals["partner_id"] = partners[doc["partner_company"]]
    cmds = []
    for cmd in doc["invoice_line_ids"]:
        line = dict(cmd[2])
        code = line.pop("account_code")
        account = account_by_code(models, uid, company_id, code)
        if not account:
            fail(f"account {code} not found in company {company_id}")
        line["account_id"] = account
        cmds.append((0, 0, line))
    vals["invoice_line_ids"] = cmds
    return vals


def receivable_payable_account(models, uid, company_id, move_id, kind):
    lines = call(
        models,
        uid,
        "account.move.line",
        "search_read",
        [[("move_id", "=", move_id), ("account_id.account_type", "=", kind)]],
        {"fields": ["account_id"], "limit": 1, "context": ctx(company_id)},
    )
    if not lines:
        fail(f"move {move_id} has no {kind} line")
    return lines[0]["account_id"][0]


def resolve_settlement(models, uid, company_id, doc, partners, doc_move_id, kind):
    journal_id = find_one(
        models,
        uid,
        "account.journal",
        [("code", "=", doc["journal"]), ("company_id", "=", company_id)],
        context=ctx(company_id),
    )
    if not journal_id:
        fail(f"journal {doc['journal']} not found in company {company_id}")
    placeholder_account = receivable_payable_account(models, uid, company_id, doc_move_id, kind)
    cmds = []
    for cmd in doc["line_ids"]:
        line = dict(cmd[2])
        code = line.pop("account_code")
        partner_company = line.pop("partner_company", None)
        if code in ("RECEIVABLE", "PAYABLE"):
            line["account_id"] = placeholder_account
        else:
            account = account_by_code(models, uid, company_id, code)
            if not account:
                fail(f"account {code} not found in company {company_id}")
            line["account_id"] = account
        if partner_company:
            line["partner_id"] = partners[partner_company]
        cmds.append((0, 0, line))
    return {
        "move_type": "entry",
        "journal_id": journal_id,
        "date": doc["date"],
        "ref": doc["ref"],
        "company_id": company_id,
        "line_ids": cmds,
    }


def create_and_post(models, uid, args, company_id, vals, label):
    print(f"  CREATE {label}")
    move_id = write_or_plan(
        models, uid, args.dry_run, "account.move", "create", [vals], {"context": ctx(company_id)}, label=label
    )
    if move_id:
        write_or_plan(
            models, uid, args.dry_run, "account.move", "action_post", [[move_id]], {"context": ctx(company_id)}
        )
    return move_id


def reconcile_pair(models, uid, args, company_id, doc_move_id, settle_move_id, kind):
    lines = call(
        models,
        uid,
        "account.move.line",
        "search",
        [
            [
                ("move_id", "in", [doc_move_id, settle_move_id]),
                ("account_id.account_type", "=", kind),
                ("reconciled", "=", False),
            ]
        ],
        {"context": ctx(company_id)},
    )
    if len(lines) < 2:
        print(f"  SKIP reconcile {kind} - already reconciled")
        return
    print(f"  RECONCILE {kind} lines {lines}")
    write_or_plan(
        models,
        uid,
        args.dry_run,
        "account.move.line",
        "reconcile",
        [lines],
        {"context": ctx(company_id)},
        label=f"reconcile {kind}",
    )


def book_side(models, uid, args, company_id, doc, settlement, partners, kind):
    """Ensure one company's document + settlement exist, are posted, and reconciled."""
    doc_id = existing_move(models, uid, company_id, doc["ref"], doc["move_type"])
    if doc_id:
        ensure_posted(models, uid, args, company_id, doc_id, f"{doc['ref']} ({doc['move_type']})")
    else:
        doc_id = create_and_post(
            models,
            uid,
            args,
            company_id,
            resolve_document(models, uid, company_id, doc, partners),
            f"{doc['ref']} ({doc['move_type']})",
        )
    settle_id = existing_move(models, uid, company_id, settlement["ref"], "entry")
    if settle_id:
        ensure_posted(models, uid, args, company_id, settle_id, settlement["ref"])
    elif doc_id:
        vals = resolve_settlement(models, uid, company_id, settlement, partners, doc_id, kind)
        settle_id = create_and_post(models, uid, args, company_id, vals, settlement["ref"])
    if doc_id and settle_id:
        reconcile_pair(models, uid, args, company_id, doc_id, settle_id, kind)


def run(models, uid, args):
    docs = rent_documents(args.period, args.amount)
    if args.dry_run:
        print("*** DRY RUN (dry-run: no writes) ***")
    company_ids = {key: company_id_by_name(models, uid, name) for key, name in COMPANY_NAMES.items()}
    partners = {}
    for key, cid in company_ids.items():
        rows = call(models, uid, "res.company", "read", [[cid], ["partner_id"]])
        partners[key] = rows[0]["partner_id"][0]
    print("\n== Farm: invoice + settlement ==")
    book_side(
        models,
        uid,
        args,
        company_ids["farm"],
        docs["farm_invoice"],
        docs["farm_settlement"],
        partners,
        "asset_receivable",
    )
    print("\n== Nursery: bill + settlement ==")
    book_side(
        models,
        uid,
        args,
        company_ids["nursery"],
        docs["nursery_bill"],
        docs["nursery_settlement"],
        partners,
        "liability_payable",
    )
    print("\nDone.")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--period", required=True, help="year, e.g. 2026")
    p.add_argument("--amount", required=True, type=float, help="annual rent in dollars (> 0)")
    p.add_argument("--dry-run", action="store_true", help="print the plan; perform no writes")
    return p.parse_args(argv)


def main():
    args = parse_args()
    if args.amount <= 0:
        fail("--amount must be greater than zero")
    models, uid = authenticate()
    run(models, uid, args)


if __name__ == "__main__":
    main()
