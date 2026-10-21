#!/usr/bin/env python3
"""One-time legacy Ghost → Farm News importer (GOL-3564, GOL-3454 §9).

Josh demoted Ghost to blog-only (GOL-3454, "option C", 2026-10-19); Odoo Email
Marketing (``mass_mailing``) is now the list of record for every Grove
newsletter. The one exception that migrates is the handful of subscribers still
on the **legacy Ghost droplet** farm blog — §9 ruled they move into the
**Farm News** ``mailing.list`` (company: Goldberry Grove Farm).

This is a deliberate, Josh-gated one-off, **out of the module install** and not
a public route (§9: "a guarded admin action / CLI reading a CSV export … out of
the request path entirely"). Same shape as ``backfill_planner_facts.py``:
XML-RPC, dry-run by default, QA first, prod only on Josh's explicit go.

Input (PII — handle with care)
------------------------------
A 1Password **Document** titled ``GOL-3455 legacy Ghost members 2026-10-21`` in
vault ``Goldberry Grove - Admin``. It is TSV with a header and the columns
``email, name, status, created_at, email_disabled, newsletters``. Josh streamed
it there from the legacy box (GOL-3455 doc ``legacy-ghost-check`` step 2b).

**It is PII.** This script never commits it, never prints an email, and never
echoes a row's contents — output is counts only, rows referenced by 1-based
index. By default it reads the document at run time into a ``0600`` temp file
via ``op document get`` and deletes it in a ``finally`` block. ``--tsv PATH``
reads a local file instead (used by the unit test and by a hand-streamed file).

What it does (§9)
-----------------
- Imports **only** rows subscribed to the "Goldberry Grove" newsletter with
  ``email_disabled=0``. Unsubscribed and bounced rows are skipped.
- Upserts into the ``Farm News`` list (``grove_headless.mailing_list_farm``) as
  a ``mailing.subscription`` with ``opt_out=False``, creating the
  ``mailing.contact`` if missing.
- Tags the imported ``res.partner`` ``source:legacy-ghost-migrated`` so these
  are distinguishable from form opt-ins (same ``res.partner.category`` path the
  live subscribe route uses for ``source:<x>``).
- **No welcome email.** The single-opt-in welcome lives only in the subscribe
  route (``controllers/main.py`` ``_apply_newsletter_subscription``); this
  importer writes the subscription rows directly via XML-RPC and never calls
  that route, so no welcome can fire.
- **Idempotent, prior-opt-out-wins.** An email already active on Farm News is
  left untouched ("existing"); an email opted out of *any* Grove list, or on
  ``mail.blacklist``, is left untouched (never revived by a bulk import).

Usage
-----
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com ODOO_DB=odoo \\
    ODOO_USER=<login> ODOO_PASSWORD=<key> \\
    python3 scripts/import_legacy_ghost_members.py            # dry-run (default)
    #   ... --apply                                            # perform the import

    # read a hand-streamed TSV instead of 1Password:
    #   ... --tsv /path/to/members.tsv

Dry-run prints the full count breakdown (to insert / existing / skipped by
reason) against the live database with **no writes**. ``--apply`` performs the
upsert. Exit codes: 0 ok, 1 auth / setup / input failure.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import subprocess
import sys
import tempfile
import xmlrpc.client

# ── Constants ────────────────────────────────────────────────────────────────
ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

# The legacy farm blog subscribers go to Farm News (§9).
FARM_LIST_XMLID = "grove_headless.mailing_list_farm"
# res.partner.category provenance tag (§9) — same namespaced shape the live
# subscribe route stamps for a capture source.
MIGRATION_TAG = "source:legacy-ghost-migrated"
# res.company that owns Farm News; overridable for odd environments.
FARM_COMPANY = os.getenv("FARM_COMPANY", "Goldberry Grove Farm")

# The Ghost newsletter whose members migrate. A member is "subscribed" when this
# newsletter appears in their ``newsletters`` column.
GOLDBERRY_GROVE_NEWSLETTER = "Goldberry Grove"

# 1Password source document (GOL-3455 step 2b).
OP_DOCUMENT_TITLE = "GOL-3455 legacy Ghost members 2026-10-21"
OP_VAULT = "Goldberry Grove - Admin"

# Required TSV columns (header names, lower-cased).
REQUIRED_COLUMNS = ("email", "email_disabled", "newsletters")


# ── Pure helpers (unit-tested; no Odoo, no I/O) ──────────────────────────────
def normalize_email(raw) -> str:
    """Lower-cased, stripped address — matches Odoo ``email_normalized`` for the
    plain addresses a Ghost export carries (``<name>`` wrappers are unwrapped)."""
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    match = re.search(r"<([^>]+)>", value)
    if match:
        value = match.group(1).strip()
    return value.lower()


def is_subscribed_to_newsletter(newsletters_field, target=GOLDBERRY_GROVE_NEWSLETTER) -> bool:
    """True when ``target`` appears among a member's subscribed newsletters.

    Ghost lists only the newsletters a member is subscribed to, so presence is
    subscription. The column may hold several names joined by ``, ; |``; match
    is case-insensitive and tolerant of a trailing qualifier (e.g.
    ``Goldberry Grove (subscribed)``)."""
    if not isinstance(newsletters_field, str):
        return False
    target_norm = target.strip().lower()
    if not target_norm:
        return False
    for token in re.split(r"[,;|]", newsletters_field):
        token = token.strip().lower()
        if token and (token == target_norm or target_norm in token):
            return True
    return False


def email_delivery_disabled(value) -> bool:
    """True when the Ghost ``email_disabled`` flag marks the address bounced/
    suppressed. Anything other than an explicit falsy marker is treated as
    disabled, so an unexpected value fails safe (skipped, not mailed)."""
    if value is None:
        return False
    text = str(value).strip().lower()
    return text not in ("", "0", "false", "no", "f", "off")


def parse_members_tsv(text: str) -> list[dict]:
    """Parse the TSV export into a list of row dicts keyed by lower-cased header.

    Tolerates surrounding whitespace, blank lines and a UTF-8 BOM. Raises
    ``ValueError`` if a required column is missing from the header."""
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = None
    rows: list[dict] = []
    for raw in reader:
        if not raw or all(cell.strip() == "" for cell in raw):
            continue
        if header is None:
            header = [cell.strip().lstrip("﻿").lower() for cell in raw]
            missing = [c for c in REQUIRED_COLUMNS if c not in header]
            if missing:
                raise ValueError(f"TSV is missing required column(s): {', '.join(missing)}")
            continue
        row = {header[i]: (raw[i].strip() if i < len(raw) else "") for i in range(len(header))}
        rows.append(row)
    if header is None:
        raise ValueError("TSV is empty (no header row)")
    return rows


def classify_eligibility(row: dict) -> tuple[bool, str | None]:
    """Static (input-only) eligibility for one row.

    Returns ``(eligible, skip_reason)``. ``skip_reason`` is one of
    ``not_subscribed`` / ``bounced`` / ``no_email`` when ineligible, else None.
    Database-state skips (already on Farm News, opted out, blacklisted) are
    decided at run time, not here."""
    if not normalize_email(row.get("email")):
        return False, "no_email"
    if not is_subscribed_to_newsletter(row.get("newsletters")):
        return False, "not_subscribed"
    if email_delivery_disabled(row.get("email_disabled")):
        return False, "bounced"
    return True, None


# ── 1Password / file input ───────────────────────────────────────────────────
def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def read_tsv_from_1password(title: str, vault: str) -> str:
    """Fetch the member document into a 0600 temp file, read it, delete it.

    The plaintext never touches disk at a predictable path and is unlinked in a
    ``finally`` block even on error, so the PII is not left behind."""
    fd, path = tempfile.mkstemp(suffix=".tsv")
    os.close(fd)
    os.chmod(path, 0o600)
    try:
        result = subprocess.run(
            ["op", "document", "get", title, "--vault", vault, "--out-file", path],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # op writes its own diagnostics to stderr; surface them without any
            # document contents.
            fail(f"`op document get` failed (exit {result.returncode}): {result.stderr.strip()}")
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ── XML-RPC orchestration (not exercised by the unit tests) ──────────────────
def authenticate() -> tuple[xmlrpc.client.ServerProxy, int]:
    if not ODOO_PASSWORD:
        fail("ODOO_PASSWORD is required")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        fail(f"authentication failed for {ODOO_USER} on {ODOO_URL} db={ODOO_DB}")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    print(f"Authenticated as uid={uid} on db={ODOO_DB} ({ODOO_URL})")
    return models, uid


def call(models, uid, model, method, args, kwargs=None):
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def resolve_xmlid_res_id(models, uid, xmlid: str) -> int:
    module, name = xmlid.split(".", 1)
    rows = call(
        models,
        uid,
        "ir.model.data",
        "search_read",
        [[("module", "=", module), ("name", "=", name)]],
        {"fields": ["res_id"], "limit": 1},
    )
    if not rows:
        fail(f"xmlid {xmlid} not found — is grove_headless installed with mass_mailing on this host?")
    return rows[0]["res_id"]


def resolve_farm_company_id(models, uid) -> int:
    rows = call(
        models, uid, "res.company", "search_read", [[("name", "=", FARM_COMPANY)]], {"fields": ["id"], "limit": 2}
    )
    if not rows:
        rows = call(
            models,
            uid,
            "res.company",
            "search_read",
            [[("name", "ilike", "Goldberry Grove")]],
            {"fields": ["id", "name"], "limit": 5},
        )
    if not rows:
        fail(
            f"could not resolve the Farm company (looked for {FARM_COMPANY!r}); "
            "set FARM_COMPANY to its exact res.company name"
        )
    if len(rows) > 1:
        fail(f"Farm company name is ambiguous ({len(rows)} matches); set FARM_COMPANY to the exact res.company name")
    return rows[0]["id"]


def plan_contact(models, uid, normalized: str, farm_list_id: int) -> str:
    """Read-only disposition for one normalized email.

    Returns one of ``insert`` / ``existing`` / ``skip_opted_out`` /
    ``skip_blacklisted``. Prior-opt-out and blacklist win over a fresh import."""
    if call(models, uid, "mail.blacklist", "search_count", [[("email", "=", normalized)]]):
        return "skip_blacklisted"
    contacts = call(
        models,
        uid,
        "mailing.contact",
        "search_read",
        [[("email_normalized", "=", normalized)]],
        {"fields": ["id"], "limit": 1},
    )
    if not contacts:
        return "insert"
    contact_id = contacts[0]["id"]
    subs = call(
        models,
        uid,
        "mailing.subscription",
        "search_read",
        [[("contact_id", "=", contact_id)]],
        {"fields": ["list_id", "opt_out"]},
    )
    # A prior opt-out on ANY Grove list wins — never bulk-revive it (§9).
    if any(s["opt_out"] for s in subs):
        return "skip_opted_out"
    if any((s["list_id"] and s["list_id"][0]) == farm_list_id and not s["opt_out"] for s in subs):
        return "existing"
    return "insert"


def apply_import(
    models, uid, normalized: str, name: str, email: str, farm_list_id: int, company_id: int, tag_id: int
) -> None:
    """Create the Farm News subscription (and contact / tagged partner) for one
    eligible, not-yet-subscribed email. Never fires a welcome (direct ORM write,
    not the subscribe route)."""
    # res.partner: find within the Farm company (or company-less), create tagged.
    partners = call(
        models,
        uid,
        "res.partner",
        "search_read",
        [[("email", "=", email), ("company_id", "in", [company_id, False])]],
        {"fields": ["id"], "limit": 1},
    )
    if partners:
        partner_id = partners[0]["id"]
        call(models, uid, "res.partner", "write", [[partner_id], {"category_id": [(4, tag_id)]}])
    else:
        call(
            models,
            uid,
            "res.partner",
            "create",
            [{"name": name or email, "email": email, "company_id": company_id, "category_id": [(4, tag_id)]}],
        )
    # mailing.contact: reuse by normalized email, else create.
    contacts = call(
        models,
        uid,
        "mailing.contact",
        "search_read",
        [[("email_normalized", "=", normalized)]],
        {"fields": ["id"], "limit": 1},
    )
    contact_id = (
        contacts[0]["id"]
        if contacts
        else call(models, uid, "mailing.contact", "create", [{"name": name or email, "email": email}])
    )
    # Farm News subscription, opt_out=False. Guard against a race / re-run.
    if not call(
        models,
        uid,
        "mailing.subscription",
        "search_count",
        [[("contact_id", "=", contact_id), ("list_id", "=", farm_list_id)]],
    ):
        call(
            models,
            uid,
            "mailing.subscription",
            "create",
            [{"contact_id": contact_id, "list_id": farm_list_id, "opt_out": False}],
        )


def run(tsv_text: str, apply: bool) -> int:
    rows = parse_members_tsv(tsv_text)
    counts = {
        "total": len(rows),
        "not_subscribed": 0,
        "bounced": 0,
        "no_email": 0,
        "insert": 0,
        "existing": 0,
        "skip_opted_out": 0,
        "skip_blacklisted": 0,
    }

    models, uid = authenticate()
    farm_list_id = resolve_xmlid_res_id(models, uid, FARM_LIST_XMLID)
    company_id = resolve_farm_company_id(models, uid)
    tag_id = None
    if apply:
        existing_tag = call(
            models,
            uid,
            "res.partner.category",
            "search_read",
            [[("name", "=", MIGRATION_TAG)]],
            {"fields": ["id"], "limit": 1},
        )
        tag_id = (
            existing_tag[0]["id"]
            if existing_tag
            else call(models, uid, "res.partner.category", "create", [{"name": MIGRATION_TAG}])
        )

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"\n── Legacy Ghost → Farm News import ── {mode} ── {len(rows)} row(s) in export")
    if not apply:
        print("DRY-RUN (default): read-only; no contacts, subscriptions or tags written. Pass --apply to import.\n")

    for idx, row in enumerate(rows, start=1):
        eligible, reason = classify_eligibility(row)
        if not eligible:
            counts[reason] += 1
            continue
        normalized = normalize_email(row.get("email"))
        disposition = plan_contact(models, uid, normalized, farm_list_id)
        counts[disposition] += 1
        if disposition == "insert" and apply:
            apply_import(
                models,
                uid,
                normalized,
                row.get("name", ""),
                row.get("email", "").strip(),
                farm_list_id,
                company_id,
                tag_id,
            )

    # Report — counts only, never an email (PII).
    print("Counts (no emails):")
    print(f"  total rows ............. {counts['total']}")
    print(f"  skipped not_subscribed . {counts['not_subscribed']}")
    print(f"  skipped bounced ........ {counts['bounced']}")
    print(f"  skipped no_email ....... {counts['no_email']}")
    print(f"  skipped opted_out ...... {counts['skip_opted_out']}")
    print(f"  skipped blacklisted .... {counts['skip_blacklisted']}")
    print(f"  already on Farm News ... {counts['existing']}")
    verb = "inserted" if apply else "to insert"
    print(f"  {verb:.<22} {counts['insert']}")
    if apply:
        print("\nDone. Farm News subscriptions written with opt_out=False; no welcome email sent.")
    else:
        print("\nDone (dry-run). Confirm the counts, then re-run with --apply.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="One-time legacy Ghost → Farm News import (GOL-3564, GOL-3454 §9).")
    parser.add_argument("--apply", action="store_true", help="perform the import (default is a read-only dry-run)")
    parser.add_argument("--tsv", metavar="PATH", help="read the member TSV from a local file instead of 1Password")
    parser.add_argument(
        "--op-document", default=OP_DOCUMENT_TITLE, help="1Password document title (default: the GOL-3455 export)"
    )
    parser.add_argument("--op-vault", default=OP_VAULT, help="1Password vault (default: 'Goldberry Grove - Admin')")
    args = parser.parse_args(argv)

    if args.tsv:
        try:
            with open(args.tsv, encoding="utf-8") as fh:
                tsv_text = fh.read()
        except OSError as exc:
            fail(f"could not read --tsv {args.tsv}: {exc}")
    else:
        tsv_text = read_tsv_from_1password(args.op_document, args.op_vault)

    try:
        return run(tsv_text, apply=args.apply)
    except ValueError as exc:
        fail(str(exc))
        return 1  # unreachable (fail exits), keeps type-checkers happy


if __name__ == "__main__":
    sys.exit(main())
