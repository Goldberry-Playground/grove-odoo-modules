#!/usr/bin/env python3
"""One-shot, idempotent farm ops bootstrap (people layer, Phase 2) via XML-RPC.

Mirrors scripts/setup_books.py: companies are resolved by name (never by id),
every write goes through write_or_plan(), and --dry-run only reads.

Phase 2 Task 4 (this file, so far):
  1. authenticate, resolve the three companies
  2. install hr, hr_timesheet, hr_holidays, hr_expense (idempotent by module state)
  3. one hr.employee per (person, company they work in), hourly_cost = 21.00 set on
     the timesheet/analytic side only (no GL account on the timesheet line)
  4. the "Platform" analytic account under the farm company for Josh's app-build hours

Tasks 8 and 9 (aspect/build/sprint project seed) extend this same script later.

The pure planning functions below carry no Odoo or network dependency and are
unit-tested in scripts/tests/test_setup_ops.py and grove_ops/tests/test_hr_pure.py.

Usage:
    ODOO_URL=... ODOO_DB=... ODOO_USER=... ODOO_PASSWORD=... \\
        python3 setup_ops.py [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys
import xmlrpc.client

# --- Pure planning layer (no Odoo, no network) -----------------------------

# The uniform sweat-equity rate, set on the employee (timesheet/analytic) side
# only. Approved hours valued at this rate are the legal co-op record (s11.4).
# The constant lives here once; no literal 21.0 is scattered through the script.
SWEAT_EQUITY_RATE = 21.00

COMPANY_NAMES = {
    "farm": "Goldberry Grove Farm",
    "nursery": "At The Grove Nursery",
    "woodworking": "George George George Woodworking",
}

# The analytic account that carries Josh's platform/app-build hours (s11.4).
PLATFORM_ANALYTIC_NAME = "Platform"
PLATFORM_ANALYTIC_COMPANY = "farm"

# Core HR modules grove_ops needs installed before it can load (s11.1).
HR_MODULES = ("hr", "hr_timesheet", "hr_holidays", "hr_expense")


def employee_specs(users: list[dict]) -> list[dict]:
    """One employee spec per (user, company the user works in).

    Each input user is ``{"name", "login", "company_keys": [<key>, ...]}`` where
    each key is one of COMPANY_NAMES. The output is one record per (user,
    company): the same person working two companies yields two specs that share
    a login. ``hourly_cost`` is the sweat-equity rate; there is deliberately no
    GL account key on the spec, because a timesheet line never posts to the
    general ledger (s11.4).
    """
    specs: list[dict] = []
    for user in users:
        for company_key in user["company_keys"]:
            if company_key not in COMPANY_NAMES:
                raise ValueError(f"unknown company key {company_key!r}")
            specs.append(
                {
                    "name": user["name"],
                    "login": user["login"],
                    "company_key": company_key,
                    "hourly_cost": SWEAT_EQUITY_RATE,
                }
            )
    return specs


def platform_analytic_spec() -> dict:
    """The 'Platform' analytic account under the farm company (s11.4)."""
    return {"name": PLATFORM_ANALYTIC_NAME, "company_key": PLATFORM_ANALYTIC_COMPANY}


# --- XML-RPC apply layer (reached only via main) ---------------------------

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "Goldberry")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")


class Odoo:
    """Thin XML-RPC wrapper with a dry-run plan buffer (pattern of setup_books)."""

    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.plan: list[str] = []
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        self.uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
        if not self.uid:
            raise SystemExit("authentication failed: check ODOO_* env vars")
        self.models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")

    def execute(self, model, method, *args, **kw):
        return self.models.execute_kw(ODOO_DB, self.uid, ODOO_PASSWORD, model, method, list(args), kw)

    def write_or_plan(self, msg, model, method, *args, **kw):
        if self.dry_run:
            self.plan.append(msg)
            return None
        return self.execute(model, method, *args, **kw)

    def company_id(self, key):
        name = COMPANY_NAMES[key]
        ids = self.execute("res.company", "search", [("name", "=", name)], limit=1)
        if not ids:
            raise SystemExit(f"company not found: {name}")
        return ids[0]


def install_hr_modules(odoo: Odoo):
    """Install the core HR modules, idempotent via ir.module.module state."""
    for tech_name in HR_MODULES:
        mods = odoo.execute("ir.module.module", "search_read", [("name", "=", tech_name)], {"fields": ["state"]})
        if not mods:
            raise SystemExit(f"module not found in registry: {tech_name}")
        state = mods[0]["state"]
        if state == "installed":
            continue
        odoo.write_or_plan(
            f"install module {tech_name} (currently {state})",
            "ir.module.module",
            "button_immediate_install",
            [mods[0]["id"]],
        )


def apply_employees(odoo: Odoo, users: list[dict]):
    """Create one employee per (person, company), idempotent by (login, company)."""
    for spec in employee_specs(users):
        cid = odoo.company_id(spec["company_key"])
        existing = odoo.execute(
            "hr.employee",
            "search",
            ["&", ("work_email", "=", spec["login"]), ("company_id", "=", cid)],
        )
        if existing:
            continue
        odoo.write_or_plan(
            f"create employee {spec['name']} in {spec['company_key']} at {spec['hourly_cost']}/h",
            "hr.employee",
            "create",
            {
                "name": spec["name"],
                "work_email": spec["login"],
                "company_id": cid,
                "hourly_cost": spec["hourly_cost"],
            },
        )


def apply_platform_analytic(odoo: Odoo):
    """Create the Platform analytic account under the farm, idempotent by name."""
    spec = platform_analytic_spec()
    cid = odoo.company_id(spec["company_key"])
    existing = odoo.execute(
        "account.analytic.account",
        "search",
        ["&", ("name", "=", spec["name"]), ("company_id", "=", cid)],
    )
    if existing:
        return
    odoo.write_or_plan(
        f"create analytic account {spec['name']} under {spec['company_key']}",
        "account.analytic.account",
        "create",
        {"name": spec["name"], "company_id": cid},
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Farm ops people-layer bootstrap")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    args = parser.parse_args(argv)

    if not ODOO_PASSWORD:
        raise SystemExit("ODOO_PASSWORD is required")

    # The person -> companies map is confirmed against the prod user list at run
    # time (s4: three farm, four nursery). Until that list is wired in, --dry-run
    # prints what it would do with the resolved companies.
    users: list[dict] = _load_users_placeholder()

    odoo = Odoo(dry_run=args.dry_run)
    install_hr_modules(odoo)
    apply_employees(odoo, users)
    apply_platform_analytic(odoo)

    if args.dry_run:
        print("DRY RUN - planned writes:")
        for line in odoo.plan:
            print(f"  - {line}")
    else:
        print("done")
    return 0


def _load_users_placeholder() -> list[dict]:
    # TODO(gol-4018): read the seven co-op members and their companies from the
    # prod user list (s4) before the promote-window run (GOL-4019). The structure
    # is fixed; the names are filled from Josh's roster, not hard-coded here.
    return []


if __name__ == "__main__":
    sys.exit(main())
