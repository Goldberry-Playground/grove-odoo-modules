# Farm Ops Phase 2 (People and Projects on Odoo) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The seven co-op members have an internal user and one employee record per company they work in, with hours costed at the uniform $21/h sweat-equity rate on the analytic side only; worker/lead/owner groups and record rules scope what each role sees; a week is submitted Friday and approved by another owner-operator by Monday with an immutable trail; every aspect of every business and every build (the footpad regrade) and the app-build work ("Grove Odoo" sprints) is an Odoo project with its own analytic account; and each in-scope Asana project has one documented destination among those projects so the import (GOL-2094) can run.

**Architecture:** One new module `grove_ops` in grove-odoo-modules carries the worker/lead/owner groups and record rules, the employee aspect-coverage field, the week-approval guardrails layered on OCA `hr_timesheet_sheet` (non-self-approval, immutable approved-edit history, reopen-reapprove, member-wide read), the sweat-equity register report, and the data fixtures for aspects, location tags, the footpad build project and the "Grove Odoo" sprint project. OCA project/timesheet/web modules arrive through odoocker's third-party addon list (Phase 1 already added the accounting set). Core HR and purchase modules install from the base image. Company-specific setup (install HR, one employee per person per company, $21 cost, aspect/build/sprint seed, group assignment) is an idempotent XML-RPC script in `scripts/`, with its planning logic pure-tested. The Asana import mapping is a table in this plan and is consumed by GOL-2094's migration script; GOL-4019 applies the project seed to prod.

**Tech Stack:** Odoo 19.0 Community, Python 3.12, OCA 19.0, Odoo `TransactionCase`, pytest for pure modules, XML-RPC scripts.

**Spec:** `2026-11-05-farm-ops-books-design.md` (this folder), sections 5, 7, 8, 9, 11 and 14 through 16. Phase 1 plan: `2026-11-05-farm-books-phase1.md`.

## Deviations from the spec, decided here

- **Adopt OCA `hr_timesheet_sheet` 19.0; do not port and do not build from scratch.** The spec (written 2026-11-05) said the module "exists on 18.0 but not yet 19.0" and told the builder to port it or build a minimal week model. Re-verified 2026-11-19: `OCA/timesheet@19.0/hr_timesheet_sheet` now exists, version `19.0.1.0.2`, so the "port" option collapses to a plain install and building a sheet model from scratch is wasted work. We install it and add only the sweat-equity guardrails in `grove_ops` (see Task 7). This pulls two transitive dependencies: core `sale_timesheet` (in the base image) and OCA `web_widget_x2many_2d_matrix` (added to the addon list in Task 2).
- **`grove_ops` is licensed AGPL-3, not LGPL-3.** `hr_timesheet_sheet` and the OCA `project_*` modules `grove_ops` depends on are AGPL-3; a module that `depends` on them is a derived work. The farm's Odoo is internal to the seven co-op members, so AGPL's network clause is satisfied by the members having source access; this is a one-line note for Terra, not a reopened decision. (`grove_schedule_f` shipped LGPL-3 in Phase 1 and is not touched here.)
- **`project_task_material` is dropped from Phase 2.** The spec listed it as a 19.0 module; re-verified 2026-11-19 it has no 19.0 branch in any OCA repo. s7.1 already marks per-task materials "optional per task," so no capability the Done bar names is lost. Revisit when OCA ports it; a bill-of-materials line on a task is not needed for the footpad (block count and pipe sticks live in the task description for now).
- **Groups install without reassigning anyone.** The three groups and record rules are created and the seven existing users keep every right they have today (s11.2, Josh). Assigning each user a single role is a post-freeze Josh action off a list he signs off; the setup script has a `--apply-roles` flag that is a no-op until that list exists.
- **The project seed is a fixture plus an idempotent script, applied to prod by GOL-4019.** Aspect projects, the footpad build and the "Grove Odoo" sprint are created by `scripts/setup_ops.py` (XML-RPC, idempotent by project name + company), the same pattern as Phase 1's `setup_books.py`. Building the fixtures and script freezes on Train #6; running them on prod is a promote-window step (GOL-4019), not a code-freeze deliverable.
- **Asana import execution is out of this plan and out of the freeze by design.** This plan carries the mapping table (the "CEO mapping" GOL-2094 is blocked on). The import itself runs after the prod target projects exist (GOL-4019) and after Train #6 promote (Wed 2026-12-02 at the earliest), tracked in GOL-2094. See Task 10.

## Global Constraints

- Repo `Goldberry-Playground/grove-odoo-modules`, branch `feature/grove-ops-phase2` cut from `origin/main` (local checkouts are stale; never branch from a local HEAD). Work in a worktree. Push over HTTPS via the gh-token-broker; unset `credential.helper` first. The SSH agent is broken in non-interactive shells.
- Module name `grove_ops`, version `19.0.1.0.0`, license `AGPL-3`, author "Gathering at the Grove". `depends` (final set, trimmed to what each task needs): `project`, `hr_timesheet`, `hr_holidays`, `hr_expense`, `analytic`, `hr_timesheet_sheet`, `project_milestone_status`, `project_timeline`, `project_task_code`, `project_task_pull_request`, `project_hr_expense_analytic`, `account_budget_oca`.
- Company ids in prod: farm 1, nursery 3, woodworking 2. Aspect projects are created per company per s8.1; the footpad build sits under the nursery (s7.1); the "Grove Odoo" sprint sits under the farm (s9, s11.4).
- Every Odoo-runtime test module is listed in `grove_ops/tests/__init__.py` AND in `collect_ignore_glob` in the root `conftest.py`, exactly as `grove_headless` and `grove_schedule_f` are. Pure test modules are loaded by file path with `importlib`, never through the package.
- Tests are tagged `@tagged("grove_ops", "post_install", "-at_install")`.
- Lint: `ruff check . --select E,F,I --line-length 120 && ruff format --check . --line-length 120`.
- Odoo tests: `bash scripts/dev/run_odoo_tests.sh grove_ops` (extend the harness's OCA clone list from Task 2). Pure tests: `python3 -m pytest grove_ops/tests/test_*pure*.py scripts/tests/ -v`.
- Hourly cost `21.00` is set on the employee (timesheet/analytic side) only; nothing posts to the general ledger from a timesheet line (s11.4). The constant lives in one pure function; no literal `21.0` scattered in scripts.
- Approved hours are the legal sweat-equity record (s11.4, Josh): every change to an approved week keeps who/when/old/new; corrections go through reopen-edit-reapprove; a week is approved by someone other than its owner; approvals and the register are readable by every member. These are enforced in code and tested, not by convention.
- No secrets in code or output. Scripts read `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_PASSWORD` from the environment like `scripts/setup_books.py`. No new credential is introduced in Phase 2 (the Discord bot token and its user are Phase 3).
- `.github/workflows/**` and `**/*stripe*` are protected paths; name files to avoid `stripe` in the path. A CI-workflow change needs Josh's approval and enqueue.
- Commit messages: `feat(grove_ops): ...`, `ci: ...`, `feat(scripts): ...`, `docs: ...`, each ending with the attribution line in effect for this session.
- Customer-facing or member-facing copy (register headers, Discord text, the runbook): no em dashes.

## Review Focus

1. **A worker opens the accounting or sales menus, or reads another person's hours.** Expect: menus not visible and an `AccessError` on direct model access; the only sales-adjacent thing a worker can reach is a transfer they are assigned to pack. Pinned in Task 6.
2. **Someone approves their own week.** Expect: `ValidationError` naming the rule; the week stays submitted. Pinned in Task 7.
3. **An approved week is edited.** Expect: the edit is blocked until the week is reopened, and reopen-edit-reapprove leaves a full who/when/old/new trail that every member can read. Pinned in Task 7.
4. **A person works for two companies in one day.** Expect: one login, one employee record per company, and the hours land in the company that owns the task's project (s5). Pinned in Task 4 and Task 8.
5. **The setup script runs twice.** Expect: no duplicate employees, projects, analytic accounts or groups; the second run reports zero created. Pinned in Task 9.
6. **An in-scope Asana project has no destination, or a destination project does not exist.** Expect: the mapping table in Task 10 covers every in-scope project exactly once, and GOL-2094's `assert_scope_names` guard fails loudly rather than importing to a default. Pinned in Task 10.

## What can make the Fri 2026-11-27 freeze and what cannot

Josh ruled (2026-11-13) that farm-ops Phase 2 stays whole on Train #6, so the target is the whole module and its scripts frozen by Fri 11-27, with prod execution in the promote window. Honest assessment:

**Frozen code/fixtures by 11-27 (achievable):**
- This plan (Task 1) — now.
- OCA addons in the odoocker image (Task 2) — the gating infra item; must merge early so the image rebuild rides Train #6. Josh approves and enqueues.
- `grove_ops` skeleton + CI (Task 3).
- HR install + employee-per-person-per-company + $21 cost script (Task 4).
- Worker/lead/owner groups + record rules (Task 6).
- Aspect projects + location tags + employee aspect-coverage (Task 8).
- "Grove Odoo" sprint project fixture (Task 9).
- The Asana mapping table (Task 10) — the deliverable this card owes GOL-2094.

**At risk for 11-27 (the schedule risk, has a fallback):**
- Week-approval guardrails + sweat-equity register (Task 7). Fallback if it slips: ship `hr_timesheet_sheet` install plus the non-self-approval constraint in Train #6, and fast-follow the immutable-history register. Flagged to Josh in the plan-approval ask.
- Footpad build project with its seven milestones (Task 8b): the project and milestone *structure* can freeze with placeholder dates; the exact milestone dates and the budget figures need Josh's footpad numbers. If they are not in hand by 11-25 the project ships with placeholders and dates are filled on prod.

**Cannot make the freeze, by design (post-promote, Wed 12-02 at earliest):**
- Running the HR install, employee seed, project seed and group creation on prod — these are promote-window steps (GOL-4019 for the projects), not code-freeze deliverables.
- The Asana import execution (GOL-2094) — gated on prod target projects (GOL-4019) and Josh's confirmation of the two open mapping specifics below.
- Per-user role assignment (s11.2) — blocked on Josh's role list; groups install without it and nobody loses access.

---

### Task 1: This plan and PR

**Files:**
- Create: `docs/superpowers/plans/2026-11-19-farm-ops-phase2.md` (this plan)

- [ ] **Step 1: Land this plan**

```bash
git add docs/superpowers/plans/2026-11-19-farm-ops-phase2.md
git commit -m "docs(grove_ops): Phase 2 (people and projects) implementation plan"
```

Open the PR (docs-only; no module manifest to bump, no Odoo tests to run). Title: "docs: farm ops Phase 2 plan (people, aspects, projects, Asana import mapping)". This is the first Phase 2 card; the others below become their own PRs against the same `feature/grove-ops-phase2` work or separate branches as scoped. The `module-grove-ops` ownership partition is added in Task 3 together with the module skeleton (the linter requires the `grove_ops/` directory to exist).

---

### Task 2: OCA modules in the odoocker image

**Files (repo `Goldberry-Playground/odoocker-goldberrygrove`, branch `feat/oca-ops-addons` from `origin/main`):**
- Modify: `odoo/third-party-addons.txt`

**Interfaces:**
- Produces: the production image additionally contains `project_milestone_status`, `project_timeline`, `web_timeline`, `project_task_code`, `project_task_pull_request`, `project_hr_expense_analytic`, `account_budget_oca`, `hr_timesheet_sheet`, `hr_timesheet_time_control`, `web_widget_x2many_2d_matrix`, `web_responsive`, `web_pwa_customize`, `web_notify`.

- [ ] **Step 1: Add the repositories (one `public` line per repo, each module `true`)**

Append after the Phase 1 accounting block (verified present on `main`: `account-reconcile`, `bank-statement-import`, `account-financial-tools`, `account-financial-reporting`, `server-ux`, `reporting-engine`, `mis-builder` with `mis_builder` + `mis_builder_budget`). `mis_builder_budget` is already in the file from Phase 1; do not re-add it.

```
# OCA project/timesheet/web addons for grove_ops (farm ops, Phase 2)
public https://github.com/OCA/project.git project_milestone_status true project_timeline true project_task_code true project_task_pull_request true project_hr_expense_analytic true
public https://github.com/OCA/web.git web_timeline true web_widget_x2many_2d_matrix true web_responsive true web_pwa_customize true web_notify true
public https://github.com/OCA/timesheet.git hr_timesheet_sheet true hr_timesheet_time_control true
public https://github.com/OCA/account-budgeting.git account_budget_oca true
```

Module/branch existence re-verified 2026-11-19 against the 19.0 trees: every module above returns a `__manifest__.py` on `19.0`. `project_task_material` (in the spec's list) has **no** 19.0 branch and is intentionally omitted (see Deviations). `hr_timesheet_sheet` depends on core `sale_timesheet` (base image) and OCA `web_widget_x2many_2d_matrix` (added above); `project_timeline` depends on `web_timeline` (added above). No new Python wheels are required.

- [ ] **Step 2: Verify the clone step locally**

Run from a scratch copy of the odoocker `odoo/` dir: `ODOO_TAG=19.0 THIRD_PARTY_ADDONS=/tmp/tpa ENTERPRISE_ADDONS=/tmp/ea bash odoo/clone-addons.sh`. Expected: no `ERROR:` lines; `ls /tmp/tpa` shows all thirteen new module directories alongside the Phase 1 ones.

- [ ] **Step 3: Commit and open a Draft PR**

```bash
git commit -am "feat(odoo): add OCA project/timesheet/web addons for grove_ops (Phase 2)"
```

CI's `Validate third-party addon branches` and `Docker — Odoo Image` must be green. Josh approves and enqueues. The image rebuild rides Train #6; prod picks it up at promote. `grove_ops` cannot install on prod before this merges. **This is the gating item; open it first of the code tasks.**

---

### Task 3: `grove_ops` skeleton, test harness, CI wiring

**Files:**
- Create: `grove_ops/__init__.py`, `grove_ops/__manifest__.py`, `grove_ops/models/__init__.py`, `grove_ops/tests/__init__.py`
- Modify: `scripts/dev/run_odoo_tests.sh` (extend the OCA clone list with `OCA/project`, `OCA/web`, `OCA/timesheet`, `OCA/account-budgeting`), `conftest.py` (stub `grove_ops`, `grove_ops.models`, `grove_ops.tests` as the other modules are stubbed; add Odoo test globs as later tasks create them), `.github/workflows/ci.yml` (add `grove_ops` to `--init` / `/grove_ops` to `--test-tags`, and the four new OCA clone paths; keep existing required-context job names unchanged), `ownership.yml` (partition `module-grove-ops`, paths `grove_ops/**`, owner `engineering-ada`, review `[devops-terra]`, wave 1 — the linter requires `grove_ops/` to exist, which this task creates)

**Interfaces:**
- Produces an installable empty `grove_ops` whose test harness runs green with `--allow-empty`.

- [ ] **Step 1: Write the manifest and empty package.** `__manifest__.py` keys: name "Grove Operations", version `19.0.1.0.0`, license `AGPL-3`, category "Project", summary "Aspects and builds as projects, worker/lead/owner roles, week approval, sweat-equity register", depends as in Global Constraints, `data: []` for now, `installable: True`, `application: False`, `auto_install: False`.
- [ ] **Step 2: Extend `run_odoo_tests.sh` and `ci.yml`** with the four new OCA repos (branch `19.0`, depth 1) on the addons path. The `.github/workflows/**` change needs Josh's approval and enqueue.
- [ ] **Step 3: Run the harness on the empty module** (`bash scripts/dev/run_odoo_tests.sh grove_ops --allow-empty`). Expected: "Modules loaded." and exit 0.
- [ ] **Step 4: Copy spec reference, commit**

```bash
git commit -m "feat(grove_ops): module skeleton, OCA-aware test harness, CI wiring"
```

Open the PR as Draft; later tasks push to it.

---

### Task 4: HR install, employee per person per company, $21 cost

**Files:**
- Create: `scripts/setup_ops.py` (XML-RPC, idempotent, pattern of `scripts/setup_books.py`) — this task writes the HR half; Tasks 6, 8, 9 add to the same script.
- Create: `scripts/tests/test_setup_ops.py` (pure; loads the script by path)
- Create: `grove_ops/models/hr_employee.py` (the cost helper if an onchange/default is wanted), `grove_ops/tests/test_hr_pure.py` for the rate function

**Interfaces:**
- Produces pure functions: `SWEAT_EQUITY_RATE = 21.00`; `employee_specs(users: list[dict]) -> list[dict]` — one spec per (user, company the user works in), each with `name`, `login`, `company_id`, `hourly_cost = SWEAT_EQUITY_RATE`; `platform_analytic_spec() -> dict` for the "Platform" analytic account on the farm company (s11.4).
- Script: installs `hr`, `hr_timesheet`, `hr_holidays`, `hr_expense` (idempotent via `ir.module.module` state check), then for the seven internal users creates one `hr.employee` per company they work in with `hourly_cost = 21.00` set on the timesheet/analytic side only (no GL account on the timesheet line), idempotent by (user, company). Creates the "Platform" analytic account under the farm company for Josh's app-build hours.
- Flags: `--dry-run` prints the plan; which users map to which companies is read from the current prod user list (three farm, four nursery per s4) and printed for confirmation in dry-run.

- [ ] **Step 1: Write the failing pure tests** — rate is exactly 21.00; `employee_specs` yields one record per (person, company) and never a GL account on the cost; a person in two companies yields two specs with the same login.
- [ ] **Step 2: Run to verify they fail** (`python3 -m pytest scripts/tests/test_setup_ops.py grove_ops/tests/test_hr_pure.py -v`).
- [ ] **Step 3: Implement** the pure functions and the XML-RPC install/create, idempotent.
- [ ] **Step 4: Dry-run against prod read-only** with Josh's env vars; expected: the plan lists the HR modules to install, seven users, the farm/nursery employee split, and the Platform analytic account.
- [ ] **Step 5: Run pure tests, lint, commit** (`feat(scripts): HR install and per-company employees at the sweat-equity rate`).

---

### Task 5: Employee aspect coverage field

**Files:**
- Create: `grove_ops/models/hr_employee.py` (extend; add the m2m), `grove_ops/views/hr_employee_views.xml`
- Modify: `grove_ops/models/__init__.py`, `__manifest__.py`
- Test: `grove_ops/tests/test_aspect_coverage.py`

**Interfaces:**
- Produces `hr.employee.grove_aspect_project_ids = fields.Many2many("project.project", domain=[("grove_is_aspect","=",True)])` — the aspects a person covers, the planner input named in s8.3. (`grove_is_aspect` Boolean is added on `project.project` in Task 8.)

- [ ] **Step 1: Write the failing test** — an employee can be assigned several aspect projects; the domain excludes non-aspect projects.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests, lint, commit** (`feat(grove_ops): employee aspect-coverage field`).

---

### Task 6: Worker / lead / owner groups and record rules

**Files:**
- Create: `grove_ops/security/groups.xml`, `grove_ops/security/record_rules.xml`, `grove_ops/security/ir.model.access.csv`
- Modify: `__manifest__.py` (`data`)
- Test: `grove_ops/tests/test_roles.py`

**Interfaces:**
- Produces three group xmlids: `grove_ops.group_worker`, `grove_ops.group_lead`, `grove_ops.group_owner`. `group_worker` implies `base.group_user` and `hr_timesheet.group_hr_timesheet_user` (own hours); `group_lead` implies `group_worker` plus the planner, crew hours, week approval and time-off approval reads; `group_owner` implies `group_lead` plus everything (accounting, sales). Every owner-operator is a lead (s11.2, Josh); today that is everyone.
- Record rules (s11.2):
  - Worker sees own `project.task`, own `account.analytic.line` (timesheets), own `hr.leave`, and the `stock.picking` transfers they are assigned to pack (picking, delivery address, contents, validate), and nothing else.
  - Worker cannot reach accounting menus, sales orders, prices, customer financials, or other people's hours — menus hidden by group and models blocked by ACL/rule.
  - Lead sees the crew's tasks and hours; owner sees all.

- [ ] **Step 1: Write the failing tests** (Review Focus #1):

```python
def test_worker_cannot_read_account_move(self):
    with self.assertRaises(AccessError):
        self.env["account.move"].with_user(worker).search([], limit=1)

def test_worker_cannot_read_other_peoples_timesheets(self):
    # worker sees own analytic lines, not the lead's
    ...

def test_worker_can_read_assigned_picking_only(self):
    # assigned-to-pack picking readable; an unrelated picking not
    ...

def test_worker_cannot_see_sale_order_or_prices(self): ...

def test_lead_sees_crew_tasks_and_hours(self): ...

def test_installing_groups_does_not_change_existing_user_rights(self):
    # a user with today's rights keeps them after the groups install (no implied-group side effect removes access)
    ...
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the groups, implied-group chain, record rules and ACL. Confirm the packing exception is limited to `stock.picking` the user is assigned to.
- [ ] **Step 4: Run tests, lint, commit** (`feat(grove_ops): worker/lead/owner groups and record rules`).

---

### Task 7: Week approval guardrails and the sweat-equity register

**Files:**
- Create: `grove_ops/models/hr_timesheet_sheet.py` (extend OCA's model), `grove_ops/models/sweat_equity_register.py` (`_auto = False` SQL view), `grove_ops/views/sheet_views.xml`, `grove_ops/views/register_views.xml`, `grove_ops/security/ir.model.access.csv` (append)
- Modify: `__manifest__.py` (`data`, add `hr_timesheet_sheet` to `depends`), `__init__.py`, `conftest.py`, `tests/__init__.py`
- Test: `grove_ops/tests/test_week_approval.py`, `grove_ops/tests/test_register.py`

**Interfaces:**
- Produces on `hr_timesheet.sheet` (OCA model):
  - Constraint `_check_not_self_approved`: the user who moves a sheet to `approved` must differ from the sheet's `employee_id.user_id`. `ValidationError`: "A week is approved by another owner-operator, never its own owner."
  - Immutable trail: `mail.thread` tracking on the hours and state, plus a lock so that once `approved`, timesheet lines in the sheet's period for that employee cannot be written without first a `action_grove_reopen()` that sets state back to `draft`, posts a chatter note (who, when, reason), and requires a fresh non-self approval. The reopen-edit-reapprove sequence leaves the full who/when/old/new trail.
  - `action_grove_reopen()`: owner-or-lead only; raises for a worker.
- Produces model `grove.sweat.equity.register` (`_auto = False`): fields `employee_id`, `company_id`, `year`, `hours` (sum of approved analytic-line hours), `amount` (`hours * 21.00`), readable by every member (ACL read for `group_worker`). A companion "days off" list reads approved `hr.leave`.
- Josh's app-build hours log against the "Grove Odoo" sprint project (Task 9) under the "Platform" analytic account (Task 4); the register shows platform hours attributed to the co-op (s11.4).

- [ ] **Step 1: Write the failing tests** (Review Focus #2 and #3):

```python
def test_owner_cannot_approve_own_week(self):
    with self.assertRaisesRegex(ValidationError, "another owner-operator"):
        sheet.with_user(sheet.employee_id.user_id).action_timesheet_done()  # OCA approve action name

def test_other_owner_can_approve(self): ...

def test_approved_week_lines_are_locked_until_reopen(self):
    sheet.action_timesheet_done()   # by another owner
    with self.assertRaises((ValidationError, UserError)):
        line.write({"unit_amount": 9.0})
    sheet.action_grove_reopen()     # trail recorded
    line.write({"unit_amount": 9.0})  # now allowed
    # reapprove requires a non-self approver again

def test_reopen_keeps_full_trail(self):
    # message_ids / tracking carry old and new value, who and when
    ...

def test_register_values_hours_at_21(self):
    # 10 approved hours -> amount 210.0; every member can read the register
    ...
```

- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** Read OCA `hr_timesheet_sheet`'s approve/lock flow (`hr_timesheet_sheet/models/hr_timesheet_sheet.py`) for the real action and state names; the tests pin the outcome, not the internal names.
- [ ] **Step 4: Run tests, lint, commit** (`feat(grove_ops): non-self week approval, immutable trail, sweat-equity register`).

> **Freeze note:** this is the schedule risk. If Step 3 cannot be finished and reviewed by 11-27, ship `hr_timesheet_sheet` install plus `_check_not_self_approved` on Train #6 and move the immutable-trail register to a fast-follow card. The plan-approval ask flags this to Josh.

---

### Task 8: Aspects as projects, location tags, and the footpad build

**Files:**
- Create: `grove_ops/models/project_project.py` (add `grove_is_aspect`, `grove_is_build` Booleans), `grove_ops/data/aspect_tags.xml` (location tag set), `grove_ops/views/project_views.xml`
- Modify: `scripts/setup_ops.py` (aspect + build seed), `scripts/tests/test_setup_ops.py`
- Test: `grove_ops/tests/test_projects.py`

**Interfaces (8a, aspects, s8.1):**
- Produces pure `aspect_specs() -> list[dict]` in `setup_ops.py`: one per aspect per company, each creating a `project.project` with `grove_is_aspect=True`, a dedicated `account.analytic.account`, and the company. Starting list (editable by Josh):
  - Nursery: watering and plant care, potting and propagation, order packing and shipping, marketing, hoophouse, catch-all.
  - Farm: orchard, firewood and road stand, equipment, pasture and grounds, marketing, catch-all.
  - Woodworking: shop, catch-all.
- Location tag set (shared): hoophouse, orchard block 2, road stand, etc., as `project.tags`.
- Recurring work uses core task recurrence; order packing is read from ready pickings, not typed (s8.2) — one recurring "packing block" task per nursery carries the hours. (The generated-packing summary and the planner are Phase 3; Phase 2 ships the projects and the coverage field they consume.)

**Interfaces (8b, footpad build, s7):**
- Produces pure `footpad_build_spec() -> dict`: a `project.project` under the **nursery** with `grove_is_build=True` and its analytic account, seven milestones (`project.milestone` + `project_milestone_status`) with target dates, a budget per project via `account_budget_oca` on the analytic account, the board (kanban by milestone) and timeline (`project_timeline`) views, and a daily-log chatter template.
- Milestone dates and budget figures come from Josh's footpad doc; if not in hand by 11-25 the spec ships with placeholder dates flagged `# TODO(josh): footpad dates` and dates are filled on prod.

- [ ] **Step 1: Write the failing tests** — `aspect_specs` yields one project+analytic per aspect per company with the s8.1 names; `grove_is_aspect` filters the coverage domain (Task 5); the footpad build has exactly seven milestones and sits under the nursery.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the Booleans, tags, specs and views.
- [ ] **Step 4: Run tests, lint, commit** (`feat(grove_ops): aspect projects, location tags, footpad build seed`).

---

### Task 9: The "Grove Odoo" sprint project

**Files:**
- Modify: `scripts/setup_ops.py` (sprint seed), `scripts/tests/test_setup_ops.py`, `grove_ops/tests/test_projects.py`

**Interfaces:**
- Produces pure `grove_odoo_sprint_spec() -> dict`: a `project.project` named "Grove Odoo" under the **farm** company (s9, s11.4) with the "Platform" analytic account (Task 4), sprints as milestones with dates, tasks carrying `project_task_code` (GOL issue code) and `project_task_pull_request` (PR link). No scrum module (s9). This is the human view of the Paperclip queue; it links to GOL issues by code and does not replace the board.
- `setup_ops.py --dry-run` prints a verification table: company, project, analytic account, is_aspect/is_build flags.
- The whole script is idempotent by (project name, company) and (user, company) — Review Focus #5: a second run creates zero.

- [ ] **Step 1: Write the failing tests** — the sprint project is on the farm company with the Platform analytic account; tasks accept a code and a PR link; a second `setup_ops` run creates nothing.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Dry-run against prod read-only**; expected: the plan lists every aspect project, the footpad build, the Grove Odoo sprint, and zero already-existing.
- [ ] **Step 5: Run tests, lint, commit** (`feat(scripts): Grove Odoo sprint project seed and idempotent verification table`).

---

### Task 10: Asana import mapping

This task produces no code in grove-odoo-modules beyond this table; it is the "CEO mapping" GOL-2094 is blocked on. The table is consumed by GOL-2094's migration script (`odoocker-goldberrygrove` PR #616), which folds in the new-scope filter and group mapping and then runs the mapped dry-run and `--execute`. GOL-4019 creates the target projects on prod.

**In-scope Asana projects (from the 2026-11-13 master-plan scope, confirmed by the GOL-2094 dry-run of the 2026-11-18 archive):**

| Asana project (gid) | Destination in Odoo | Company / analytic | Notes |
|---|---|---|---|
| Content calendar (`1213824483978728`) | **marketing** aspect | farm **and** nursery marketing | Open: the farm/nursery split needs Josh's rule (see confirmation ask). Default if unruled: nursery marketing. |
| Gather at the Grove Dev (`1213867393569940`) | **"Grove Odoo"** sprint | farm / Platform | Collapse the 9 Asana sections to task stages; exclude tasks that are GOL twins. |
| Odoo ERP Features and Requests (`1213903263906001`, note trailing space in Asana) | **"Grove Odoo"** sprint | farm / Platform | Exclude GOL twins. |
| Operations (Nursery & Orchard) (`1213898117964570`) | **orchard** + **nursery** aspects | farm orchard / nursery | Route by task area; orchard tasks to the farm orchard aspect, nursery tasks to the matching nursery aspect. |
| Observability — 9 open tasks (`1216085335628887`) | **"Grove Odoo"** sprint | farm / Platform | Exactly the 9 open tasks; the dry-run count matches. |

**Explicitly excluded (do not import):** AgenticOS, AgriforestryOS Dev Sprints, Josh - Personal Tasks (`1213898907106904` — gid trap: this is Personal, not Odoo ERP Features), Social Media Content Pipeline (all 21 tasks are completed, so zero bleed into Content calendar under the open-only filter).

- [ ] **Step 1: Record the table above in this plan (done).**
- [ ] **Step 2: Hand the table to GOL-2094** as the mapping input; GOL-2094 wires the new-scope filter, the destination routing and the GOL-twin exclusion into the migration script and reruns the mapped dry-run.
- [ ] **Step 3: Confirm the two open specifics with Josh** (plan-approval ask): the Content-calendar farm-vs-nursery marketing split, and the GOL-twin exclusion rule for the Dev / Odoo ERP / Observability tasks routed to the sprint.

---

### Task 11: Setup runbook and PR ready

**Files:**
- Create: `docs/ops-runbook.md`, `grove_ops/README.md`
- Modify: `grove_ops/__manifest__.py` (final `data` order: security, data, views), `conftest.py` (all Odoo test globs), `grove_ops/tests/__init__.py` (all Odoo test modules, alphabetical)

- [ ] **Step 1: Write the runbook** (no em dashes; fenced commands; no secrets): prod order — Task 2 image merges and promotes, then `setup_ops.py` (HR install, employees, groups, aspects, footpad, sprint) in the promote window (GOL-4019), then Josh assigns roles off his list (`--apply-roles`), then GOL-2094 runs the mapped Asana import.
- [ ] **Step 2: Run everything** — `ruff check . --select E,F,I --line-length 120 && ruff format --check . --line-length 120 && python3 -m pytest grove_ops/tests/test_*pure*.py scripts/tests/ -v && bash scripts/dev/run_odoo_tests.sh grove_ops`. Expected: all green; the Odoo run reports the Task 5–9 test count and 0 failures.
- [ ] **Step 3: Push, mark Ready, fill the PR template** — what Phase 2 delivers, the deviations above, the dependency on the odoocker PR from Task 2 (`odoocker-goldberrygrove#NNN`), the prod steps GOL-4019 runs, and the Asana mapping handed to GOL-2094. Ends with the attribution line.
- [ ] **Step 4: Hand off to Josh** — approves and enqueues the protected-path (CI) and image PRs; prod execution runs in the Train #6 promote window.
