#!/usr/bin/env python3
"""One-shot, idempotent farm books bootstrap (Schedule F, phase 1) via XML-RPC.

Companies are looked up by name (never by id):
  * Goldberry Grove Farm   - owns the shared bank (grove_owns_shared_bank)
  * At The Grove Nursery   - gets its own journals
  (George George George Woodworking is never touched.)

Order of operations:
  1. authenticate, resolve the two companies
  2. --reset-test-entries: list posted moves; with --reset-ref-pattern, draft + delete matches
  3. farm grove_owns_shared_bank = True
  4. grove_schedule_f_setup() on farm, then nursery (accounts, tags, MIRR journal)
  5. journals per company (idempotent by code + company)
  6. product-category income/expense defaults (ir.default) and per-category income account
  7. repoint enabled nursery Stripe payment providers to the nursery STRP journal
  8. "Grove Agents" user (no password; mint the API key in the UI)
  9. grove_schedule_f.review_threshold is left alone
 10. print the verification table

Every write goes through write_or_plan(); --dry-run prints the plan and only reads.

Usage:
    ODOO_URL=... ODOO_DB=... ODOO_USER=... ODOO_PASSWORD=... \\
        python3 setup_books.py [--dry-run] \\
        [--reset-test-entries --reset-ref-pattern REGEX]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "Goldberry")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

COMPANY_NAMES = {"farm": "Goldberry Grove Farm", "nursery": "At The Grove Nursery"}

AGENT_LOGIN = "agents@goldberrygrove.farm"
AGENT_NAME = "Grove Agents"
AGENT_GROUP_MODULE, AGENT_GROUP_NAME = "grove_schedule_f", "group_grove_agent"

DEFAULT_INCOME_CODE = "4020"
DEFAULT_EXPENSE_CODE = "6320"
MERCH_INCOME_CODE = "4010"
PLANT_WORDS = ("seed", "plant", "tree")
MERCH_WORDS = ("merch", "shirt", "sticker", "pot")

_JOURNAL_SPECS = {
    "farm": [
        {
            "code": "BNK1",
            "name": "Shared bank (2026)",
            "type": "bank",
            "grove_enterprise_required": True,
            "existing": True,
        },
        {
            "code": "BILL",
            "name": "Vendor Bills",
            "type": "purchase",
            "grove_enterprise_required": True,
            "existing": True,
            "keep_name": True,
        },
        {"code": "STRP", "name": "Stripe (farm)", "type": "bank"},
        {
            "code": "OWNR",
            "name": "Owner paid",
            "type": "bank",
            "default_account_code": "2900",
            "grove_enterprise_required": True,
        },
    ],
    "nursery": [
        {"code": "INV", "name": "Customer Invoices", "type": "sale"},
        {"code": "BILL", "name": "Vendor Bills", "type": "purchase"},
        {"code": "MISC", "name": "Miscellaneous Operations", "type": "general"},
        {"code": "STRP", "name": "Stripe (nursery)", "type": "bank"},
        {"code": "SQR", "name": "Square clearing", "type": "bank"},
        {"code": "VENM", "name": "Venmo clearing", "type": "cash"},
        {"code": "CSH", "name": "Cash", "type": "cash"},
    ],
}

MAX_RESET_MATCHES = 10
READ_ONLY_HINT = "(dry-run: no writes)"


# ---------------------------------------------------------------- pure logic
def journal_specs(company_key: str) -> list[dict]:
    """Journals this script owns for a company ("farm" or "nursery"). MIRR comes from setup."""
    return [dict(j) for j in _JOURNAL_SPECS[company_key]]


def category_income_account(category_name: str) -> str:
    """Income account code for a product category (case-insensitive substring rules)."""
    name = (category_name or "").lower()
    if any(w in name for w in PLANT_WORDS):
        return DEFAULT_INCOME_CODE
    if any(w in name for w in MERCH_WORDS):
        return MERCH_INCOME_CODE
    return DEFAULT_INCOME_CODE


def _m2o_id(value):
    if isinstance(value, (list, tuple)):
        return value[0] if value else False
    return value


def provider_repoint_plan(providers: list[dict], nursery_journals: dict, nursery_company_id=None) -> list[dict]:
    """Updates pointing enabled Stripe providers of the nursery at the nursery STRP journal.

    nursery_journals maps journal code -> id; the nursery company id comes from the
    nursery_company_id argument or a "company_id" key in nursery_journals.
    """
    target = nursery_journals.get("STRP")
    company_id = nursery_company_id if nursery_company_id is not None else nursery_journals.get("company_id")
    if not target:
        return []
    plan = []
    for p in providers:
        if p.get("code") != "stripe" or p.get("state") != "enabled":
            continue
        if company_id is not None and _m2o_id(p.get("company_id")) != company_id:
            continue
        if _m2o_id(p.get("journal_id")) == target:
            continue
        plan.append({"provider_id": p["id"], "journal_id": target})
    return plan


# ------------------------------------------------------------------ plumbing
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


# --------------------------------------------------------------------- steps
def resolve_companies(models, uid) -> dict:
    out = {}
    for key, name in COMPANY_NAMES.items():
        cid = find_one(models, uid, "res.company", [("name", "=", name)])
        if not cid:
            fail(f"Company not found: {name}")
        out[key] = cid
        print(f"Company {key}: {name} (id={cid})")
    return out


def reset_test_entries(models, uid, args, companies):
    print("\n== Posted moves in farm and nursery ==")
    moves = call(
        models,
        uid,
        "account.move",
        "search_read",
        [[("state", "=", "posted"), ("company_id", "in", list(companies.values()))]],
        {"fields": ["name", "ref", "company_id"], "context": {"allowed_company_ids": list(companies.values())}},
    )
    pattern = re.compile(args.reset_ref_pattern, re.I) if args.reset_ref_pattern else None

    def is_match(m):
        return bool(pattern and (pattern.search(m.get("name") or "") or pattern.search(m.get("ref") or "")))

    for m in moves:
        print(f"  posted move id={m['id']} name={m.get('name')!r} ref={m.get('ref')!r} company={m['company_id']}")
    if not args.reset_test_entries:
        return
    pat = (args.reset_ref_pattern or "").strip()
    count = sum(1 for m in moves if is_match(m))
    print(f"  {count} posted move(s) match --reset-ref-pattern {pat!r}")
    if pat in ("", ".*", ".+") or count > MAX_RESET_MATCHES:
        fail(f"--reset-ref-pattern {pat!r} is too broad (matches {count}; max {MAX_RESET_MATCHES}); aborting")
    for m in moves:
        if not is_match(m):
            continue
        c = ctx(_m2o_id(m["company_id"]))
        print(f"  RESET move id={m['id']}: button_draft then unlink")
        try:
            write_or_plan(
                models,
                uid,
                args.dry_run,
                "account.move",
                "button_draft",
                [[m["id"]]],
                {"context": c},
                f"button_draft move {m['id']}",
            )
            write_or_plan(
                models,
                uid,
                args.dry_run,
                "account.move",
                "unlink",
                [[m["id"]]],
                {"context": {**c, "force_delete": True}},
                f"unlink move {m['id']}",
            )
        except xmlrpc.client.Fault as exc:
            print(f"  FAILED reset of move id={m['id']}: {exc.faultString[:200]!r}", file=sys.stderr)


def ensure_shared_bank_flag(models, uid, args, farm_id):
    rows = call(models, uid, "res.company", "read", [[farm_id], ["grove_owns_shared_bank"]])
    if rows and rows[0].get("grove_owns_shared_bank"):
        print("  SKIP grove_owns_shared_bank already True on farm")
        return
    print("  UPDATE farm grove_owns_shared_bank = True")
    write_or_plan(
        models,
        uid,
        args.dry_run,
        "res.company",
        "write",
        [[farm_id], {"grove_owns_shared_bank": True}],
        label="farm shared bank flag",
    )


def run_schedule_f_setup(models, uid, args, companies):
    for key in ("farm", "nursery"):
        cid = companies[key]
        print(f"  grove_schedule_f_setup on {key} (id={cid})")
        result = write_or_plan(
            models,
            uid,
            args.dry_run,
            "res.company",
            "grove_schedule_f_setup",
            [[cid]],
            {"context": ctx(cid)},
            f"grove_schedule_f_setup({key})",
        )
        if result is not None:
            print(f"    result: {result}")


def ensure_journals(models, uid, args, key, company_id):
    out = {}
    for spec in journal_specs(key):
        code = spec["code"]
        domain = [("code", "=", code), ("company_id", "=", company_id)]
        ids = call(models, uid, "account.journal", "search", [domain], {"limit": 1, "context": {"active_test": False}})
        flags = {k: v for k, v in spec.items() if k.startswith("grove_")}
        if ids:
            jid = ids[0]
            cur = call(models, uid, "account.journal", "read", [[jid], ["name", *flags]])
            cur = cur[0] if cur else {}
            vals = {}
            if spec.get("existing") and not spec.get("keep_name"):
                if cur.get("name") != spec["name"]:
                    vals["name"] = spec["name"]
            vals.update({k: v for k, v in flags.items() if cur.get(k) != v})
            if vals:
                print(f"  UPDATE journal {code} (id={jid}) {vals}")
                write_or_plan(
                    models, uid, args.dry_run, "account.journal", "write", [[jid], vals], label=f"journal {code} update"
                )
            else:
                print(f"  SKIP journal {code} - exists (id={jid})")
            out[code] = jid
            continue
        if spec.get("existing"):
            if not args.dry_run:
                fail(f"Expected existing journal {code} not found for company {company_id}; not creating a duplicate")
            print(f"  WARN expected existing journal {code} not found for company {company_id} (real run would abort)")
            continue
        vals = {"name": spec["name"], "code": code, "type": spec["type"], "company_id": company_id, **flags}
        acct_code = spec.get("default_account_code")
        if acct_code:
            acct = account_by_code(models, uid, company_id, acct_code)
            if acct:
                vals["default_account_id"] = acct
            elif args.dry_run:
                print(f"  NOTE account {acct_code} not present yet (created by setup); would be linked on a real run")
            else:
                fail(f"Account {acct_code} missing for company {company_id} after grove_schedule_f_setup")
        print(f"  CREATE journal {code} {vals}")
        new_id = write_or_plan(
            models,
            uid,
            args.dry_run,
            "account.journal",
            "create",
            [vals],
            {"context": ctx(company_id)},
            f"journal {code} create",
        )
        if new_id:
            out[code] = new_id
    return out


def set_category_defaults(models, uid, args, key, company_id):
    income = account_by_code(models, uid, company_id, DEFAULT_INCOME_CODE)
    expense = account_by_code(models, uid, company_id, DEFAULT_EXPENSE_CODE)
    for field, acct, code in (
        ("property_account_income_categ_id", income, DEFAULT_INCOME_CODE),
        ("property_account_expense_categ_id", expense, DEFAULT_EXPENSE_CODE),
    ):
        if not acct:
            print(f"  NOTE account {code} missing for {key}; skip ir.default {field}")
            continue
        existing = call(
            models,
            uid,
            "ir.default",
            "search_read",
            [
                [
                    ("field_id.model", "=", "product.category"),
                    ("field_id.name", "=", field),
                    ("company_id", "=", company_id),
                ]
            ],
            {"fields": ["json_value"], "limit": 1},
        )
        if existing and existing[0].get("json_value") == str(acct):
            print(f"  SKIP ir.default {field} -> {code} ({key})")
            continue
        print(f"  SET ir.default {field} -> {code} ({key})")
        write_or_plan(
            models,
            uid,
            args.dry_run,
            "ir.default",
            "set",
            ["product.category", field, acct],
            {"company_id": company_id},
            f"ir.default {field} {key}",
        )
    cats = call(models, uid, "product.category", "search_read", [[]], {"fields": ["complete_name"]})
    for cat in cats:
        code = category_income_account(cat.get("complete_name") or "")
        acct = account_by_code(models, uid, company_id, code)
        if not acct:
            print(f"  NOTE account {code} missing for {key}; skip category {cat['id']}")
            continue
        cur = call(
            models,
            uid,
            "product.category",
            "read",
            [[cat["id"]], ["property_account_income_categ_id"]],
            {"context": ctx(company_id)},
        )
        if cur and _m2o_id(cur[0].get("property_account_income_categ_id")) == acct:
            print(f"  SKIP category {cat['complete_name']!r} income {code} ({key})")
            continue
        print(f"  UPDATE category {cat['complete_name']!r} income -> {code} ({key})")
        write_or_plan(
            models,
            uid,
            args.dry_run,
            "product.category",
            "write",
            [[cat["id"]], {"property_account_income_categ_id": acct}],
            {"context": ctx(company_id)},
            f"category {cat['id']} income {code}",
        )


def repoint_providers(models, uid, args, nursery_id, nursery_journals):
    providers = call(
        models,
        uid,
        "payment.provider",
        "search_read",
        [[("code", "=", "stripe")]],
        {"fields": ["name", "code", "state", "company_id", "journal_id"], "context": {"active_test": False}},
    )
    if "STRP" not in nursery_journals and args.dry_run:
        print("  NOTE nursery STRP journal does not exist yet; provider repoint follows its creation")
        return
    for upd in provider_repoint_plan(providers, nursery_journals, nursery_company_id=nursery_id):
        print(f"  REPOINT provider {upd['provider_id']} -> journal {upd['journal_id']}")
        write_or_plan(
            models,
            uid,
            args.dry_run,
            "payment.provider",
            "write",
            [[upd["provider_id"]], {"journal_id": upd["journal_id"]}],
            label=f"provider {upd['provider_id']} journal",
        )


def ensure_agent_user(models, uid, args, companies):
    gids = call(
        models,
        uid,
        "ir.model.data",
        "search_read",
        [[("module", "=", AGENT_GROUP_MODULE), ("name", "=", AGENT_GROUP_NAME), ("model", "=", "res.groups")]],
        {"fields": ["res_id"], "limit": 1},
    )
    if not gids:
        if args.dry_run:
            print("  NOTE agent group not installed yet; cannot plan user precisely")
            return
        fail(f"Group {AGENT_GROUP_MODULE}.{AGENT_GROUP_NAME} not found (module installed?)")
    gid = gids[0]["res_id"]
    cids = [companies["farm"], companies["nursery"]]
    existing = call(
        models,
        uid,
        "res.users",
        "search_read",
        [[("login", "=", AGENT_LOGIN)]],
        {"fields": ["group_ids", "company_ids", "company_id"], "limit": 1, "context": {"active_test": False}},
    )
    if existing:
        cur = existing[0]
        vals = {}
        if sorted(cur.get("group_ids", [])) != [gid]:
            vals["group_ids"] = [(6, 0, [gid])]
        if sorted(cur.get("company_ids", [])) != sorted(cids):
            vals["company_ids"] = [(6, 0, cids)]
        if vals:
            print(f"  UPDATE user {AGENT_LOGIN} (id={cur['id']})")
            write_or_plan(
                models, uid, args.dry_run, "res.users", "write", [[cur["id"]], vals], label="agent user update"
            )
        else:
            print(f"  SKIP user {AGENT_LOGIN} - exists (id={cur['id']})")
        return
    vals = {
        "login": AGENT_LOGIN,
        "name": AGENT_NAME,
        "group_ids": [(6, 0, [gid])],
        "company_ids": [(6, 0, cids)],
        "company_id": companies["farm"],
    }
    print(f"  CREATE user {AGENT_LOGIN} (no password; mint an API key in the UI)")
    write_or_plan(models, uid, args.dry_run, "res.users", "create", [vals], label="agent user create")


def print_verification(models, uid, companies):
    print("\n== Verification ==")
    print(f"{'company':<8} {'code':<6} {'name':<28} {'type':<9} {'acct':<8} flags")
    for key, cid in companies.items():
        rows = call(
            models,
            uid,
            "account.journal",
            "search_read",
            [[("company_id", "=", cid)]],
            {
                "fields": [
                    "code",
                    "name",
                    "type",
                    "default_account_id",
                    "grove_enterprise_required",
                    "grove_is_mirror",
                ],
                "order": "code",
                "context": ctx(cid),
            },
        )
        acct_ids = [_m2o_id(r["default_account_id"]) for r in rows if r.get("default_account_id")]
        codes = {}
        if acct_ids:
            for a in call(models, uid, "account.account", "read", [acct_ids, ["code"]], {"context": ctx(cid)}):
                codes[a["id"]] = a.get("code")
        for r in rows:
            acct = codes.get(_m2o_id(r.get("default_account_id")), "-")
            flags = [f for f in ("grove_enterprise_required", "grove_is_mirror") if r.get(f)]
            print(f"{key:<8} {r['code']:<6} {r['name']:<28} {r['type']:<9} {acct or '-':<8} {','.join(flags)}")


# ---------------------------------------------------------------------- main
def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true", help="print the plan; perform no writes")
    p.add_argument("--reset-test-entries", action="store_true", help="draft + delete posted moves matching the pattern")
    p.add_argument("--reset-ref-pattern", default=None, help="regex matched against move name or ref")
    return p.parse_args(argv)


def run(models, uid, args):
    if args.reset_test_entries and not args.reset_ref_pattern:
        fail("--reset-test-entries requires --reset-ref-pattern")
    dry = args.dry_run
    if dry:
        print(f"*** DRY RUN {READ_ONLY_HINT} ***")
    companies = resolve_companies(models, uid)
    reset_test_entries(models, uid, args, companies)
    print("\n== Shared bank flag ==")
    ensure_shared_bank_flag(models, uid, args, companies["farm"])
    print("\n== grove_schedule_f_setup ==")
    run_schedule_f_setup(models, uid, args, companies)
    journals = {}
    for key in ("farm", "nursery"):
        print(f"\n== Journals ({key}) ==")
        journals[key] = ensure_journals(models, uid, args, key, companies[key])
    for key in ("farm", "nursery"):
        print(f"\n== Category defaults ({key}) ==")
        set_category_defaults(models, uid, args, key, companies[key])
    print("\n== Payment providers ==")
    repoint_providers(models, uid, args, companies["nursery"], journals["nursery"])
    print("\n== Grove Agents user ==")
    ensure_agent_user(models, uid, args, companies)
    print("\n(review_threshold left unchanged)")
    print_verification(models, uid, companies)


def main():
    args = parse_args()
    models, uid = authenticate()
    run(models, uid, args)


if __name__ == "__main__":
    main()
