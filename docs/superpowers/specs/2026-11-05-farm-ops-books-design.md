# Grove farm operations on Odoo: design

Date: 2026-11-05
Status: draft for Josh's review
Target: prod Odoo 19 Community at odoo.gatheringatthegrove.com, three companies (Goldberry Grove Farm, At The Grove Nursery, George George George Woodworking)

## 1. Intent

Make Odoo the single source of truth for three things across every Grove business: hours worked, money spent, and tasks done. Today none of the three is tracked in one place. The bank history lives in two bank portals, hours are not recorded at all, and build plans like the hoophouse footpad regrade live in Google Docs.

The trigger was planning the footpad regrade (about $9,900, seven phases, a crew of two, five to eight calendar weeks with weather). Josh needs to see budget against actual, who worked when, and what slipped, and the same need exists for every other build, for daily farm work, for equipment upkeep, and for building this very app.

The December 31 tax year end sets the first deadline: the farm and nursery books for 2026 must come out of Odoo as Schedule F line totals a CPA can use, which means backfilling ten months of bank history now.

## 2. Goals, non-goals, anti-goals

Goals

- One spine: every hour and every dollar lands on an analytic account (a job, an aspect of the farm, or a machine). Every report reads from that spine.
- Five front doors onto the spine, each a separate Odoo module or app, that stay separate in daily use: books and Schedule F, build projects, operations tasks, app-build sprints, maintenance.
- Farm hands log their own work from their phones, with a daily Discord loop as the primary capture.
- Penny (the Paperclip CFO agent) does the categorization and the monthly close through the Odoo MCP, with humans approving before posting.
- Tax year 2026 is reported from Odoo.

Non-goals

- Payroll. Everyone is an owner-operator today. Hours are costed at the co-op's uniform sweat-equity rate on the analytic side only and never touch the general ledger.
- Replacing farmOS. It stays a possible later feed, not a parallel entry point.
- Enterprise-only Odoo features (Gantt, timesheet grid validation, bank sync). Community plus OCA plus a small custom module set.
- Crop planning.

Anti-goals (what makes this a failure even if it technically works)

- A worker has to create tasks in the field, or the daily check-in takes more than two minutes.
- An agent posts to the books without a human confirm.
- The Schedule F report is a spreadsheet export somebody edits by hand.
- A fourth place to look. If a number is not in Odoo, it does not exist.

## 3. Constraints

- Odoo 19.0 Community, multi-company. Modules ship through grove-odoo-modules and ride the biweekly release train (Monday up, Wednesday promote, Thursday teardown). Agent PRs on protected paths need Josh to approve and enqueue.
- Prefer no new credentials. The NWS weather API is keyless. Discord webhooks already exist for topic channels. One new credential is unavoidable and is called out in section 15.
- Agents act through the Odoo MCP (YOLO mode, all models). Every record an agent touches needs a draft state and a chatter trail.
- Penny's run queue can take minutes to hours. Nothing in the 7am or 5pm loop may wait on a Paperclip run.
- Josh prefers automation over manual steps; click-docs only for one-time bootstrap.

## 4. Current state in prod (inventoried 2026-11-04)

- Accounting: generic US chart (18 income and expense accounts), one bank journal, zero bank statement lines, zero analytic accounts, two posted entries total, both from the nursery Stripe invoice test. The farm has nothing posted. The nursery's invoice posted into the farm's journal set; the nursery has no journals of its own.
- Project is installed with zero projects. No HR, timesheet, expense, purchase, maintenance, time-off or asset modules. Only OCA module present is web_dark_mode.
- Seven internal users: three farm, four nursery.
- Stripe payment provider is live on the nursery; POS, sales, stock, MRP and website are installed and in use.

Consequence: the backfill has nothing to conflict with. Step zero is giving each company its own journals.

## 5. The spine

Three core objects carry everything. Each front door only decides how they get created.

| Object | What it is here | Who creates it |
|---|---|---|
| Analytic account | A job (footpad regrade), an aspect of a business (nursery watering, farm orchard, nursery marketing), or a machine (tractor) | Created with each project or equipment record, never by hand |
| Timesheet line | Hours by one person on one task on one date, costed at that person's hourly cost | The 5pm check-in, the phone timer, or the lead |
| Journal entry | Money. A bank line categorized to an expense account, a vendor bill, a receipt | Bank import plus Penny, or a person entering a bill |

Each company owns its own analytic plan. A person who works for two companies in one day has one login and one employee record per company; the task's project decides which company the hours land in.

## 6. Front door 1: Books and Schedule F

### 6.1 Chart of accounts

A custom module `grove_schedule_f` replaces the generic chart on the farm and nursery companies with a Schedule F chart. Every income account carries a Schedule F line (1 through 8) and every expense account carries a line (10 through 32), stored as a field on the account and also as an account tag so standard Odoo reports can group by it. The line map is lifted from the harknett/schedule-f-books category map (35 categories, MIT) and checked against the current IRS Schedule F instructions by the builder.

Sub-accounts are allowed under a line (for example line 24a rent of machinery split into excavator and roller) as long as they roll up.

The woodworking company keeps a generic Schedule C style chart. Farm and nursery file separate returns (Josh, 2026-11-05), so both get the Schedule F chart; 6.9 says how one shared bank feeds two sets of books in 2026.

### 6.2 Cash basis

The Schedule F report is cash basis. The rule for dating a line:

- A line in a bank or cash journal is dated the bank date.
- A line on a vendor bill or customer invoice is dated by the payment that reconciled it. A bill paid in two parts splits by payment date.
- An unpaid bill at year end is not on the report. It appears on a separate "unpaid at year end" list for the CPA.

Most expenses will arrive as bank lines categorized straight to an expense account, so most of the report is simply bank-dated. Vendor bills are the exception, used for committed items (rentals, gravel orders) so the project budget can show committed before paid.

### 6.3 Bank import

OCA `account_statement_import_file` plus `account_statement_import_ofx` and `account_statement_import_sheet_file` (CSV) on 19.0. Each bank's export is imported into its company's bank journal. OCA `account_reconcile_oca` provides the Community reconciliation screen. `account_statement_import_online_stripe` pulls the nursery's Stripe balance activity directly, which closes the loop on online sales without a file.

An online bank connector is a follow-up once the categorization loop is proven; the OCA online framework is on 19.0 and the choice of provider is left to the builder to research at that time.

Money sources known today: one unified business bank account that carries farm, nursery and personal spending mixed together (Josh, 2026-11-05), the nursery Stripe account, the farm Stripe account (the Printify shirts on goldberrygrove.farm), Square (POS before the Odoo POS cutover), Venmo (a clearing journal, already ruled). The plan is to split into two clean bank accounts for 2027.

Consequence for 2026: every bank line needs an enterprise dimension (farm, nursery, personal) on top of its account. Personal lines post to an owner-draw equity account, never to an expense. Penny proposes the enterprise along with the account, and vendor rules can carry a default enterprise. How the enterprise split is booked across the two Odoo companies is in 6.9. Owner-paid expenses (a personal card at the hardware store) go in as receipts through `hr_expense` against an owner-contribution liability account, dated the receipt date, and are not reimbursed through payroll. Josh confirmed this account list on 2026-11-05.

### 6.7 Income recognition

Schedule F needs gross sales with processor fees as a separate expense, so bank deposits (net payouts) are never the income source. One authoritative source per channel; everything else is matched to it, never added:

- Online and invoice sales: the Odoo invoice, posted on payment. Stripe fees come from the Stripe balance import as a fee expense line, and the Stripe payout is matched to the deposit on the bank line.
- POS: the POS session close entry (gross), with Square or card fees from the processor report as an expense.
- Venmo and cash: the clearing journal entry at gross.

For the 2026 backfill, orders already in Odoo that never posted are posted by a one-time script per channel, and pre-Odoo months (Square era) are entered from the processor's monthly summary at gross with fees as one line. From Phase 1 go-live, every sale channel posts: product income accounts, POS journal, and the Stripe provider journal are configured as part of Phase 1. That setup work was missing from the first draft and is now in section 15.

### 6.8 How "no agent posts without a human confirm" is enforced

Convention is not enough, so it is enforced by permissions:

- Agents get one dedicated Odoo user, "Grove Agents", with a group that can read everything, write chatter, write a `proposed_account_id` and `proposed_note` on bank statement lines, and create draft bills. It cannot post journal entries, reconcile, or validate anything. Penny's MCP connection uses that user, not Josh's. This is one internal API key, stored in the AgenticOS Infra item.
- Reconciliation models start in propose-only mode. Josh promotes a model to auto-reconcile only after a month where its proposals were all accepted. The promotion is a checkbox on the model with a chatter note.
- Odoo's own behaviour of posting an imported statement line to the suspense account is accepted: the cash is real, the category is pending. The Schedule F report reads only categorized lines and lists "uncategorized at year end" separately, so nothing in suspense can silently count as an expense.

### 6.4 Categorization with Penny

Reconciliation models hold the rules ("Tractor Supply goes to line 24 supplies"). The monthly loop:

1. Import the file. Lines land as unreconciled statement lines.
2. Penny, through the MCP, applies reconciliation models, proposes a category for every unmatched line, and proposes a new model whenever a vendor shows up for the first time. Her proposals are chatter messages on the statement line plus a draft reconciliation, not posted entries.
3. Josh reviews an exceptions list: new vendors, lines over a threshold, anything Penny flagged as uncertain. He approves in the reconciliation screen, which posts.
4. Penny posts a month-close summary to the finance Discord channel.

The rules live in Odoo so they survive a change of agent or model. The same loop runs the 2026 backfill: two exports, January through October, one import per company, then the loop in chunks of a month.

### 6.5 Fixed assets

OCA `account_asset_management` (19.0). Each capital purchase becomes an asset with purchase date, cost, in-service date and a depreciation profile. The CPA decides the method; Odoo keeps the register. Equipment records (front door 5) link to their asset.

### 6.6 Reports for the CPA

- Schedule F by line, per company, cash basis, for a date range. Custom report in `grove_schedule_f`, built as a MIS Builder template over the account tags with the cash-date rule applied in a custom query. MIS Builder (OCA, 19.0) handles the layout, export and comparison columns.
- Unpaid bills and uncollected invoices at year end.
- Fixed asset register with the year's depreciation.
- Profit and loss, balance sheet, general ledger: OCA `account_financial_report` (19.0).
- Hours by person by company for the year (the sweat-equity register, section 11.4).

Penny drafts a year-end package from these and a list of open questions for the accountant.

### 6.9 Two returns, one bank: how 2026 is booked

Farm and nursery file separate returns (Josh, 2026-11-05), and in 2026 one bank account paid for both plus personal spending. The booking rule:

- The shared bank journal lives in the farm company for all of 2026. An analytic plan "Enterprise" with three accounts (farm, nursery, personal) is mandatory on every line of that journal. Penny proposes it with the account; a line cannot post without it.
- Farm lines post normally. Personal lines post to owner draw (equity), never an expense. Nursery lines post to the matching Schedule F expense account in the farm company, tagged enterprise = nursery, so the receipt, vendor and bank date stay where the money moved.
- At each month close a generated entry mirrors the month's nursery-tagged activity into the nursery company: one line per Schedule F account against "Due to farm" (nursery side) and "Due from nursery" (farm side), with the farm's expense accounts reclassified out in the same entry. After the mirror, each company's Schedule F report reads only its own books. The mirror is a script in `grove_schedule_f`, run from Penny's close checklist, reversible per month.
- Nursery income already lives in company 3 (invoices, POS, Stripe, Venmo clearing). When a nursery payout lands in the shared bank, the farm books it to "Due to nursery" and the nursery books the payout against "Due from farm". Nursery company journals must exist first (the "step zero" in section 4).
- Rent: the nursery pays the farm rent that covers tractor hours, shared machines and shared labor (Josh, 2026-11-05). The farm invoices rent as income (line chosen by the CPA), the nursery records the bill as rent expense (line 24b). It settles through the intercompany accounts, not the bank, until the 2027 split. There is no other intercompany charging, and labor is never charged between companies.
- Year end: the net intercompany balance is one figure for the CPA to classify (loan, contribution, or offset against rent). Two Schedule F reports, one per company; the woodworking company stays on its own chart.
- 2027: two bank accounts, each journal in its own company. The Enterprise plan stays mandatory only on the farm bank (farm or personal), the mirror script retires, rent keeps going through intercompany.

## 7. Front door 2: Build projects

### 7.1 Shape

One Odoo project per build, under the company that owns it. The footpad regrade is under the nursery. The project's analytic account is created with it.

- Phases are milestones (core Community) with a target date. OCA `project_milestone_status` adds done/late state.
- Tasks sit under a milestone with planned start, deadline, allocated hours, assignees, and dependencies (core Community task dependencies).
- Money attaches to a task: a vendor bill line or a bank line carries the project's analytic account and optionally the task. Budget per project uses OCA `account_budget_oca` plus `mis_builder_budget` on the analytic account: budgeted, committed (bills posted), paid (bills paid or bank lines).
- Materials on a task use OCA `project_task_material` where a bill of materials matters (block count, pipe sticks). Optional per task.

### 7.2 Progress

Computed, never typed:

- Tasks done over tasks planned, per milestone and per project.
- Hours logged over hours allocated.
- Dollars paid and committed over budget.

### 7.3 Views

- Board first (Josh's choice): kanban columns per milestone, task cards inside.
- Timeline second: OCA `project_timeline` (19.0, built on `web_timeline`), phases and tasks as bars, today marked, slipped tasks visible. Dragging a bar moves dependents.
- Numbers: the three progress meters on the project form.

### 7.4 Daily log

Chatter on the project with a short template (who was on site, what happened, deliveries, weather). The weather job (section 13) writes observed rain and temperature into the log every evening so nobody types it.

### 7.5 Not used

The MedELHattab/odoo-construction suite (single maintainer, zero stars, self-described test project). The OCA building blocks above cover budget against actual, materials, timeline and daily log.

## 8. Front door 3: Operations tasks by aspect

### 8.1 Aspects are projects

Each aspect of each business is its own project with its own analytic account. Starting list, editable by Josh:

- Nursery: watering and plant care, potting and propagation, order packing and shipping, marketing, hoophouse (the build and later its operation), catch-all.
- Farm: orchard, firewood and road stand, equipment, pasture and grounds, marketing, catch-all.
- Woodworking: shop, catch-all.

Recurring work uses core task recurrence (daily watering, weekly mowing). Tasks carry a location tag (hoophouse, orchard block 2, road stand) from a shared tag set.

### 8.2 Generated work

Order packing is read from warehouse pickings in the ready state, not typed as tasks. The morning summary says "4 orders to pack, 2 ship today" with links. One recurring "packing block" task carries the hours.

### 8.3 The daily planner

A nightly job builds each person's list for the next day. Inputs:

- Who is working (resource calendar minus approved time off).
- Hours available per person.
- Which aspects each person covers (a many-to-many on the employee).
- Open tasks: priority, deadline, remaining hours, weather condition and window, "needs a hand" flag, assignee if fixed.
- Pickings ready to pack.

Output: an ordered list per person that fits their hours, with the rest pushed to the next day, and a "rainy day list" of indoor tasks per person. First version is deterministic rules in Odoo (deadline first, then priority, then weather fit, then fill remaining hours), run by cron. The lead can reorder or reassign in Odoo before 7am. An agent may propose changes to a plan through the MCP later; the planner never waits on one.

## 9. Front door 4: App-build sprints

The work of building all of this is tracked in the same Project app under a "Grove Odoo" project on the farm company. Sprints are milestones with dates; tasks carry OCA `project_task_code` (task IDs) and `project_task_pull_request` (PR links). The Paperclip board stays the agents' queue; the Odoo project is the human view of the same work and links to GOL issues by code. No scrum module. vertelab/odoo-project_scrum and OCA/project-agile are skipped (ecosystem lock-in and dead respectively).

## 10. Front door 5: Maintenance

Core `maintenance` module per company: equipment register, maintenance requests, teams. OCA additions on 19.0: `maintenance_plan` (preventive schedules by interval that auto-create requests ahead of time), `maintenance_equipment_status`, `maintenance_timesheet` (hours on a request land on the machine's analytic account), `maintenance_request_purchase` (parts bought against a request), `maintenance_project` if a repair becomes a build.

Custom additions in `grove_ops`:

- Equipment links to its fixed asset so the depreciation register and service log are the same list.
- Each equipment record gets an analytic account so service costs roll up per machine.
- Reminders: a preventive request due within N days raises an activity on the owner and a line in the morning summary of whoever covers the equipment aspect.

Hour-based intervals (service every 200 engine hours) need a meter reading on the equipment; builder adds a simple reading log if `maintenance_plan` does not cover it.

## 11. People

### 11.1 Accounts

Every farm hand is an internal user (no per-user fee in Community) with an employee record in each company they work for. Core `hr`, `hr_timesheet`, `hr_holidays` are installed.

### 11.2 Permissions

Three groups, designed rather than defaulted:

- Worker: sees own tasks across all projects, own timesheets, own time off, and the warehouse transfers they pack (picking, delivery address, contents, and the validate button). No accounting, no sales orders or prices, no customer financials, no other people's hours.
- Lead: everything a worker sees plus the crew's tasks and hours, the planner, approval of the week, time-off approval. Every owner-operator is a lead (Josh, 2026-11-05); today that is everyone.
- Owner: everything.

Record rules enforce the worker scope. The builder confirms the worker group cannot reach the accounting menus or the nursery sales data, and that the packing exception is limited to transfers.

The seven existing users keep their current rights until Phase 2 assigns each one a role from a list Josh signs off on. Nobody loses access as a side effect of installing the groups.

### 11.3 Timesheets and the week

Hours arrive three ways: the 5pm check-in (primary), the phone timer (OCA `hr_timesheet_time_control`, optional), or the lead typing them. OCA `web_responsive` and `web_pwa_customize` make the phone view usable and installable.

Weekly approval: OCA `hr_timesheet_sheet` exists on 18.0 but not yet 19.0. The builder either ports it (preferred if the diff is small, and contributes the port upstream) or builds a minimal week-approval model in `grove_ops` (one record per employee per week, states open, submitted, approved; approved lines are locked). Either way, Friday is submit day and any other owner-operator approves by Monday morning; nobody approves their own week. Every member can see every week, its approval, and its edit history (Josh, 2026-11-05: approvals are public to all).

### 11.4 Hourly cost and the sweat-equity register

Each employee's hourly cost is the co-op's uniform sweat-equity rate: $21 per hour, the same in all three companies (Josh, 2026-11-05). Nobody is paid until the businesses are profitable, so the rate is a contribution value, not a wage. Analytic lines carry it so job reports show labor at the rate; nothing posts to the general ledger.

Approved hours in Odoo are the legal record of contribution under the co-op agreement (Josh, 2026-11-05). That raises the bar on the week model: every change to an approved week is kept as history (who, when, old and new value), corrections go through a reopen-edit-reapprove step that leaves the trail, and a week is approved by someone other than its owner. The register is a report every member can read: approved hours by person by company by year, valued at the rate, with days off listed.

Josh's hours building this app count (Josh, 2026-11-05). They log against the app-build sprint project under a "Platform" analytic account, and the register shows platform hours as a contribution to the co-op, which owns the platform, rather than to the farm or the nursery (my call on the company placement; the sprint project sits in the farm company because something has to hold it).

When someone is hired on a wage, their hourly cost becomes real and payroll handles the ledger side; no change to entry habits.

### 11.5 Time off

Core `hr_holidays`. A worker says "off next Friday" in the check-in, the bot creates a time-off request, any other owner-operator approves by reacting in the crew channel so the approval is public, the Odoo button exists as a fallback, and the planner stops scheduling them.

## 12. The Discord loop

Every farm hand has a private channel (for example `#wes`) that the worker and the owners can read. One `#crew` channel is shared.

- 7:00am, private channel: the day's planned list from the planner, with the weather line, the packing count, maintenance reminders, and the rainy-day list.
- Mid-day, private channel: only when the weather job moves one of their tasks.
- 5:00pm, private channel: "how'd it go." The worker replies in plain words. The bot parses it into done (with hours), new unplanned work (filed under the right aspect and company), and pushed. It echoes the parse and waits for "ok" or corrections. On ok it writes timesheet lines, closes or reopens tasks, creates the unplanned tasks, moves the pushed ones, and records any help request or time-off ask. Nothing is written before ok.
- Evening, `#crew`: one digest of all completed work across all three businesses, hours by business, plus "needs a hand" requests and pending time-off asks.

### 12.1 Parsing

Two tiers. Tier A ships first and Tier B is a later phase (my call, section 15), so no language-model key is needed for the bot now:

- Tier A, deterministic: the 5pm message carries a numbered list and the worker replies with numbers and hours ("1 1h, 2 1.5h, 3 1h partial, new: marketing 2.5h nursery, push 4"). No language model needed, works offline from any agent. Always available as a fallback.
- Tier B, free text: a language model turns Wesley's paragraph into the same structure. Needs a fast model call outside Paperclip's run queue.

### 12.2 Where the bot runs

A small service (Python) deployed beside the Odoo stack through odoocker, reading from and writing to Odoo over XML-RPC with a dedicated bot user. It needs a Discord bot token to read replies, which webhooks cannot do. Josh approved the bot token on 2026-11-05 ("how we've done everything before"); it lives in the AgenticOS Infra item beside the existing Discord webhooks. The restricted Odoo API key for the Grove Agents user follows the same pattern. Read-only Stripe keys for the balance import are assumed to follow it too (my call); if not, income stays on file import. See section 15.

## 13. Weather

Source: the National Weather Service public API (api.weather.gov), keyless, hourly and seven-day forecast plus observations for the farm's coordinates (Armstrong Rd site; builder reads the exact point from the site records).

Task conditions, a fixed list on the task: any weather, no rain that day, N dry days since last rain (default N = 2, editable per project), above freezing overnight, indoor.

Nightly job:

1. Fetch forecast and yesterday's observations. Write observed rain and temperature to each active build project's daily log.
2. For every open task with a condition, check the forecast window. If the task's planned day breaks the condition, move it to the next day that satisfies it, shift dependents, and leave a chatter note saying why. If a move would cross a hard date on the project (the hoophouse arrival), flag instead of move.
3. Beyond the seven-day window, pad durations by the historical average of rain days per week for the month at this location, so the project end date is always the padded one and tightens as forecasts arrive.
4. If overnight lows stay below freezing across the window after a phase marked "stop at freeze" is done, move the remaining outdoor phases to a spring placeholder and mark the project "paused for winter."

A mid-day run at 1pm catches rain that arrived early and moves that day's remaining outdoor tasks.

## 14. Module list

Custom (grove-odoo-modules):

- `grove_schedule_f`: chart fixture with line numbers and tags, cash-date query, MIS templates, reconciliation-model seeds, year-end package views.
- `grove_ops`: aspects as projects with analytic accounts, task conditions and weather job, daily planner, week approval (if not ported), equipment-to-asset link, employee aspect coverage, worker/lead/owner groups and record rules, generated packing summary, sweat-equity report.
- `grove_discord_bot` (service, not an Odoo module): 7am, mid-day, 5pm, evening posts; parser tiers A and B; XML-RPC writes.

Core Community: account, analytic, project, hr, hr_timesheet, hr_holidays, hr_expense (receipts), purchase (vendor bills with commitments), maintenance, sms (for the optional SMS mirror).

OCA on 19.0, verified 2026-11-05 against the branch trees: account_statement_import_file, account_statement_import_ofx, account_statement_import_sheet_file, account_statement_import_online, account_statement_import_online_stripe, account_reconcile_oca, account_asset_management, account_budget_oca, account_financial_report, mis_builder, mis_builder_budget, project_timeline, web_timeline, project_milestone_status, project_task_code, project_task_pull_request, project_task_material, project_hr_expense_analytic, hr_timesheet_time_control, maintenance_plan, maintenance_equipment_status, maintenance_timesheet, maintenance_request_purchase, maintenance_project, web_responsive, web_pwa_customize, web_notify, queue_job (for the weather and planner jobs if they outgrow cron).

OCA on 18.0 only, port or replace: hr_timesheet_sheet, project_budget (replaced by account_budget_oca plus mis_builder_budget).

Skipped: MedELHattab/odoo-construction, vertelab/odoo-project_scrum, OCA/project-agile, OCA/field-service (revisit only if dispatch or skills matching is needed), paid apps.

## 15. Phasing

Phase 1, Train 5 (books, deadline-driven): nursery journals, Schedule F chart on farm and nursery, income accounts on products and POS and Stripe provider journals so every sale channel posts, the Grove Agents user and group, bank import modules, reconciliation screen, reconciliation-model seeds, the Enterprise analytic plan and the monthly mirror script (6.9), intercompany and owner-draw accounts, the rent invoice and bill, Penny loop, 2026 backfill (bank lines with enterprise tags, and income by channel), asset module. Done means: October 2026 closes in Odoo with Penny's loop, the mirror has run for every closed month, and each company's Schedule F report reads from its own books.

Phase 2, Train 5 or 6 (people and projects): users, employees per company, groups, hr_timesheet, time off, aspects as projects, the footpad project with its seven milestones and budget, timeline view, board view, week approval. Done means: the footpad's remaining phases are tracked in Odoo with hours and bills against budget.

Phase 3, Train 6 (the loop): Discord bot with Tier A parsing, 7am and 5pm and evening posts, daily planner, generated packing summary. Done means: Wesley's week is captured entirely through Discord and approved on Monday.

Phase 4, Train 6 or 7 (weather and maintenance): NWS job, task conditions, auto-reschedule, winter pause, maintenance register with preventive plans, equipment-to-asset link, reminders in the morning summary.

Phase 5, later: Tier B free-text parsing, online bank connector, SMS mirror, farmOS feed, field service if ever needed.

## 16. Decisions made in brainstorming (Josh's unless marked)

- Odoo as single source of truth; separate apps that talk through the spine (Josh).
- Backfill 2026 now (Josh).
- Farm hands log on their own phones; the 5pm Discord check-in is the primary capture (Josh).
- Owner-operators, no wages; labor costed at the co-op uniform rate on the analytic side only (Josh).
- Penny does categorization and the close, humans approve (Josh).
- Lead's home screen on a project is the board, timeline second (Josh, clicked in mockup).
- 7am plan and 5pm ask in a private channel per farm hand; evening digest with help and time-off requests in the crew channel (Josh).
- Weather moves tasks automatically with a note, flags instead of moving across hard dates (my call, from Josh's automation preference).
- Two drying days default after rain (my call, from the footpad doc; editable).
- Skip odoo-construction, vertelab scrum, field service for now (my call).
- File import before online bank sync (my call).
- Deterministic planner first, agents propose later (my call, from the Paperclip queue constraint).
- Aspect list in section 8.1 (my first draft, Josh edits).
- One authoritative income source per channel, gross, fees as expense; every sale channel posts from Phase 1 (my call, after review).
- Agents act as a dedicated restricted Odoo user; reconciliation models start propose-only (my call, after review).
- Workers can see and validate the transfers they pack, nothing else in sales (my call, after review).
- Farm and nursery are two tax returns; one shared bank in 2026, two in 2027 (Josh, 2026-11-05).
- 2026 booked in the farm company with a mandatory Enterprise analytic plan; monthly mirror entry gives the nursery its own books; personal to owner draw (my call, section 6.9).
- The only intercompany charge is rent from the nursery to the farm, which covers tractor hours, shared machines and shared labor; nothing else is charged between companies (Josh, 2026-11-05).
- Discord bot token approved; credentials live in the AgenticOS Infra item as before (Josh, 2026-11-05). Tier A parsing first, no language-model key now (my call).
- Approved hours in Odoo are the legal sweat-equity record; Josh's app-build hours count (Josh, 2026-11-05). Platform hours attributed to the co-op (my call).
- Any owner-operator approves weeks and time off, never their own; approvals and the register are visible to every member (Josh, 2026-11-05).
- Uniform rate $21 per hour in all three companies; nobody paid until profitability (Josh, 2026-11-05).

## 17. Open questions for Josh

None. All six were answered on 2026-11-05 and are recorded in section 16 and folded into 6.3, 6.9, 11.2 through 11.5 and 12.2.

## 18. Left to the builder

- Exact Schedule F line map and account codes, checked against current IRS instructions.
- The cash-date query and its handling of partial payments and refunds.
- The monthly mirror entry: its reversal, how it treats a nursery line re-tagged after the month was mirrored, and the year-end intercompany statement for the CPA.
- Whether to port hr_timesheet_sheet to 19.0 or build the minimal week model.
- Planner rule details and the hours-fit algorithm.
- NWS endpoints, caching, and the historical rain-day table source.
- Discord bot framework, hosting in odoocker, and the bot user's Odoo permissions.
- Record rules for the worker group and tests proving the scope.
- Data fixtures: aspects, tags, conditions, the footpad project seed, maintenance plans for the known equipment.
