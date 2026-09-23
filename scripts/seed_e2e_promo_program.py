#!/usr/bin/env python3
"""Seed the QA Playwright E2E **promo-code** fixture: a ``with_code`` promotion
program on the nursery company (GOL-2478).

Companion to ``seed_e2e_test_inventory.py`` (which seeds the buyable ``AAA …``
product fixtures). This one seeds the missing *discount* half: the
``loyalty.program`` a shopper's promo **code** resolves to.

Why this exists
---------------
``apps/nursery/e2e/checkout-promo-code.spec.ts`` → *"an eligible promo on an
in-stock cart itemizes a discount line that reconciles"* (``@stripe @promo``,
GOL-2432) **skips itself on QA** because the session POST comes back
``400 "This code is invalid"``:

    test.skip(status === 400 && /invalid|not found|no such/i.test(errorBody),
              PROMO_NOT_PROVISIONED_REASON)

QA carries only two ``loyalty.program`` rows — ``Gift Cards`` (company 1) and the
auto ``Volume discount`` (company 9) — and **no** ``loyalty.rule`` carries a
code, so ``grove_headless.promotions._program_for_code`` (which matches a
``with_code`` rule by ``code =ilike``) never resolves ``FLATWOODS`` and the
positive promo-code path shipped in grove-sites #800 has never run end-to-end.

What this seeds
---------------
One ``program_type='promotion'`` / ``trigger='with_code'`` program on **company 9
(At The Grove Nursery)** — the same shape the passing ``TestPromotions._code_program``
fixture builds — with:

* a single ``loyalty.rule`` ``mode='with_code'`` ``code=FLATWOODS`` (no minimum
  qty / amount / product restriction, so any in-stock nursery cart qualifies —
  the spec adds one buyable line and expects the discount to apply), and
* a single ``reward`` = a flat **$10 per-order** discount
  (``reward_type='discount'``, ``discount_mode='per_order'``,
  ``discount_applicability='order'``).

The company **must** be 9: ``promotions.explain_code_failure`` rejects a code
whose ``program.company_id`` differs from the nursery cart's company with *"This
code isn't available in this store."*

Determinism / idempotency
-------------------------
The program is matched by its ``with_code`` rule's ``code`` (case-insensitive).
A converged program is a no-op re-run; an existing one is **reconciled** (name,
active, company, rule minimums cleared, reward amount/mode) rather than forked,
so repeated runs and a QA rebuild converge to the same single program.

Root-scoped name uniqueness (GOL-2451)
--------------------------------------
The nursery (id 9) is a *branch* whose ``root_id`` is Goldberry Grove (id 1).
Odoo 17+ enforces some name-uniqueness at root scope; this program's name
(``Promo FLATWOODS``) does not collide with the two existing programs, and we
create it *on company 9* (matching the Volume-discount program), so the reward
prices against the nursery's own taxes/pricelist.

Prod safety (mirrors seed_e2e_test_inventory.py, GOL-1310)
----------------------------------------------------------
* **Dry run is the DEFAULT** (opt-out); it reports the plan and writes nothing.
  A live run requires an explicit ``DRY_RUN=0``.
* A live run is **REFUSED** unless BOTH the URL host is a known QA host
  (``localhost`` / ``127.0.0.1`` / ``odoo.qa.gatheringatthegrove.com``) AND the
  DB is a known QA DB (``odoo``). Override only with
  ``--force-i-know-this-is-not-qa``. A promo code that discounts every order is
  not something to drop on the live storefront by accident.

Usage
-----
    # Dry run (read-only, DEFAULT): resolves the company + reports the plan.
    ODOO_URL=https://odoo.qa.gatheringatthegrove.com \\
    ODOO_DB=odoo \\
    ODOO_USER=josh@goldberrygrove.farm \\
    ODOO_PASSWORD=<admin-or-api-key> \\
    python3 scripts/seed_e2e_promo_program.py

    # Live: add DRY_RUN=0 -> creates/reconciles the program on a known-QA target.
    DRY_RUN=0 ODOO_URL=... ODOO_DB=odoo ... python3 scripts/seed_e2e_promo_program.py

Knobs (env, all optional):
    DRY_RUN           default "1" (dry)      set "0" for a LIVE run (opt-out)
    E2E_PROMO_CODE    default "FLATWOODS"    the code the spec enters (must match
                                             grove-sites ``E2E_PROMO_CODE``)
    E2E_PROMO_AMOUNT  default "10.00"        flat per-order discount (USD)

Flags (argv):
    --force-i-know-this-is-not-qa   allow a LIVE run against a non-QA target

Exit codes: 0 ok, 1 auth/data failure OR refused non-QA live target.
"""

from __future__ import annotations

import os
import sys
import xmlrpc.client
from urllib.parse import urlsplit as _urlsplit

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
# QA `odoo` DB by default (NOT the prod-style "Goldberry") — see guard_environment().
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
# Dry run is the DEFAULT (opt-out); a live run needs DRY_RUN=0 *and* passes guard.
DRY_RUN = os.getenv("DRY_RUN", "1") != "0"

# --- Prod-safety allowlist (GOL-1310) -------------------------------------
QA_HOSTS = {"localhost", "127.0.0.1", "odoo.qa.gatheringatthegrove.com"}
QA_DBS = {"odoo"}
FORCE_FLAG = "--force-i-know-this-is-not-qa"
FORCE_NOT_QA = FORCE_FLAG in sys.argv

# The nursery company the promo lives on. Its cart is what the spec drives, and
# promotions.explain_code_failure rejects a code whose program company differs.
COMPANY_NAME = "At The Grove Nursery"

# The single source of truth the spec asserts against — keep in lockstep with
# grove-sites `E2E_PROMO_CODE` (apps/nursery/e2e/checkout-promo-code.spec.ts).
PROMO_CODE = os.getenv("E2E_PROMO_CODE", "FLATWOODS").strip()
PROMO_AMOUNT = float(os.getenv("E2E_PROMO_AMOUNT", "10.00"))
PROGRAM_NAME = f"Promo {PROMO_CODE}"


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def guard_environment() -> None:
    """Refuse a LIVE run unless the target is known-QA (GOL-1310). Dry runs are
    always allowed (read-only). Runs before any network call."""
    if DRY_RUN:
        return
    host = (_urlsplit(ODOO_URL).hostname or "").lower()
    host_ok = host in QA_HOSTS
    db_ok = ODOO_DB in QA_DBS
    if host_ok and db_ok:
        return
    if FORCE_NOT_QA:
        print(
            f"WARNING: {FORCE_FLAG} set — live seed against non-QA target host={host!r} db={ODOO_DB!r}. Proceeding.",
            file=sys.stderr,
        )
        return
    reasons = []
    if not host_ok:
        reasons.append(f"host {host!r} not in QA_HOSTS {sorted(QA_HOSTS)}")
    if not db_ok:
        reasons.append(f"db {ODOO_DB!r} not in QA_DBS {sorted(QA_DBS)}")
    fail(
        "REFUSED live seed against a non-QA target (" + "; ".join(reasons) + "). "
        "This script provisions a promo code that discounts every order. "
        f"Point it at QA, run with DRY_RUN=1, or pass {FORCE_FLAG} if you are "
        "certain this is not production."
    )


def authenticate() -> tuple[xmlrpc.client.ServerProxy, int]:
    if not ODOO_PASSWORD:
        fail("ODOO_PASSWORD env var is required (admin password or a user API key)")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        fail(f"Authentication failed for user {ODOO_USER} on db {ODOO_DB}")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    print(f"Authenticated as uid={uid} on db={ODOO_DB}")
    return models, uid


def call(models, uid, model: str, method: str, args: list, kwargs: dict | None = None):
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def _program_vals(company_id: int) -> dict:
    """The GOL-2478 QA shape — mirrors ``TestPromotions._code_program`` defaults:
    a with_code promotion whose single reward is a flat per-order discount."""
    return {
        "name": PROGRAM_NAME,
        "program_type": "promotion",
        "trigger": "with_code",
        "applies_on": "current",
        "company_id": company_id,
        "active": True,
        "rule_ids": [
            (
                0,
                0,
                {
                    "mode": "with_code",
                    "code": PROMO_CODE,
                    # minimum_qty=1 (Odoo's default) / no minimum_amount / no
                    # product restriction: any in-stock nursery cart with at
                    # least one line qualifies (the spec adds a single buyable
                    # line). Set explicitly so create + reconcile converge.
                    "minimum_qty": 1,
                    "minimum_amount": 0.0,
                },
            )
        ],
        "reward_ids": [
            (
                0,
                0,
                {
                    "reward_type": "discount",
                    "discount": PROMO_AMOUNT,
                    "discount_mode": "per_order",
                    "discount_applicability": "order",
                    "description": f"${PROMO_AMOUNT:.0f} off (promo {PROMO_CODE})",
                },
            )
        ],
    }


def reconcile_program(models, uid, company_id: int, existing_program_id: int) -> None:
    """Bring an existing program back to the canonical shape (idempotent re-run):
    reset name/active/company and clear any rule minimums a prior run/click-op
    left, and pin the reward amount + mode. Only the fields the spec depends on."""
    call(
        models,
        uid,
        "loyalty.program",
        "write",
        [
            [existing_program_id],
            {
                "name": PROGRAM_NAME,
                "active": True,
                "company_id": company_id,
                "program_type": "promotion",
                "trigger": "with_code",
                "applies_on": "current",
            },
        ],
    )
    prog = call(
        models,
        uid,
        "loyalty.program",
        "read",
        [[existing_program_id], ["rule_ids", "reward_ids"]],
    )[0]
    rule_ids = prog["rule_ids"]
    reward_ids = prog["reward_ids"]
    # Match the with_code rule to reset its minimums/restrictions to "no gate".
    for rid in rule_ids:
        rule = call(models, uid, "loyalty.rule", "read", [[rid], ["mode", "code"]])[0]
        if rule["mode"] == "with_code":
            call(
                models,
                uid,
                "loyalty.rule",
                "write",
                [
                    [rid],
                    {
                        "code": PROMO_CODE,
                        "minimum_qty": 1,
                        "minimum_amount": 0.0,
                        "product_ids": [(5, 0, 0)],
                    },
                ],
            )
    for rwid in reward_ids:
        call(
            models,
            uid,
            "loyalty.reward",
            "write",
            [
                [rwid],
                {
                    "reward_type": "discount",
                    "discount": PROMO_AMOUNT,
                    "discount_mode": "per_order",
                    "discount_applicability": "order",
                },
            ],
        )
    print(f"  ~ reconciled program id={existing_program_id} to canonical shape")


def main() -> None:
    print(
        f"Target: {ODOO_URL} db={ODOO_DB} company={COMPANY_NAME}  "
        f"code={PROMO_CODE!r} amount=${PROMO_AMOUNT:.2f} (per-order)  "
        f"DRY_RUN={'yes' if DRY_RUN else 'NO — LIVE'}"
    )
    if not PROMO_CODE:
        fail("E2E_PROMO_CODE resolved empty; a with_code rule needs a non-empty code")
    guard_environment()
    models, uid = authenticate()

    company_ids = call(models, uid, "res.company", "search", [[("name", "=", COMPANY_NAME)]], {"limit": 1})
    if not company_ids:
        fail(f"Company '{COMPANY_NAME}' not found")
    company_id = company_ids[0]
    print(f"Company '{COMPANY_NAME}' id={company_id}")

    # Match by the with_code rule's code (case-insensitive) so a converged
    # program is a no-op and we never fork a duplicate.
    rule_ids = call(
        models,
        uid,
        "loyalty.rule",
        "search",
        [[("mode", "=", "with_code"), ("code", "=ilike", PROMO_CODE)]],
        {"limit": 1},
    )
    existing_program_id = 0
    if rule_ids:
        existing_program_id = call(models, uid, "loyalty.rule", "read", [[rule_ids[0]], ["program_id"]])[0][
            "program_id"
        ][0]
        print(f"  = with_code rule {PROMO_CODE!r} exists (rule id={rule_ids[0]}, program id={existing_program_id})")

    if DRY_RUN:
        if existing_program_id:
            print(
                f"  + WOULD RECONCILE loyalty.program id={existing_program_id} ('{PROGRAM_NAME}') on company {company_id}"
            )
        else:
            print(
                f"  + WOULD CREATE loyalty.program '{PROGRAM_NAME}' (with_code '{PROMO_CODE}', ${PROMO_AMOUNT:.0f}/order) on company {company_id}"
            )
        print("\nDry run — no writes performed. Re-run with DRY_RUN=0 against QA to apply.")
        return

    if existing_program_id:
        reconcile_program(models, uid, company_id, existing_program_id)
        program_id = existing_program_id
    else:
        program_id = call(
            models,
            uid,
            "loyalty.program",
            "create",
            [_program_vals(company_id)],
            {"context": {"allowed_company_ids": [company_id], "company_id": company_id}},
        )
        print(f"  + created loyalty.program '{PROGRAM_NAME}' (id={program_id})")

    # Read back the canonical state as proof.
    prog = call(
        models,
        uid,
        "loyalty.program",
        "read",
        [[program_id], ["name", "program_type", "trigger", "company_id", "active"]],
    )[0]
    rule = call(
        models,
        uid,
        "loyalty.rule",
        "search_read",
        [[("program_id", "=", program_id), ("mode", "=", "with_code")]],
        {"fields": ["code", "mode", "minimum_qty", "minimum_amount"]},
    )
    reward = call(
        models,
        uid,
        "loyalty.reward",
        "search_read",
        [[("program_id", "=", program_id)]],
        {"fields": ["reward_type", "discount", "discount_mode", "discount_applicability"]},
    )
    print(f"\nDone. program={prog}\n  rule={rule}\n  reward={reward}")
    print(f"Shoppers on the {COMPANY_NAME} storefront can now redeem code {PROMO_CODE!r}.")


if __name__ == "__main__":
    main()
