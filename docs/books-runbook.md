# Farm books runbook (Schedule F, phase 1)

How to bring the 2026 books up, run the month loop, and produce the year-end package. The module is `grove_schedule_f`; see its README for the account map. Penny's exact steps are in `docs/penny-month-close.md`.

All scripts read `ODOO_URL`, `ODOO_DB`, `ODOO_USER` and `ODOO_PASSWORD` from the environment. Export them in your shell first. Never type the password inline in a command.

## 1. One-time bootstrap (Josh, in the UI)

1. Stripe feed for the nursery. Open the nursery company's `STRP` journal, set its bank feed to the online statement provider `account_statement_import_online_stripe`, and paste a read-only Stripe key. Use a restricted key with read access only.
2. API key for the Grove Agents user. `setup_books.py` creates `agents@goldberrygrove.farm` without a password. Open that user in Settings > Users, and create an API key for it from the Account Security tab. Store the key in the AgenticOS Infra item in 1Password. Do not paste it anywhere else.
3. Bind the key into Penny's MCP configuration in Paperclip as an env binding on the Penny agent. Read the agent's current env first, merge the new binding into it, and write the merged result back. Never replace the whole env, because a PATCH shallow-merges and drops what you leave out.

## 2. The 2026 backfill

Run in this order. Josh runs these from his own terminal.

1. Install the module first. git-sync picks up the merged branch; then confirm in Apps that `grove_schedule_f` is installed (not just listed). The setup script needs the module's accounts, journal flags and the Grove Agents group.
2. Dry-run the bootstrap (step below).
3. Run it for real only when the dry-run output looks right.

Check the plan for the bootstrap without writing anything:

```bash
python3 scripts/setup_books.py --dry-run
```

Before continuing, the dry-run output must show:

- the preflight line for the nursery with a `chart_template` value and `parent_id=False` (the script stops otherwise);
- 7 farm journals in the verification table, including `BNK1`;
- 0 nursery journals (they are created by the real run);
- the payment provider 24 (nursery Stripe) currently pointing at the farm bank journal, which the real run repoints to the nursery `STRP` journal once it exists;
- the 2 posted test moves in the posted-moves list.

If any of these differ, stop and find out why before the real run. The real run calls `grove_schedule_f_setup` on both companies twice: once before it creates the journals and again after, so the new nursery sale and purchase journals get the 4020 and 6320 defaults.

Run the bootstrap. The reset flags draft and delete the posted test moves whose name or ref matches the pattern. This deletes posted moves and cannot be undone. The pattern is a case-insensitive regular expression matched against the move name or ref across both companies, so anchor it with `^`, for example `^TEST-`. At most 10 matches are allowed. Fill in the pattern:

```bash
export RESET_PATTERN=""
```

Preview first. This prints the posted moves and the match count without changing anything:

```bash
python3 scripts/setup_books.py --dry-run --reset-test-entries --reset-ref-pattern "$RESET_PATTERN"
```

When the matches are exactly the test entries, run it for real:

```bash
python3 scripts/setup_books.py --reset-test-entries --reset-ref-pattern "$RESET_PATTERN"
```

Seed the reconcile models (suggestions only, nothing auto-posts):

```bash
python3 scripts/seed_reconcile_models.py
```

Seed the asset profiles (two per company, accounts 1500, 1590 and 6140, journal MISC):

```bash
python3 scripts/seed_asset_profiles.py
```

Import the shared bank CSV for January through October. Name your CSV's columns with the flags; use `--debit-col` and `--credit-col` instead of `--amount-col` if the bank splits them, and add `--id-col` if the bank gives a transaction id:

```bash
python3 scripts/import_bank_csv.py bank.csv --date-col "Date" --amount-col "Amount" --desc-col "Description" --from 2026-01 --to 2026-10
```

Book the Square-era monthly income (CSV columns month, gross, fees):

```bash
python3 scripts/import_square_summary.py square_months.csv
```

Book the annual rent. Josh decides the amount:

```bash
export RENT_AMOUNT=""
```

```bash
python3 scripts/book_rent.py --period 2026 --amount "$RENT_AMOUNT"
```

Each script also takes `--dry-run`. Every script is safe to run again; they skip what already exists.

Then run the month loop below, one month at a time, January through October.

## 3. The month loop

Penny's side, in short (details in `docs/penny-month-close.md`): for each unreconciled line on the shared bank, she applies the matching reconcile model or proposes an account and enterprise, flags anything uncertain, and never accepts anything. She writes the close summary as one close note per month (Accounting > Schedule F > Close notes), and reports the mirror as pending for Josh. She can create and read close notes but cannot change or delete them; Josh can correct one.

Josh's side:

1. Open Accounting > Schedule F > Bank lines to review.
2. For each line, click Accept or Reject. Accept validates the proposed account and enterprise and then reconciles.
3. Open Accounting > Schedule F > Month mirrors and run the mirror for the month. Josh runs the mirror; Penny's access is read-only on it.
4. If you retag an older month after its mirror ran, open that month's mirror and click Check. A stale mirror is flagged; reverse and re-run it.

## 4. Year-end package

Run these before January payments are entered.

1. Schedule F by line. Open Accounting > Schedule F > Schedule F by line in each company (farm, then nursery), set the tax year, and export the pivot to xlsx.
2. Unpaid at year end. Export it BEFORE any January payments are entered. It shows current residuals, not an as-of snapshot, so a January payment would make a December bill look paid. Set the invoice-date period to the tax year.
3. Uncategorized bank lines. Export it and clear it, or list what remains.
4. Asset register. Export the OCA asset register with the year's depreciation.
5. Intercompany balance. Read the 1350 Intercompany balance in each company. The two should offset; give the CPA the one net figure to classify (loan, contribution, or offset against rent).
6. CPA question list: how to classify the 1350 net; which Schedule F line the farm's rent income belongs on (the books use 4080, line 8); depreciation method and section 179 for each asset; the taxable portion of any line with an a/b split; whether the nursery's Square-era monthly summaries are acceptable support; how to treat owner-paid receipts (account 2900) and owner draws (3100); does rent settled through the intercompany account count as a cash-basis payment?

## Rules the module enforces

- Every counterpart on the shared bank journal carries exactly one enterprise (Farm, Nursery or Personal) at 100%.
- Bills and owner-paid receipts in the farm need an enterprise too: the farm's Vendor Bills (BILL) and Owner paid (OWNR) journals carry the same rule, so nursery spending on a farm bill is tagged Nursery and mirrored.
- Personal posts only to 3100 Owner draw.
- Agents can only propose. They cannot post, reconcile, auto-reconcile, edit auto-reconcile models, delete any reconcile model, or touch an entry armed for auto-post.
- Accept validates the proposed account and enterprise before it reconciles.
- Credit-note offsets are not cash events.
- The mirror moves only expenses, and only what the cash view shows for the month. Nursery-tagged income on the farm is not mirrored; the mirror shows its total as Skipped income so you can book it in the nursery.
