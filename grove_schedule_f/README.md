# grove_schedule_f

Schedule F books for the farm and nursery on Odoo 19. The module creates a Schedule F chart of accounts in each company, adds a mandatory Enterprise dimension (Farm, Nursery or Personal) on the shared bank journal, lets the Grove Agents user propose a category for each bank line while only people accept it, provides a cash-basis "Schedule F by line" report plus year-end lists, and mirrors each month's nursery-tagged expenses from the farm company into the nursery company through the intercompany account. The step-by-step procedures are in `docs/books-runbook.md` and `docs/penny-month-close.md`.

## Account map

Generated from `schedule_f_lines.py`. If you edit the lines file, update this table by hand.

| Code | Name | Schedule F line | Kind |
|---|---|---|---|
| 4010 | Sales of resale items | 1a | income |
| 4020 | Sales of raised produce, plants and seeds | 2 | income |
| 4030 | Cooperative distributions | 3a | income |
| 4040 | Agricultural program payments | 4a | income |
| 4050 | CCC loans reported | 5a | income |
| 4055 | CCC loans forfeited | 5b | income |
| 4060 | Crop insurance and disaster payments | 6a | income |
| 4070 | Custom hire income | 7 | income |
| 4080 | Other farm income | 8 | income |
| 4015 | Cost of resale items | 1b | expense |
| 6100 | Car and truck | 10 | expense |
| 6110 | Chemicals | 11 | expense |
| 6120 | Conservation | 12 | expense |
| 6130 | Custom hire | 13 | expense |
| 6140 | Depreciation and section 179 | 14 | expense |
| 6150 | Employee benefit programs | 15 | expense |
| 6160 | Feed | 16 | expense |
| 6170 | Fertilizers and lime | 17 | expense |
| 6180 | Freight and trucking | 18 | expense |
| 6190 | Gasoline, fuel and oil | 19 | expense |
| 6200 | Insurance | 20 | expense |
| 6210 | Interest, mortgage | 21a | expense |
| 6215 | Interest, other | 21b | expense |
| 6220 | Labor hired | 22 | expense |
| 6230 | Pension and profit sharing | 23 | expense |
| 6240 | Rent, machinery and equipment | 24a | expense |
| 6245 | Rent, land and other | 24b | expense |
| 6250 | Repairs and maintenance | 25 | expense |
| 6260 | Seeds and plants | 26 | expense |
| 6270 | Storage and warehousing | 27 | expense |
| 6280 | Supplies | 28 | expense |
| 6290 | Taxes | 29 | expense |
| 6300 | Utilities | 30 | expense |
| 6310 | Veterinary, breeding and medicine | 31 | expense |
| 6320 | Other expenses | 32 | expense |
| 6321 | Payment processing fees | 32 | expense |
| 1350 | Intercompany | none | balance |
| 1500 | Farm equipment and improvements | none | balance |
| 1590 | Accumulated depreciation | none | balance |
| 2900 | Owner contributions payable | none | balance |
| 3100 | Owner draw | none | balance |

## Source of the line labels and the IRS check

The line labels come from harknett/schedule-f-books (MIT). They were checked on 2026-11-05 against the IRS instructions for Schedule F (Form 1040), tax year 2025, at irs.gov/instructions/i1040sf. That page paraphrases most lines but does not print the form's own labels for lines 1a to 1c, 11, 17, 19, 26 to 28 and 31, so those were checked against the 2025 form as the author knows it. Re-check before filing.

Differences noted:

- Line 32 (other expenses): the form has lines 32a to 32e for itemised other expenses and 32f for capitalised costs. The module maps both 6320 and 6321 to a single "32". The CPA splits them.
- Lines 3, 4 and 6 have an "a" line (total) and a "b" line (taxable portion). The module books the amount at "a" only (3a, 4a, 6a). The CPA sets the taxable portion.
- Lines 5a and 5b exist (CCC loans reported, forfeited). The form's 5c (forfeited loans not elected as income) has no account.
- Line 1b is the cost of resale items. It sits in account 4015 with account type direct cost, so it reads as an expense in the cash view, which matches how line 1c is computed on the form (1a minus 1b).
- Line 9 (gross income), 33 (total expenses) and 34 (net profit) are totals, not accounts.

No line number from the harknett map was found to be wrong.

## Odoo 19 deviations

- Accounts are archived, not deprecated. Odoo 19 has no `deprecated` flag, so the generic "Product Sales" and "Expenses" accounts are archived (unless a journal or company property still uses them).
- The Enterprise analytic plan is shared by name across companies (Odoo 19 plans carry no company). The Farm, Nursery and Personal analytic accounts exist per company.
- Penny's proposal notes and the Accept and Reject buttons act on the statement line's journal entry, not on the statement line itself.

## Where to find it

Accounting > Schedule F, with these menus: Schedule F by line, Bank lines to review, Unpaid at year end, Uncategorized bank lines, Month mirrors.
