#!/usr/bin/env python3
"""One-shot, idempotent farm ops bootstrap (people layer, Phase 2) via XML-RPC.

Mirrors scripts/setup_books.py: companies are resolved by name (never by id),
every write goes through write_or_plan(), and --dry-run only reads.

People layer (Phase 2 Task 4, GOL-4018):
  1. authenticate, resolve the three companies
  2. install hr, hr_timesheet, hr_holidays, hr_expense (idempotent by module state)
  3. one hr.employee per (person, company they work in), hourly_cost = 21.00 set on
     the timesheet/analytic side only (no GL account on the timesheet line)
  4. the "Platform" analytic account under the farm company for Josh's app-build hours

Project layer (Phase 2 Tasks 8 and 9, GOL-4019):
  5. an aspect project per company per s8.1, each with its own analytic account
  6. the footpad regrade build under the nursery, with seven milestones and a budget
  7. the "Grove Odoo" sprint under the farm, on the Platform analytic account
  Idempotent by (project name, company); a second run creates nothing. --dry-run
  prints a verification table (company / project / analytic / kind).

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


# --- Project seed (Tasks 8 and 9): aspects, the footpad build, the sprint ---

# Aspects per company, s8.1 (Josh edits this list). Order is stable so the
# dry-run verification table reads the same on every run.
ASPECTS = {
    "nursery": [
        "Watering and plant care",
        "Potting and propagation",
        "Order packing and shipping",
        "Marketing",
        "Hoophouse",
        "Catch-all",
    ],
    "farm": [
        "Orchard",
        "Firewood and road stand",
        "Equipment",
        "Pasture and grounds",
        "Marketing",
        "Catch-all",
    ],
    "woodworking": [
        "Shop",
        "Catch-all",
    ],
}

# Footpad regrade build (s7.1): under the nursery, seven milestones, its own
# analytic account and a budget. Exact dates and the budget figure come from
# Josh's footpad doc; placeholders ship if his numbers are not in hand by the
# Train #6 go/no-go, and dates are filled on prod.
FOOTPAD_PROJECT_NAME = "Footpad regrade"
FOOTPAD_MILESTONES = [
    "Survey and stakeout",
    "Clear and grub",
    "Rough grade and drainage",
    "Base material placed",
    "Compaction",
    "Final grade",
    "Inspection and signoff",
]
# TODO(josh): footpad milestone target dates (ISO yyyy-mm-dd) and the budget
# figure. None means "ship the structure, fill the date/amount on prod".
FOOTPAD_MILESTONE_DEADLINES: dict[str, str | None] = {name: None for name in FOOTPAD_MILESTONES}
FOOTPAD_BUDGET_AMOUNT: float | None = None  # TODO(josh): footpad budget

# "Grove Odoo" sprint (s9, s11.4): under the farm, Platform analytic account,
# milestones are trains, tasks carry the GOL id (project_task_code) and the PR
# link (project_task_pull_request). No scrum module. This is the human view of
# the Paperclip queue; it links to GOL issues and does not replace the board.
SPRINT_PROJECT_NAME = "Grove Odoo"
# One row per train as they are cut; dates filled on prod. TODO(josh): dates.
SPRINT_MILESTONES = ["Train #6"]


def aspect_specs() -> list[dict]:
    """One project+analytic spec per aspect per company (s8.1).

    Each aspect project is ``grove_is_aspect=True``, sits in its company, and
    gets a dedicated analytic account named for the aspect under that company.
    The spec is deterministic so a second run creates nothing (Review Focus #5).
    """
    specs: list[dict] = []
    for company_key, aspects in ASPECTS.items():
        for aspect in aspects:
            specs.append(
                {
                    "company_key": company_key,
                    "name": aspect,
                    "analytic_name": aspect,
                    "grove_is_aspect": True,
                    "grove_is_build": False,
                }
            )
    return specs


def footpad_build_spec() -> dict:
    """The footpad regrade build under the nursery (s7.1)."""
    return {
        "company_key": "nursery",
        "name": FOOTPAD_PROJECT_NAME,
        "analytic_name": FOOTPAD_PROJECT_NAME,
        "grove_is_aspect": False,
        "grove_is_build": True,
        "milestones": [{"name": name, "deadline": FOOTPAD_MILESTONE_DEADLINES[name]} for name in FOOTPAD_MILESTONES],
        "budget_amount": FOOTPAD_BUDGET_AMOUNT,
    }


def grove_odoo_sprint_spec() -> dict:
    """The 'Grove Odoo' sprint under the farm with the Platform analytic (s9, s11.4)."""
    return {
        "company_key": "farm",
        "name": SPRINT_PROJECT_NAME,
        "analytic_name": PLATFORM_ANALYTIC_NAME,
        "grove_is_aspect": False,
        "grove_is_build": False,
        "milestones": [{"name": name, "deadline": None} for name in SPRINT_MILESTONES],
        # Task fields the sprint relies on (OCA project_task_code /
        # project_task_pull_request); listed so apply and tests agree.
        "task_fields": ["code", "pull_request_url"],
    }


def project_seed_rows() -> list[dict]:
    """Every project the seed creates, in table order: aspects, footpad, sprint."""
    rows = list(aspect_specs())
    rows.append(footpad_build_spec())
    rows.append(grove_odoo_sprint_spec())
    return rows


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


def ensure_analytic(odoo: Odoo, name: str, cid: int, created: dict):
    """Return the analytic-account id for (name, company), creating it if absent.

    In --dry-run a create returns None (the real id is not known yet); callers
    must tolerate a None analytic id and skip the account link in the plan.
    """
    existing = odoo.execute(
        "account.analytic.account",
        "search",
        ["&", ("name", "=", name), ("company_id", "=", cid)],
    )
    if existing:
        return existing[0]
    created["analytic"] += 1
    return odoo.write_or_plan(
        f"create analytic account {name} (company id={cid})",
        "account.analytic.account",
        "create",
        {"name": name, "company_id": cid},
    )


def ensure_project(odoo: Odoo, spec: dict, created: dict):
    """Create a project idempotent by (name, company); return its id or None.

    The dedicated analytic account is ensured first and linked via account_id.
    """
    cid = odoo.company_id(spec["company_key"])
    analytic_id = ensure_analytic(odoo, spec["analytic_name"], cid, created)
    existing = odoo.execute(
        "project.project",
        "search",
        ["&", ("name", "=", spec["name"]), ("company_id", "=", cid)],
    )
    if existing:
        return existing[0]
    vals = {
        "name": spec["name"],
        "company_id": cid,
        "grove_is_aspect": spec.get("grove_is_aspect", False),
        "grove_is_build": spec.get("grove_is_build", False),
    }
    if analytic_id:
        vals["account_id"] = analytic_id
    created["project"] += 1
    return odoo.write_or_plan(
        f"create project {spec['name']} in {spec['company_key']} "
        f"(aspect={vals['grove_is_aspect']} build={vals['grove_is_build']})",
        "project.project",
        "create",
        vals,
    )


def ensure_milestones(odoo: Odoo, project_id, milestones: list[dict], created: dict):
    """Create each milestone idempotent by (name, project). Skipped in dry-run
    when the project id is not yet known (it was only planned, not created)."""
    for m in milestones:
        if not project_id:
            created["milestone"] += 1
            odoo.write_or_plan(
                f"create milestone {m['name']} (project pending)",
                "project.milestone",
                "create",
                {"name": m["name"]},
            )
            continue
        existing = odoo.execute(
            "project.milestone",
            "search",
            ["&", ("name", "=", m["name"]), ("project_id", "=", project_id)],
        )
        if existing:
            continue
        vals = {"name": m["name"], "project_id": project_id}
        if m.get("deadline"):
            vals["deadline"] = m["deadline"]
        created["milestone"] += 1
        odoo.write_or_plan(
            f"create milestone {m['name']} on project id={project_id}",
            "project.milestone",
            "create",
            vals,
        )


def apply_project_seed(odoo: Odoo, created: dict):
    """Seed aspect projects, the footpad build and the Grove Odoo sprint (s8, s9)."""
    for spec in aspect_specs():
        ensure_project(odoo, spec, created)

    footpad = footpad_build_spec()
    footpad_id = ensure_project(odoo, footpad, created)
    ensure_milestones(odoo, footpad_id, footpad["milestones"], created)

    sprint = grove_odoo_sprint_spec()
    sprint_id = ensure_project(odoo, sprint, created)
    ensure_milestones(odoo, sprint_id, sprint["milestones"], created)


def print_verification_table(created: dict):
    """Print company / project / analytic / kind for every seeded project."""
    print("\nProject seed verification table:")
    print(f"  {'company':<12} {'project':<28} {'analytic':<28} kind")
    for spec in aspect_specs():
        print(f"  {spec['company_key']:<12} {spec['name']:<28} {spec['analytic_name']:<28} aspect")
    f = footpad_build_spec()
    print(f"  {f['company_key']:<12} {f['name']:<28} {f['analytic_name']:<28} build")
    s = grove_odoo_sprint_spec()
    print(f"  {s['company_key']:<12} {s['name']:<28} {s['analytic_name']:<28} sprint")
    print(
        f"  created this run: {created['project']} project(s), "
        f"{created['analytic']} analytic account(s), {created['milestone']} milestone(s)"
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

    created = {"analytic": 0, "project": 0, "milestone": 0}

    odoo = Odoo(dry_run=args.dry_run)
    # People layer (GOL-4018): HR modules, employees, the Platform analytic.
    install_hr_modules(odoo)
    apply_employees(odoo, users)
    apply_platform_analytic(odoo)
    # Project layer (GOL-4019): aspects, the footpad build, the Grove Odoo sprint.
    apply_project_seed(odoo, created)

    print_verification_table(created)
    if args.dry_run:
        print("\nDRY RUN - planned writes:")
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
