# Farm Books Phase 1 (Schedule F on Odoo) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two Odoo companies (farm, nursery) keep cash-basis Schedule F books fed by one shared 2026 bank account, with every bank line tagged farm/nursery/personal, agents proposing and humans posting, and a monthly mirror that gives the nursery its own ledger.

**Architecture:** One new module `grove_schedule_f` in grove-odoo-modules carries the Schedule F account map, the Enterprise analytic plan and its posting constraint, the agent-proposal fields and the restricted "Grove Agents" group, a SQL view that cash-dates every Schedule F amount, and the month mirror. OCA modules (reconcile screen, CSV statement import, Stripe balance import, assets, financial reports) arrive through odoocker's third-party addon list. Company-specific setup (journals, provider repoint, product defaults, agent user) and the 2026 backfill are idempotent XML-RPC scripts in `scripts/`, with their planning logic pure-tested.

**Tech Stack:** Odoo 19.0 Community, Python 3.12, OCA 19.0 (`account_reconcile_oca`, `account_statement_import_sheet_file`, `account_statement_import_online_stripe`, `account_asset_management`, `account_financial_report`, `mis_builder`), Odoo `TransactionCase`, pytest for pure modules, XML-RPC scripts.

**Spec:** `2026-11-05-odoo-farm-ops-design.md` (this folder), sections 3 through 6 and 14 through 16. Task 1 copies it into grove-odoo-modules as `docs/superpowers/specs/2026-11-05-farm-ops-books-design.md` so the plan and spec travel together.

## Deviations from the spec, decided here

- **Accounts are added, the chart is not reloaded.** Odoo 19 `account.chart.template._load` on a company without posted moves unlinks every account, journal and reconcile model before loading the new template (`chart_template.py:234-246`). That would delete the farm's seeded POS/payment journals and the Stripe provider's journal. So the module creates the Schedule F income/expense accounts beside the generic chart, sets them as every journal's default, and deprecates the two generic catch-all accounts ("Product Sales", "Expenses"). Same result for the report, no destruction.
- **CSV only for bank files.** `account_statement_import_ofx` has no 19.0 branch (checked 2026-11-05); `account_statement_import_sheet_file` does. The bank's CSV export is the import format.
- **The Schedule F report is a pivot over a SQL view, not a MIS Builder template.** The view carries the cash-date rule; the pivot gives per-company, per-line, per-enterprise totals for any date range with native xlsx export. `mis_builder` is still installed for the CPA's P&L layout later; a MIS template over the same view is a follow-up, not Phase 1.
- **Owner-paid receipts are vendor bills paid through an "Owner paid" journal** whose default account is 2900 Owner contributions payable. `hr_expense` needs `hr`, which arrives in Phase 2; the entry is identical for the CPA.
- **The mirror handles expenses only.** Nursery payouts landing in the shared bank are booked farm-side to the intercompany account by a reconcile model; the nursery side is booked by the nursery's own Stripe/Square/Venmo journal reconciliation to its intercompany account. No mirror line is needed for money in.

## Global Constraints

- Repo `Goldberry-Playground/grove-odoo-modules`, branch `feature/grove-schedule-f-phase1` cut from `origin/main` (local checkouts are stale; never branch from a local HEAD). Work in a worktree at `gather-at-the-grove/grove-odoo-modules-books`. Push over HTTPS (`gh auth setup-git`); the SSH agent is broken in non-interactive shells.
- Module name `grove_schedule_f`, version `19.0.1.0.0`, license `LGPL-3`, author "Gathering at the Grove". `depends`: `account`, `analytic`, `account_reconcile_oca`, `account_statement_import_sheet_file`.
- Company ids in prod: farm 1, nursery 3, woodworking 2. Nothing in this plan touches company 2. The module is installed on all companies; setup runs only for 1 and 3.
- Every Odoo-runtime test module is listed in `grove_schedule_f/tests/__init__.py` AND in `collect_ignore_glob` in the root `conftest.py`. Pure test modules are loaded by file path with `importlib`, never through the package.
- Tests are tagged `@tagged("grove_schedule_f", "post_install", "-at_install")`.
- Lint: `ruff check . --select E,F,I --line-length 120 && ruff format --check . --line-length 120`.
- Odoo tests: `bash scripts/dev/run_odoo_tests.sh grove_schedule_f` (Task 1). Pure tests: `python3 -m pytest grove_schedule_f/tests/test_lines.py scripts/tests/ -v`.
- Agents never post: the `Grove Agents` group can read, write proposal fields, create draft bills, create manual reconcile models, and nothing else. Enforced in code, tested, not by convention.
- Personal spending posts only to 3100 Owner draw. Nursery-tagged lines on the farm bank post to real Schedule F accounts and are reclassed by the mirror.
- Sweat-equity rate `21.00` is not used in Phase 1 (no HR yet).
- No secrets in code or output. Scripts read `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_PASSWORD` from the environment like `scripts/seed_payment_journals.py`. Stripe keys and the agent API key are entered by Josh in the Odoo UI (one-time bootstrap, documented in Task 13).
- `.github/workflows/**` and `**/*stripe*` are protected paths: the PR needs Josh's approval and enqueue. Name files to avoid `stripe` in the path unless the file is about Stripe.
- Commit messages: `feat(grove_schedule_f): ...`, `ci: ...`, `feat(scripts): ...`, each ending with the attribution line in effect for this session.
- Customer-facing or CPA-facing copy: no em dashes.

## Review Focus

1. **Bank CSV imported twice for overlapping months.** Expect: no duplicate statement lines. Pinned in Task 11 (stable `unique_import_id` per row; OCA skips duplicates).
2. **Bill dated December paid half in December, half in January.** Expect: only the December half on the 2026 Schedule F, the bill on the "unpaid at year end" list. Pinned in Task 8.
3. **Line re-tagged from farm to nursery after that month's mirror ran.** Expect: the mirror run reports stale, and reverse-and-rerun fixes both companies. Pinned in Task 9.
4. **Agent user accepts a proposal itself.** Expect: AccessError, nothing posted. Pinned in Task 7.
5. **Personal purchase categorized to Supplies, or a farm purchase tagged personal.** Expect: ValidationError at post with a message naming 3100 Owner draw. Pinned in Task 5.

---

### Task 1: Module skeleton, test harness, CI wiring

**Files:**
- Create: `grove_schedule_f/__init__.py`, `grove_schedule_f/__manifest__.py`, `grove_schedule_f/models/__init__.py`, `grove_schedule_f/tests/__init__.py`
- Create: `scripts/dev/run_odoo_tests.sh`
- Create: `docs/superpowers/specs/2026-11-05-farm-ops-books-design.md` (copy of the design), `docs/superpowers/plans/2026-11-05-farm-books-phase1.md` (copy of this plan)
- Modify: `conftest.py` (root): stub `grove_schedule_f`, `grove_schedule_f.models`, `grove_schedule_f.tests` exactly as `grove_headless` is stubbed; add the Odoo test globs as later tasks create them
- Modify: `.github/workflows/ci.yml`: the `install-smoke-test` job and the pytest job
- Modify: `ownership.yml`: partition `module-grove-schedule-f`, paths `grove_schedule_f/**`, owner `engineering-ada`, review `[devops-terra]`, wave 1

**Interfaces:**
- Produces: `scripts/dev/run_odoo_tests.sh <module> [--allow-empty]`: starts `postgres:17` and `odoo:19` containers, clones the OCA repos below at branch `19.0` with `--depth 1` into `.oca/` (gitignored), mounts the repo at `/mnt/extra-addons` and `.oca/*` at `/mnt/oca/*`, runs `--init=<module> --test-enable --test-tags=/<module> --stop-after-init --log-level=info`, exits non-zero on any test failure or on "0 tests" (mirror the GOL-1941 check in ci.yml). OCA repos: `OCA/account-reconcile`, `OCA/bank-statement-import`, `OCA/account-financial-tools`, `OCA/account-financial-reporting`, `OCA/reporting-engine`, `OCA/server-ux`, `OCA/mis-builder`.

- [ ] **Step 1: Write the manifest and empty package**

`__manifest__.py` keys: name "Grove Schedule F Books", version, category "Accounting", summary "Schedule F accounts, enterprise tagging, agent proposals, cash-basis report, month mirror", depends as in Global Constraints, `data: []` for now, `installable: True`, `application: False`, `auto_install: False`. `__init__.py` imports `models`. `tests/__init__.py` holds the discovery comment from `grove_support/tests/__init__.py`.

- [ ] **Step 2: Write `scripts/dev/run_odoo_tests.sh`**

One argument, the module. Fail with usage text otherwise. Reuse the container start/poll/exit-code sequence from `.github/workflows/ci.yml` (install-smoke-test), adding the OCA clone and mount. Add `.oca/` to `.gitignore`.

- [ ] **Step 3: Run the harness on the empty module**

Run: `bash scripts/dev/run_odoo_tests.sh grove_schedule_f`
Expected: exit 0 with "Modules loaded." in the log and the 0-tests guard passing because the harness treats an empty suite as success only when `--allow-empty` is passed; pass it here.

- [ ] **Step 4: Wire CI**

In `ci.yml` `install-smoke-test`: add a step before the docker run that clones the same seven OCA repos (same branch, depth 1) into `oca/` in the workspace; mount `oca/` at `/mnt/oca`; extend `--addons-path` with `/mnt/oca/account-reconcile,/mnt/oca/bank-statement-import,...` (one path per repo); add `grove_schedule_f` to `--init` and `/grove_schedule_f` to `--test-tags`. In the `pytest` job add `grove_schedule_f/tests/` to the paths. Keep the existing jobs' names unchanged (they are required contexts).

- [ ] **Step 5: Copy spec and plan, add ownership partition, commit**

```bash
git add grove_schedule_f conftest.py .github/workflows/ci.yml ownership.yml scripts/dev docs/superpowers .gitignore
git commit -m "feat(grove_schedule_f): module skeleton, OCA-aware test harness, CI wiring"
```

Open the PR as a Draft now (`gh pr create --draft`), titled "grove_schedule_f: Phase 1 books (Schedule F, enterprise tags, agent proposals, mirror)". Every later task pushes to it.

---

### Task 2: OCA modules in the odoocker image

**Files (repo `Goldberry-Playground/odoocker-goldberrygrove`, branch `feat/oca-books-addons` from `origin/main`):**
- Modify: `odoo/third-party-addons.txt`

**Interfaces:**
- Produces: the production image contains `account_statement_base`, `account_reconcile_oca`, `account_statement_import_base`, `account_statement_import_file`, `account_statement_import_sheet_file`, `account_statement_import_online`, `account_statement_import_online_stripe`, `account_asset_management`, `account_financial_report`, `date_range`, `report_xlsx`, `report_xlsx_helper`, `mis_builder`, `mis_builder_budget`.

- [ ] **Step 1: Add the repositories**

One `public` line per repo in the file's documented format, listing each module with `true`:
`OCA/account-reconcile` (account_statement_base, account_reconcile_oca), `OCA/bank-statement-import` (the five import modules), `OCA/account-financial-tools` (account_asset_management), `OCA/account-financial-reporting` (account_financial_report), `OCA/server-ux` (date_range), `OCA/reporting-engine` (report_xlsx, report_xlsx_helper), `OCA/mis-builder` (mis_builder, mis_builder_budget). Python deps (`xlsxwriter`, `xlrd`, `python-dateutil`) ship in the official `odoo:19` image; add nothing to `odoo/requirements.txt`.

- [ ] **Step 2: Verify the clone step locally**

Run from the odoocker checkout: `ODOO_TAG=19.0 THIRD_PARTY_ADDONS=/tmp/tpa ENTERPRISE_ADDONS=/tmp/ea bash odoo/clone-addons.sh` (from a scratch copy of `odoo/`; the script expects `third-party-addons.txt` in cwd).
Expected: no `ERROR:` lines; `ls /tmp/tpa` shows all fourteen module directories.

- [ ] **Step 3: Commit and open a Draft PR**

```bash
git commit -am "feat(odoo): add OCA accounting addons for the Schedule F books (Phase 1)"
```

CI's `Validate third-party addon branches` and `Docker — Odoo Image` must be green. Josh approves and enqueues. The image rebuild rides Train 5; prod picks it up at promote. The module in Task 1 cannot install on prod before this merges.

---

### Task 3: Schedule F line map (pure)

**Files:**
- Create: `grove_schedule_f/schedule_f_lines.py` (no `odoo` import)
- Test: `grove_schedule_f/tests/test_lines.py` (pure; loaded by path)

**Interfaces:**
- Produces:
  - `ACCOUNTS: list[dict]` with keys `code`, `name`, `line` (str or None), `account_type` (Odoo `account.account.account_type` value), `kind` in {"income", "expense", "balance"}.
  - `LINE_ORDER: list[str]` = Schedule F lines in form order.
  - `sort_key(line: str) -> tuple[int, str]`, e.g. `"21b" -> (21, "b")`, `None -> (999, "")`.
  - `accounts_for_line(line: str) -> list[dict]`.
  - `account_by_code(code: str) -> dict` (KeyError if unknown).

- [ ] **Step 1: Write the failing tests**

```python
def test_every_schedule_f_line_has_at_least_one_account():
    for line in mod.LINE_ORDER:
        assert mod.accounts_for_line(line), line


def test_codes_are_unique_and_four_digits():
    codes = [a["code"] for a in mod.ACCOUNTS]
    assert len(codes) == len(set(codes))
    assert all(len(c) == 4 and c.isdigit() for c in codes)


def test_income_lines_use_income_types_and_expenses_use_expense_types():
    for a in mod.ACCOUNTS:
        if a["kind"] == "income":
            assert a["account_type"] in {"income", "income_other"}
        if a["kind"] == "expense":
            assert a["account_type"] in {"expense", "expense_direct_cost", "expense_depreciation"}
        if a["kind"] == "balance":
            assert a["line"] is None


def test_sort_key_orders_sub_lines():
    assert mod.sort_key("21b") == (21, "b")
    assert sorted(["24b", "8", "1a", "24a"], key=mod.sort_key) == ["1a", "8", "24a", "24b"]


def test_specific_codes():
    assert mod.account_by_code("4020")["line"] == "2"
    assert mod.account_by_code("6280")["line"] == "28"
    assert mod.account_by_code("6321")["line"] == "32"
    assert mod.account_by_code("3100")["kind"] == "balance"
    assert mod.account_by_code("1350")["account_type"] == "asset_current"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest grove_schedule_f/tests/test_lines.py -v`
Expected: FAIL, module not found.

- [ ] **Step 3: Write the map**

Income (kind income, type `income`): 4010 "Sales of resale items" 1a; 4020 "Sales of raised produce, plants and seeds" 2; 4030 "Cooperative distributions" 3a; 4040 "Agricultural program payments" 4a; 4050 "CCC loans reported" 5a; 4055 "CCC loans forfeited" 5b; 4060 "Crop insurance and disaster payments" 6a; 4070 "Custom hire income" 7; 4080 "Other farm income" 8.
Expense: 4015 "Cost of resale items" 1b (`expense_direct_cost`); 6100 Car and truck 10; 6110 Chemicals 11; 6120 Conservation 12; 6130 Custom hire 13; 6140 Depreciation and section 179 14 (`expense_depreciation`); 6150 Employee benefit programs 15; 6160 Feed 16; 6170 Fertilizers and lime 17; 6180 Freight and trucking 18; 6190 Gasoline, fuel and oil 19; 6200 Insurance 20; 6210 Interest, mortgage 21a; 6215 Interest, other 21b; 6220 Labor hired 22; 6230 Pension and profit sharing 23; 6240 Rent, machinery and equipment 24a; 6245 Rent, land and other 24b; 6250 Repairs and maintenance 25; 6260 Seeds and plants 26; 6270 Storage and warehousing 27; 6280 Supplies 28; 6290 Taxes 29; 6300 Utilities 30; 6310 Veterinary, breeding and medicine 31; 6320 Other expenses 32; 6321 Payment processing fees 32.
Balance (kind balance, line None): 1350 "Intercompany" `asset_current`; 1500 "Farm equipment and improvements" `asset_fixed`; 1590 "Accumulated depreciation" `asset_fixed`; 2900 "Owner contributions payable" `liability_current`; 3100 "Owner draw" `equity`.
Source of the line labels: harknett/schedule-f-books `src/lib/schedule-f.ts` (MIT), checked against the 2025 IRS Schedule F instructions by the implementer; note any renumbering in the module README.

- [ ] **Step 4: Run to verify it passes, lint, commit**

```bash
git add grove_schedule_f/schedule_f_lines.py grove_schedule_f/tests/test_lines.py
git commit -m "feat(grove_schedule_f): Schedule F account and line map"
```

---

### Task 4: Accounts, tags, line field, company setup method

**Files:**
- Create: `grove_schedule_f/models/account_account.py`, `grove_schedule_f/models/res_company.py`
- Create: `grove_schedule_f/views/account_views.xml` (line field on the account form and list, group-by on list)
- Modify: `grove_schedule_f/models/__init__.py`, `__manifest__.py` (`data`)
- Test: `grove_schedule_f/tests/test_accounts.py`

**Interfaces:**
- Produces:
  - `account.account.grove_schedule_f_line = fields.Char(index=True)`.
  - `res.company.grove_schedule_f_setup() -> dict` with keys `accounts_created`, `accounts_existing`, `tags_created`, `deprecated` (codes). Idempotent. Callable over XML-RPC.
  - `res.company._grove_account(code: str) -> account.account` (the company's account with that code; raises `UserError` naming the code if missing).
  - Tags `account.account.tag` named `Schedule F {line}` with `applicability="accounts"`, one per line, shared across companies.

- [ ] **Step 1: Write the failing tests**

```python
class TestAccounts(TransactionCase):
    def test_setup_creates_every_mapped_account_once(self):
        company = self.env["res.company"].create({"name": "Setup Co"})
        first = company.grove_schedule_f_setup()
        self.assertEqual(first["accounts_created"], len(schedule_f_lines.ACCOUNTS))
        second = company.grove_schedule_f_setup()
        self.assertEqual(second["accounts_created"], 0)
        self.assertEqual(second["accounts_existing"], len(schedule_f_lines.ACCOUNTS))

    def test_mapped_accounts_carry_line_and_tag(self):
        company = ...
        company.grove_schedule_f_setup()
        acc = company._grove_account("6280")
        self.assertEqual(acc.grove_schedule_f_line, "28")
        self.assertIn("Schedule F 28", acc.tag_ids.mapped("name"))
        self.assertFalse(company._grove_account("3100").grove_schedule_f_line)

    def test_journal_defaults_point_at_schedule_f(self):
        # a sale journal defaults to 4020, a purchase journal to 6320
        ...

    def test_generic_catch_alls_are_deprecated(self):
        # an account named "Expenses" / "Product Sales" from the generic chart ends up deprecated=True
        ...
```

For the journal and deprecation tests, load `generic_coa` on the test company first (`self.env["account.chart.template"].try_loading("generic_coa", company, install_demo=False)`), then run setup.

- [ ] **Step 2: Run to verify they fail**

Run: `bash scripts/dev/run_odoo_tests.sh grove_schedule_f`
Expected: FAIL, `grove_schedule_f_setup` not defined.

- [ ] **Step 3: Implement**

`grove_schedule_f_setup` per company: for each `ACCOUNTS` entry create the account with `company_ids=[company]`, `code`, `name`, `account_type`, `grove_schedule_f_line`, and link the line tag (create tags on demand); if the code exists for the company, update name/line/tag only. Then set `default_account_id` on the company's sale journals to 4020 and purchase journals to 6320, and `deprecated=True` on generic accounts whose name is exactly "Product Sales" or "Expenses" and that are not a journal default or a company property. Return the summary dict.

- [ ] **Step 4: Run tests, lint, commit**

```bash
git commit -am "feat(grove_schedule_f): Schedule F accounts, tags and company setup"
```

---

### Task 5: Enterprise plan and the posting constraint

**Files:**
- Create: `grove_schedule_f/models/account_journal.py`, `grove_schedule_f/models/account_move_line.py`
- Modify: `res_company.py` (setup creates the plan), `views/account_views.xml` (journal flags), `__init__.py`
- Test: `grove_schedule_f/tests/test_enterprise.py`

**Interfaces:**
- Produces:
  - `account.journal.grove_enterprise_required = fields.Boolean()`; `account.journal.grove_is_mirror = fields.Boolean()`.
  - `res.company._grove_ensure_enterprise_plan() -> account.analytic.plan` named "Enterprise" on the company, with analytic accounts "Farm", "Nursery", "Personal". Called by `grove_schedule_f_setup` only when `self.grove_owns_shared_bank` is True (new Boolean on `res.company`, default False; the setup script sets it on the farm).
  - `account.move.line.grove_enterprise = fields.Selection([("farm","Farm"),("nursery","Nursery"),("personal","Personal")], compute="_compute_grove_enterprise", store=True)` derived from `analytic_distribution` keys that belong to the Enterprise plan (by analytic account name, case-insensitive).
  - `ENTERPRISE_EXEMPT_ACCOUNT_TYPES = {"asset_cash", "liability_credit_card"}` plus the journal's `suspense_account_id`, `default_account_id`, and the company's outstanding receipts/payments and transfer accounts.
  - Constraint `_check_grove_enterprise` on `account.move.line` (`account_id`, `analytic_distribution`, `journal_id`): in a journal with `grove_enterprise_required`, every non-exempt line needs a 100% Enterprise distribution; `personal` requires account code 3100; account 3100 requires `personal`. `ValidationError` messages: "Line {name}: pick an enterprise (Farm, Nursery or Personal) before posting." and "Personal spending posts only to 3100 Owner draw."

- [ ] **Step 1: Write the failing tests**

```python
def test_setup_creates_plan_with_three_accounts(self):
    company.grove_owns_shared_bank = True
    company.grove_schedule_f_setup()
    plan = self.env["account.analytic.plan"].search([("name", "=", "Enterprise"), ("company_id", "=", company.id)])
    self.assertEqual(sorted(plan.account_ids.mapped("name")), ["Farm", "Nursery", "Personal"])


def test_bank_counterpart_without_enterprise_is_rejected(self):
    # bank journal with grove_enterprise_required; a misc-style entry Dr 6280 / Cr bank default account
    with self.assertRaisesRegex(ValidationError, "pick an enterprise"):
        self._post_entry(journal=bank, debit_account="6280", distribution=None)


def test_bank_counterpart_with_enterprise_posts_and_stores_it(self):
    move = self._post_entry(journal=bank, debit_account="6280", distribution={nursery.id: 100})
    self.assertEqual(move.line_ids.filtered(lambda l: l.account_id.code == "6280").grove_enterprise, "nursery")


def test_suspense_and_liquidity_lines_are_exempt(self):
    # a plain statement line import (bank + suspense) succeeds with no distribution
    ...


def test_personal_must_go_to_owner_draw(self):
    with self.assertRaisesRegex(ValidationError, "3100"):
        self._post_entry(journal=bank, debit_account="6280", distribution={personal.id: 100})
    with self.assertRaisesRegex(ValidationError, "3100"):
        self._post_entry(journal=bank, debit_account="3100", distribution={farm.id: 100})


def test_unflagged_journal_is_untouched(self):
    self._post_entry(journal=misc, debit_account="6280", distribution=None)  # no error
```

- [ ] **Step 2: Run to verify they fail**

Expected: FAIL on missing fields.

- [ ] **Step 3: Implement**

Plan creation uses `account.analytic.plan` with `company_id` and `default_applicability="optional"`; the constraint, not applicability rules, does the enforcing (applicability rules cannot target one journal). The compute reads `analytic_distribution` keys (ids, possibly comma-joined) and maps to the plan's accounts.

- [ ] **Step 4: Run tests, lint, commit**

```bash
git commit -am "feat(grove_schedule_f): Enterprise analytic plan and mandatory tagging on the shared bank journal"
```

---

### Task 6: Proposal fields and the Grove Agents group

**Files:**
- Create: `grove_schedule_f/models/account_bank_statement_line.py`, `grove_schedule_f/models/account_move.py`, `grove_schedule_f/models/account_reconcile_model.py`
- Create: `grove_schedule_f/security/groups.xml`, `grove_schedule_f/security/ir.model.access.csv`
- Modify: `__manifest__.py` (`data`), `__init__.py`
- Test: `grove_schedule_f/tests/test_agent_group.py`

**Interfaces:**
- Produces:
  - Group xmlid `grove_schedule_f.group_grove_agent`, name "Grove Agents", category Accounting, implies `base.group_user` only.
  - Statement line fields: `grove_proposed_account_id` (M2o account.account), `grove_proposed_enterprise_id` (M2o account.analytic.account, domain plan name Enterprise), `grove_proposed_model_id` (M2o account.reconcile.model), `grove_proposal_note` (Text), `grove_proposal_uncertain` (Boolean), `grove_proposal_state` (Selection none/proposed/accepted/rejected, default none). Writing any proposed field sets state `proposed` unless state is accepted.
  - `AGENT_WRITABLE_STATEMENT_FIELDS = frozenset({...the six fields above, "message_main_attachment_id"})`.
  - Overrides for a user in the group (and not `env.su`): `account.bank.statement.line.write` raises `AccessError` if vals has other keys; `account.move.create` forces `state` draft and `write` raises if the move is posted or vals touches `state`; `account.move.line.write` raises if its move is posted; `account.move.action_post` raises; `account.bank.statement.line.reconcile_bank_line` raises; `account.reconcile.model.create/write` raise if `trigger` would become `auto_reconcile`.
  - ACL rows for the group: read on account.account, account.journal, account.move, account.move.line, account.bank.statement, account.bank.statement.line, account.reconcile.model, account.reconcile.model.line, account.analytic.account, account.analytic.plan, res.partner; write on account.bank.statement.line; create+write on account.move, account.move.line, account.reconcile.model, account.reconcile.model.line; create on mail.message via chatter (standard).

- [ ] **Step 1: Write the failing tests**

```python
def setUp: create user "agent" in group_grove_agent only; a bank statement line; a draft and a posted move.

def test_agent_can_write_a_proposal_and_state_flips(self):
    line.with_user(agent).write({"grove_proposed_account_id": acc_6280.id, "grove_proposal_note": "Tractor Supply"})
    self.assertEqual(line.grove_proposal_state, "proposed")

def test_agent_cannot_change_amount_or_date(self):
    with self.assertRaises(AccessError): line.with_user(agent).write({"amount": 1.0})

def test_agent_cannot_post_or_reconcile(self):
    with self.assertRaises(AccessError): draft_move.with_user(agent).action_post()
    with self.assertRaises(AccessError): posted_move.with_user(agent).write({"ref": "x"})
    with self.assertRaises(AccessError): posted_move.line_ids[0].with_user(agent).write({"account_id": acc_6280.id})
    with self.assertRaises(AccessError): line.with_user(agent).reconcile_bank_line()

def test_agent_can_create_a_draft_bill(self):
    bill = self.env["account.move"].with_user(agent).create({"move_type": "in_invoice", "partner_id": p.id, "invoice_line_ids": [...]})
    self.assertEqual(bill.state, "draft")

def test_agent_models_stay_manual(self):
    with self.assertRaises(AccessError):
        self.env["account.reconcile.model"].with_user(agent).create({"name": "x", "trigger": "auto_reconcile", ...})
    model = ...create({"name": "x"})  # manual by default: ok
    with self.assertRaises(AccessError): model.with_user(agent).write({"trigger": "auto_reconcile"})

def test_owner_is_unaffected(self):
    line.write({"amount": 2.0}); draft_move.action_post()  # no error as admin
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement**

Helper `_grove_is_agent(env) -> bool` in `account_bank_statement_line.py`, reused by the other overrides. Group check uses `env.user.has_group`.

- [ ] **Step 4: Run tests, lint, commit**

```bash
git commit -am "feat(grove_schedule_f): proposal fields and the propose-only Grove Agents group"
```

---

### Task 7: Accept and reject proposals, the exceptions list

**Files:**
- Modify: `grove_schedule_f/models/account_bank_statement_line.py`
- Create: `grove_schedule_f/views/statement_line_views.xml` (action "Bank lines to review", list view with Accept/Reject buttons, filters), `grove_schedule_f/data/config_parameter.xml` (`grove_schedule_f.review_threshold` = `500`)
- Create: `grove_schedule_f/views/menus.xml` (top menu "Schedule F" under Accounting with items added by Tasks 7, 8, 9)
- Test: `grove_schedule_f/tests/test_proposals.py`

**Interfaces:**
- Produces:
  - `action_grove_accept_proposal()`: for a line in state `proposed` with a proposed account (and a proposed enterprise when the journal requires one), reconciles the line to that account with `analytic_distribution = {str(enterprise.id): 100}` through `account_reconcile_oca` (build the default `reconcile_data_info`, replace the suspense entry's account and distribution, call `reconcile_bank_line()`), sets state `accepted`, posts a chatter note "Accepted: {account} / {enterprise}". Raises `AccessError` for an agent, `UserError` if nothing is proposed.
  - `action_grove_reject_proposal()`: state `rejected`, chatter note, no other change.
  - `grove_needs_review = fields.Boolean(compute, search="_search_grove_needs_review")`: true when unreconciled and (uncertain, or no proposed model, or `abs(amount) >= review_threshold`).
  - Action `grove_schedule_f.action_bank_lines_to_review`: domain `[("is_reconciled","=",False)]`, default filter needs-review, context grouping by journal.

- [ ] **Step 1: Write the failing tests**

```python
def test_accept_reconciles_to_proposed_account_with_enterprise(self):
    line = self._import_line(amount=-42.10, label="TRACTOR SUPPLY")
    line.write({"grove_proposed_account_id": a6280.id, "grove_proposed_enterprise_id": farm_ent.id})
    line.action_grove_accept_proposal()
    self.assertTrue(line.is_reconciled)
    cp = line.move_id.line_ids.filtered(lambda l: l.account_id == a6280)
    self.assertEqual(cp.debit, 42.10)
    self.assertEqual(cp.grove_enterprise, "farm")
    self.assertEqual(line.grove_proposal_state, "accepted")


def test_accept_without_proposal_raises(self): ...
def test_agent_cannot_accept(self):
    with self.assertRaises(AccessError):
        line.with_user(agent).action_grove_accept_proposal()


def test_reject_leaves_line_unreconciled(self): ...
def test_needs_review_rules(self):
    # uncertain -> True; model set and amount 12 -> False; amount -600 -> True; no model -> True
    ...


def test_needs_review_is_searchable(self):
    self.assertIn(big_line, self.env["account.bank.statement.line"].search([("grove_needs_review", "=", True)]))
```

`_import_line` creates an `account.bank.statement.line` on a bank journal with `grove_enterprise_required=True` (the statement move posts with bank + suspense, exempt).

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement**

Read `account_reconcile_oca/models/account_bank_statement_line.py` (`_default_reconcile_data`, `_reconcile_bank_line_edit`, `_reconcile_move_line_vals`) for the data shape; the test pins the outcome, not the shape.

- [ ] **Step 4: Run tests, lint, commit**

```bash
git commit -am "feat(grove_schedule_f): accept/reject proposals and the bank lines to review list"
```

---

### Task 8: Cash-basis Schedule F view, pivot, year-end lists

**Files:**
- Create: `grove_schedule_f/models/schedule_f_cashline.py`
- Create: `grove_schedule_f/views/schedule_f_views.xml` (pivot + list for the view model; actions "Unpaid at year end", "Uncategorized bank lines"; menu items)
- Modify: `security/ir.model.access.csv` (read for `account.group_account_readonly` and `account.group_account_invoice`), `__init__.py`, `menus.xml`
- Test: `grove_schedule_f/tests/test_cashline.py`

**Interfaces:**
- Produces model `grove.schedulef.cashline` (`_auto = False`, SQL view) with fields: `company_id`, `account_id`, `schedule_f_line` (Char), `enterprise` (Char, from `grove_enterprise`), `kind` ("income"/"expense"), `cash_date` (Date), `amount` (Float; income credits positive, expense debits positive), `source` ("bank"/"mirror"/"document"), `move_id`, `move_line_id`.
- Rows:
  - `bank`: posted lines in journals of type `bank` or `cash` whose account has a line; `cash_date = move.date`.
  - `mirror`: posted lines in journals with `grove_is_mirror`; `cash_date = move.date`.
  - `document`: lines with a Schedule F account on posted moves of type `in_invoice`, `in_refund`, `out_invoice`, `out_refund`; one row per `account.partial.reconcile` touching the document's receivable/payable line; `cash_date` = the partial's other move's date; `amount = line_amount * partial.amount / abs(move.amount_total_signed)` (the one algorithm block the implementer needs; write it as SQL joining `account_partial_reconcile` on `debit_move_id`/`credit_move_id`).
- Actions: "Unpaid at year end" = `account.move` posted documents with `amount_residual != 0` and `invoice_date <= context year end`; "Uncategorized bank lines" = statement lines `is_reconciled = False`.

- [ ] **Step 1: Write the failing tests**

```python
def test_bank_line_appears_at_bank_date(self):
    # accepted proposal on a bank line dated 2026-03-04 to 6280 -> one row, cash_date 2026-03-04, amount 42.10, kind expense, enterprise farm
def test_bill_paid_in_two_parts_splits_by_payment_date(self):
    # bill 2026-12-10 for 1000 on 6240; payments 400 on 2026-12-20 and 600 on 2027-01-05
    rows = search([("move_id","=",bill.id)])
    self.assertEqual(sorted((r.cash_date.isoformat(), r.amount) for r in rows), [("2026-12-20", 400.0), ("2027-01-05", 600.0)])
def test_unpaid_bill_has_no_rows_and_is_listed(self):
    # bill with no payment -> zero rows; appears in the Unpaid at year end action's domain
def test_refund_flips_sign(self):
    # customer refund of 50 on 4020 paid -> amount -50 kind income
def test_income_sign_is_positive_for_credits(self):
    # paid invoice 200 on 4020 -> amount 200
def test_sum_by_line_for_year(self):
    # read_group by schedule_f_line over 2026 matches the scenario totals
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement the view, the pivot (rows `schedule_f_line`, columns `company_id`, measure `amount`), the list, the two actions**
- [ ] **Step 4: Run tests, lint, commit**

```bash
git commit -am "feat(grove_schedule_f): cash-basis Schedule F view with pivot and year-end lists"
```

---

### Task 9: Month mirror

**Files:**
- Create: `grove_schedule_f/models/mirror_run.py`, `grove_schedule_f/views/mirror_views.xml`
- Modify: `res_company.py` (setup creates the MIRR journal per company), `ir.model.access.csv`, `__init__.py`, `menus.xml`
- Test: `grove_schedule_f/tests/test_mirror.py`

**Interfaces:**
- Produces model `grove.mirror.run` with fields `month` (Date, first of month), `source_company_id`, `target_company_id`, `state` (draft/posted/reversed), `source_move_id`, `target_move_id`, `summary` (Json: list of `{code, amount}`), `is_stale` (Boolean, stored, set by `action_check`). Methods:
  - `@api.model grove_run_month(month: date, source_company, target_company) -> grove.mirror.run`: raises `UserError` if a non-reversed run exists for (month, source); otherwise computes and posts both moves, returns the run.
  - `_grove_collect(month, source_company) -> dict[str, float]`: sums `balance` of posted source-company move lines with `date` in month, `grove_enterprise == "nursery"`, account line set, journal not mirror, grouped by account code.
  - `action_reverse()`: `_reverse_moves(cancel=True)` on both moves, state reversed.
  - `action_check()`: `is_stale = (_grove_collect(...) != summary)`.
  - Source move (journal MIRR of source, date = last day of month, ref `MIRROR {YYYY-MM} nursery`): one line per code crediting (or debiting when negative) that account with distribution Nursery 100, plus one intercompany 1350 line for the total. Target move (journal MIRR of target, same date and ref): one line per code debiting the target's account with the same code, one intercompany 1350 line crediting the total.
  - Journals: `grove_schedule_f_setup` creates journal code `MIRR`, name "Intercompany mirror", type `general`, `grove_is_mirror=True`.

- [ ] **Step 1: Write the failing tests**

```python
def test_run_mirrors_nursery_tagged_expenses_and_zeroes_the_farm(self):
    # farm bank lines in 2026-03: 6280 nursery 100, 6280 nursery 25, 6260 nursery 40, 6280 farm 10
    run = Run.grove_run_month(date(2026,3,1), farm, nursery)
    self.assertEqual(run.summary, [{"code":"6260","amount":40.0},{"code":"6280","amount":125.0}])
    self.assertEqual(self._farm_cash_total("28", enterprise="nursery", year=2026), 0.0)
    self.assertEqual(self._nursery_cash_total("28", year=2026), 125.0)
    self.assertEqual(run.source_move_id.line_ids.filtered(lambda l: l.account_id.code=="1350").debit, 165.0)
def test_second_run_same_month_raises(self): ...
def test_reverse_then_rerun(self):
    run.action_reverse(); self.assertEqual(run.state, "reversed")
    Run.grove_run_month(...)  # no error; farm nursery total back to 0
def test_retag_after_run_marks_stale(self):
    # accept another nursery line in March after the run; run.action_check(); assertTrue(run.is_stale)
def test_farm_tagged_and_personal_lines_are_ignored(self): ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement**, including a form view with Run/Check/Reverse buttons and a list of runs; a server action "Run mirror for month" taking the month from a wizard-free default of last month.
- [ ] **Step 4: Run tests, lint, commit**

```bash
git commit -am "feat(grove_schedule_f): month mirror of nursery-tagged expenses into the nursery company"
```

---

### Task 10: Books setup script

**Files:**
- Create: `scripts/setup_books.py` (XML-RPC, idempotent, pattern of `scripts/setup_pos.py`)
- Test: `scripts/tests/test_setup_books.py` (pure; loads the script by path like `test_setup_livechat_support.py`)

**Interfaces:**
- Produces pure functions in the script: `journal_specs(company_key: str) -> list[dict]` (`code`, `name`, `type`, flags), `category_income_account(category_name: str) -> str` (code), `provider_repoint_plan(providers: list[dict], nursery_journals: dict) -> list[dict]`.
- Journals, farm: `BNK1` renamed "Shared bank (2026)" with `grove_enterprise_required=True`; `STRP` "Stripe (farm)" bank; `OWNR` "Owner paid" cash with default account 2900; `MIRR` via setup. Nursery: `INV` sale, `BILL` purchase, `MISC` general, `STRP` "Stripe (nursery)" bank, `SQR` "Square clearing" bank, `VENM` "Venmo clearing" cash, `CSH` "Cash" cash, `MIRR` via setup.
- Product categories: names containing "seed" or "plant" or "tree" or any nursery catalog category → 4020; names containing "merch", "shirt", "sticker", "pot" → 4010; everything else 4020. Written to `property_account_income_categ_id`.
- Payment provider 24 "Stripe (Nursery)" `journal_id` → nursery `STRP`. Provider 18 left disabled.
- `res.company` 1: `grove_owns_shared_bank=True`. Then `grove_schedule_f_setup()` on 1 and 3.
- User "Grove Agents" (login `agents@goldberrygrove.farm`, groups: Grove Agents only, companies 1 and 3, no password; Josh mints the API key in the UI).
- `ir.config_parameter` `grove_schedule_f.review_threshold` left at 500.
- Flags: `--dry-run` prints the plan; `--reset-test-entries` lists posted moves in both companies and, only with the flag, drafts and deletes the ones whose ref matches `--reset-ref-pattern`.

- [ ] **Step 1: Write the failing pure tests**

```python
def test_farm_journals_include_shared_bank_flag_and_owner_paid(self): ...
def test_nursery_journals_cover_every_channel(self):
    codes = {j["code"] for j in mod.journal_specs("nursery")}
    self.assertEqual(codes, {"INV", "BILL", "MISC", "STRP", "SQR", "VENM", "CSH"})


def test_category_mapping(self):
    self.assertEqual(mod.category_income_account("Chestnut Trees"), "4020")
    self.assertEqual(mod.category_income_account("T-shirts"), "4010")


def test_provider_repoint_only_touches_enabled_nursery_stripe(self): ...
```

- [ ] **Step 2: Run to verify they fail**: `python3 -m pytest scripts/tests/test_setup_books.py -v`
- [ ] **Step 3: Implement**; `main()` prints a verification table (company, journal code, default account, flags) at the end.
- [ ] **Step 4: Dry-run against prod read-only**

Run with `--dry-run` and the prod env vars from Josh's terminal (the script only reads in dry-run). Expected: the plan lists 7 existing farm journals, 0 nursery journals, provider 24 repoint, and the 2 test moves.

- [ ] **Step 5: Run pure tests, lint, commit**

```bash
git commit -am "feat(scripts): idempotent books setup for farm and nursery companies"
```

---

### Task 11: Bank CSV backfill import

**Files:**
- Create: `scripts/import_bank_csv.py`
- Test: `scripts/tests/test_import_bank_csv.py`

**Interfaces:**
- Produces pure functions: `split_by_month(rows: list[dict], date_col: str, date_fmt: str) -> dict[str, list[dict]]` (key `YYYY-MM`, order preserved); `unique_import_id(row: dict, index: int, id_col: str | None) -> str` (the bank's id when present, else `sha1(date|amount|description|index)`); `mapping_vals(args) -> dict` for `account.statement.import.sheet.mapping` from CLI column names (`--date-col`, `--amount-col` or `--debit-col`/`--credit-col`, `--desc-col`, `--id-col`, `--date-fmt`).
- Script: creates or reuses the mapping named "Shared bank CSV", writes each month to a temp CSV adding a `unique_import_id` column mapped to `transaction_id_column`, creates `account.statement.import` with `statement_file` (base64), `statement_filename`, `sheet_mapping_id`, journal from `--journal-code` (default `BNK1`, company 1), calls `import_file_button`, prints lines created per month. `--from 2026-01 --to 2026-10` selects months.

- [ ] **Step 1: Write the failing pure tests**

```python
def test_split_by_month_keeps_order_and_groups(self): ...
def test_unique_import_id_prefers_bank_id_and_is_stable(self):
    self.assertEqual(mod.unique_import_id(row_with_id, 3, "id"), "TX123")
    self.assertEqual(mod.unique_import_id(row, 3, None), mod.unique_import_id(row, 3, None))
    self.assertNotEqual(mod.unique_import_id(row, 3, None), mod.unique_import_id(row, 4, None))


def test_mapping_vals_single_amount_vs_debit_credit(self): ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Prove the duplicate guard on the test harness**

Write an Odoo test `grove_schedule_f/tests/test_import_dedupe.py` that imports a two-row CSV twice through the wizard with the mapping from `mapping_vals` and asserts two statement lines exist, not four.

- [ ] **Step 5: Run tests, lint, commit**

```bash
git commit -am "feat(scripts): monthly bank CSV backfill import with stable import ids"
```

---

### Task 12: Square-era income summaries, rent, reconcile model seeds

**Files:**
- Create: `scripts/import_square_summary.py`, `scripts/book_rent.py`, `scripts/seed_reconcile_models.py`
- Test: `scripts/tests/test_import_square_summary.py`, `scripts/tests/test_book_rent.py`, `scripts/tests/test_seed_reconcile_models.py`

**Interfaces:**
- `book_rent.py --period 2026 --amount 0.00` (amount from Josh; the script refuses 0): pure `rent_documents(period: str, amount: float) -> dict` returns four val dicts: farm customer invoice to the nursery's partner on 4080 "Other farm income" (ref `RENT {period}`; the CPA may move it to another line), nursery vendor bill from the farm's partner on 6245 (line 24b), and one settlement move per company in `MISC` dated the invoice date: farm Dr 1350 / Cr receivable, nursery Dr payable / Cr 1350. The script posts all four and reconciles each settlement line with its document's receivable or payable line. Idempotent by ref.
- `import_square_summary.py`: input CSV columns `month,gross,fees` (dollars). Pure `summary_entry(month: str, gross: float, fees: float) -> dict` returns move vals for the nursery: journal `SQR`, date = last day of month, ref `SQUARE {month}`, lines: Dr `SQR` journal default account `gross - fees`, Dr 6321 `fees`, Cr 4020 `gross`. Idempotent by ref.
- `seed_reconcile_models.py`: pure `MODEL_SPECS: list[dict]` and `model_vals(spec, company_accounts) -> dict`. Specs (all `trigger="manual"`, `match_label="contains"`): farm company: "Stripe payout (nursery)" label `STRIPE` amount received → 1350 with Nursery distribution, `grove_proposal_uncertain` note "two Stripe accounts share this descriptor; match the amount to a payout in each Stripe journal"; "Square deposit" label `SQUARE` → 1350 Nursery; "Venmo cashout" label `VENMO` → 1350 Nursery; "Owner draw" label `TRANSFER TO` → 3100 Personal. Nursery company: "Stripe payout to shared bank" (Stripe journal, label `payout`) → 1350; "Square transfer to bank" → 1350.

- [ ] **Step 1: Write the failing pure tests**

```python
def test_summary_entry_balances_and_dates_month_end(self):
    vals = mod.summary_entry("2026-04", 1234.50, 36.20)
    debits = sum(l["debit"] for l in lines)
    credits = ...
    self.assertAlmostEqual(debits, credits)
    self.assertEqual(vals["date"], "2026-04-30")


def test_rent_documents_balance_and_settle_through_1350(self):
    d = mod.rent_documents("2026", 1200.0)
    self.assertEqual(d["farm_invoice"]["invoice_line_ids"][0][2]["account_code"], "4080")
    self.assertEqual(d["nursery_bill"]["invoice_line_ids"][0][2]["account_code"], "6245")
    for key in ("farm_settlement", "nursery_settlement"):
        lines = [l[2] for l in d[key]["line_ids"]]
        self.assertAlmostEqual(sum(l["debit"] for l in lines), sum(l["credit"] for l in lines))
        self.assertIn("1350", {l["account_code"] for l in lines})
    with self.assertRaises(ValueError):
        mod.rent_documents("2026", 0)


def test_model_specs_name_real_accounts_and_stay_manual(self):
    for spec in mod.MODEL_SPECS:
        self.assertEqual(spec["trigger"], "manual")
        self.assertIn(spec["account_code"], KNOWN_CODES)
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement the three scripts (XML-RPC, idempotent by `ref` / by model name + company)**
- [ ] **Step 4: Run tests, lint, commit**

```bash
git commit -am "feat(scripts): Square-era monthly income entries, rent documents, reconcile model seeds"
```

---

### Task 13: Asset profiles, runbook, Penny month-close

**Files:**
- Create: `scripts/seed_asset_profiles.py` (two `account.asset.profile` per company: "Machinery and equipment, 7 years" and "Land improvements, 15 years", method linear, accounts 1500 / 1590 / 6140, journal MISC; idempotent by name)
- Create: `docs/books-runbook.md`, `docs/penny-month-close.md`
- Create: `grove_schedule_f/README.md`
- Test: `scripts/tests/test_seed_asset_profiles.py` (pure: profile vals use the three codes and the MISC journal code)

**Interfaces:**
- `docs/books-runbook.md` sections, in order: (1) One-time bootstrap by Josh in the UI: Stripe online statement provider on the nursery `STRP` journal with a read-only key, API key for the Grove Agents user, Paperclip env binding of that key for Penny's MCP server (per `paperclip-agent-env-binding-canonicalization`), Discord finance webhook URL as `DISCORD_FINANCE_WEBHOOK_URL` on the odoo service env. (2) Order of scripts for the 2026 backfill: `setup_books.py` → `seed_reconcile_models.py` → `seed_asset_profiles.py` → `import_bank_csv.py --from 2026-01 --to 2026-10` → `import_square_summary.py` → `book_rent.py` → month loop. (3) The month loop as Penny runs it. (4) Year-end package: pivot export per company, unpaid list, uncategorized list, asset register, intercompany balance (1350 in each company), the CPA question list.
- `docs/penny-month-close.md`: Penny's exact steps with MCP calls (`search_records` on `account.bank.statement.line` with `is_reconciled=False` and journal; for each line: apply the matching reconcile model by `match_label` to fill `grove_proposed_account_id`, `grove_proposed_enterprise_id`, `grove_proposed_model_id`; when no model matches, propose from the vendor name and set `grove_proposal_uncertain=True`; never call `action_grove_accept_proposal`; after Josh's review run `grove.mirror.run.grove_run_month` for the month; post the close summary to the finance webhook with counts: lines imported, proposed, accepted, rejected, still open, mirror total).

- [ ] **Step 1: Write the failing pure test for asset profiles**
- [ ] **Step 2: Implement the script; run the test; lint**
- [ ] **Step 3: Write the three docs** (no em dashes; commands in fenced blocks; no secret values)
- [ ] **Step 4: Commit**

```bash
git commit -am "docs: books runbook, Penny month-close procedure, asset profile seeds"
```

---

### Task 14: Full suite, manifest, PR ready

**Files:**
- Modify: `grove_schedule_f/__manifest__.py` (final `data` list order: security, data, views, menus), `conftest.py` (all Odoo test globs), `grove_schedule_f/tests/__init__.py` (all Odoo test modules, alphabetical)

- [ ] **Step 1: Run everything**

Run: `ruff check . --select E,F,I --line-length 120 && ruff format --check . --line-length 120 && python3 -m pytest grove_schedule_f/tests/test_lines.py scripts/tests/ -v && bash scripts/dev/run_odoo_tests.sh grove_schedule_f`
Expected: all green; the Odoo run reports the test count from Tasks 4 through 11 (at least 30) and 0 failures.

- [ ] **Step 2: Push, mark the PR Ready, fill the template**

PR body: what Phase 1 delivers, the five deviations above, the dependency on the odoocker PR from Task 2 (link it, qualified `odoocker-goldberrygrove#NNN`), the one-time bootstrap steps Josh does, and the backfill order. Ends with the attribution line.

- [ ] **Step 3: Hand off to Josh**

Josh approves and enqueues both PRs (protected paths). Backfill runs on prod after Train 5 promote, in the runbook order, from Josh's terminal.
