# Penny month-close procedure

Penny works through the Odoo MCP as the Grove Agents user. That user can read the books and write proposal fields on bank statement lines. It cannot post, reconcile, accept or reject. Josh does those in the UI.

## Rules

- Never call `action_grove_accept_proposal` or `action_grove_reject_proposal`. Only Josh does.
- Writing any proposal field on a line sets `grove_proposal_state` to `proposed` automatically. Do not set the state yourself.
- The enterprise is one of Farm, Nursery or Personal. Personal goes only with account 3100 Owner draw.
- Agents may create manual reconcile models. Never create or change an auto-reconcile one.
- Do not write to posted entries.

## Step 1. Find the open lines

`search_records` on `account.bank.statement.line`:

- domain: `[["is_reconciled", "=", false], ["journal_id.code", "=", "BNK1"], ["date", ">=", "2026-MM-01"], ["date", "<=", "2026-MM-DD"]]`
- substitute the first and last day of the month being closed, as YYYY-MM-DD
- fields: `date`, `payment_ref`, `partner_name`, `amount`, `journal_id`, `grove_proposal_state`

Keep the count as "imported" for the month's lines.

## Step 2. Find the models

`search_records` on `account.reconcile.model`:

- fields: `name`, `match_label`, `match_label_param`, `match_journal_ids`, `line_ids`
- then read `account.reconcile.model.line` for the `line_ids`, fields `account_id` and `analytic_distribution`

A model matches a line when `payment_ref` contains `match_label_param` (for `match_label` equal to `contains`) and the line's journal is in `match_journal_ids` (or that list is empty).

## Step 3. Propose each line

For each line, `update_record` on `account.bank.statement.line` with these fields:

- `grove_proposed_account_id`: the model line's `account_id`
- `grove_proposed_enterprise_id`: the Farm, Nursery or Personal analytic account of the line's company. The model line's `analytic_distribution` has its id as the key.
- `grove_proposed_model_id`: the matching model
- `grove_proposal_note`: one plain sentence on why

When no model matches, propose from the vendor name in `partner_name` or `payment_ref`, set `grove_proposal_uncertain` to true, and say in `grove_proposal_note` what you guessed.

Special case: lines matched by the model "Stripe payout (nursery), money in" always get `grove_proposal_uncertain` set to true, with a note to match the amount against both Stripe journals (nursery and farm), because both accounts share one descriptor.

First-time vendor: also propose a new model. Use `post_message` on the line's journal entry (the line's `move_id`) with the vendor, the match text, the account and the enterprise. Set `grove_proposal_uncertain` to true on the line. You may create a manual model with `create_record` on `account.reconcile.model` (trigger `manual` only). Never create an auto one.

Keep counts: proposed.

## Step 4. Wait for Josh

Josh opens Accounting > Schedule F > Bank lines to review and accepts or rejects each line. Do not continue until he says the review is done.

Then count with `aggregate_records` on `account.bank.statement.line` grouped by `grove_proposal_state` for the month: accepted, rejected, and still open (lines with `is_reconciled` false).

## Step 5. Mirror

Do not call `grove_run_month` or `action_check`. Your access to `grove.mirror.run` is read-only. Report in the summary: "mirror pending, Josh to run it from Schedule F > Month mirrors". Josh also clicks Check on older months he has retagged.

## Step 6. Post the close summary

Use `post_message` on `account.journal` (the farm "Shared bank (2026)" journal, code BNK1) with one plain-text note containing:

- month
- lines imported
- proposed
- accepted
- rejected
- still open
- mirror: pending, Josh to run it from Schedule F > Month mirrors

Never include a key.
