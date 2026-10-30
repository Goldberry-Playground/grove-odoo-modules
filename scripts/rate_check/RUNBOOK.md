# rate-check — operations runbook

The `rate-check` workflow (`.github/workflows/rate-check.yml`) is designed to
run daily at 07:00 ET (**schedule removed 2026-10-14 — see "Quote source
unavailable" below; `workflow_dispatch` only until it is restored**), quotes
**Pirate Ship** (public rate calculator, no auth — GOL-2270)
for the shipping zones, and — when rates have drifted ≥ $1 — force-pushes
`chore/rate-check` and opens/refreshes a PR against `main`. Each published cell
records the winning carrier/service (`shipping_rates.json` `_schema` 3); the
Odoo loader reads `base` only. Shippo is retired from quoting (spec
`docs/superpowers/specs/2026-09-09-pirateship-fulfillment-design.md` §A). No
`SHIPPO_API_KEY` is needed anymore.

## Ops secrets — what fails silently without them

This workflow has one integration that **degrades quietly** rather than fail, so
a missing secret is invisible unless you go looking. The first step of the job
(`Ops-alerting preflight`) now reports it as a `::warning::` plus a line in
the run summary, and never fails the run.

| Secret | Scope | Without it |
|---|---|---|
| `DISCORD_OPS_WEBHOOK_URL` | repo (or org) secret, the shared ops webhook | All three Discord alerts — rate drift, "regenerated table FAILED its invariants, PR held as DRAFT", and "rate-check failed, rates NOT updated" — short-circuit and send nothing. The money-path rate alarm surfaces only as a red CI badge plus a router-filed GitHub issue. |

**GOL-2642, resolved 2026-10-30.** Until 2026-09-29 a live read of the repo
secrets showed `DISCORD_OPS_WEBHOOK_URL` had never existed here, so every Discord
alert this workflow attempted was a no-op. On 2026-10-30 Josh set it as a repo
secret pointing at the **shipping/logistics** Discord channel (this repo only
uses it for `rate-check.yml` and `pirateship-query-drift.yml`). The same name in
`odoocker-goldberrygrove` and on prod Odoo is the infra/ops channel, a different
webhook.

If the secret is ever removed, the preflight step warns again. To re-provision:

```bash
gh secret set DISCORD_OPS_WEBHOOK_URL -R Goldberry-Playground/grove-odoo-modules
```

The value is stored as `DISCORD_SHIPPING_WEBHOOK_URL` on `Grove Prod/odoocker` in
1Password.

## Calibrating the packed-weight model (GOL-3201)

The rate feed prices each box at an **estimated actual billable weight**, not the
full-capacity worst case — packed weight moves the carrier price as much as
geography (the shipped small boxes ran 6.5–14.5 lb, most at 6.5). That weight
lives in `grove_headless/models/shipping_boxes.py` as `WEIGHT_CALIBRATION`
(per box: `samples` + `typical_billable_lb`) and the `weight_basis`
(`verified` / `unverified`) it drives. A box is priced at its calibrated typical
weight only when it is **verified** (≥ `MIN_CALIBRATION_SAMPLES` recorded shipped
weights and not in `FORCE_CONSERVATIVE_BOXES`); every other box keeps the
conservative full-capacity weight so an unmeasured box is never undercharged.

`calibrate_weights.py` reports predicted vs. recorded weight per box so a human
can decide whether to move `WEIGHT_CALIBRATION` in a reviewed PR. **It never
writes anything live** — same contract as the table refresh. Run it before each
refresh PR against a JSON of recorded `grove.label.batch.line.weight_lb`
(grouped by box, or GOL-3200's `golden_labels_*.json` directly):

```bash
python3 scripts/rate_check/calibrate_weights.py --samples <weights.json> [--json]
# exit 0 = no change needed; 3 = refresh PR recommended (prints a pasteable
# WEIGHT_CALIBRATION fragment); 2 = bad input.
```

When samples carry a per-box tree `count`, the report adds a least-squares fit
of `tare_total` + `per_tree_lb` (what `PER_TREE_LB` / tare should move to);
without counts it reports the empirical median (the carrier bills `ceil()` of
it). Apply the printed fragment by hand in a PR — the script is the evidence,
the PR is the change.

## Is the table still trustworthy? (freshness guard, GOL-2641)

`rate-check` answers *"can we reach a rate source right now"*. That is **not**
the money question, which is *"how long have we been billing customers off
numbers nobody re-verified"*. The two need separate alarms: while a quote source
is unavailable `rate-check` is red **every day** and carries no new information,
so a genuinely fossilizing table hides inside a wall of identical failures.

Every rewrite now stamps provenance into `shipping_rates.json`:

```json
"_rates_verified_on": "2026-09-21",
"_rates_source": "pirateship"
```

Both are underscore keys, so `shipping_zones._load_rates` filters them and
neither can ever move a published rate.

`scripts/rate_check/staleness.py` reads the stamp and needs **no network and no
secret**:

```bash
python3 scripts/rate_check/staleness.py            # the shipped table, today
python3 scripts/rate_check/staleness.py --today 2026-10-19 --max-age-days 28
```

| verdict | exit | meaning |
|---|---|---|
| `fresh` | 0 | younger than `--warn-age-days` (default 14) |
| `aging` | 0 + `::warning::` | past warn, not past fail — refresh before it goes red |
| `stale` | 1 + `::error::` | past `--max-age-days` (default 28); assume under-billing |
| `unstamped` | 1 + `::error::` | real published rates with no usable stamp — freshness is never *assumed* |
| `provisional` | 0 | the `_provisional` placeholder / empty table isn't real pricing |

Defaults are 14/28 because a UPS/USPS general rate increase is a few percent,
which on a $20–$27 parcel already clears the `$1` drift threshold. The value of
this check is the **green → red transition on a dated deadline** — something a
permanently-red `rate-check` cannot give you.

### Where it runs (GOL-2646)

The daily `rate-check` job runs it, in two steps that bracket the probe:

1. **`Rate table freshness (report)`** — the *first* step, before the Pirate Ship
   probe. It needs no network and no secret, so it still produces a dated verdict
   on the mornings the probe dies early. It records the exit code and **never
   fails the job itself**, so the probe's exit code, its `chore/rate-check` PR and
   its alerts are untouched.
2. **`Rate table freshness (enforce)`** — the *last* step, `if: always()`. It
   re-asserts the recorded verdict: `stale`/`unstamped` → the job is red. It is
   last because every other step carries an implicit `success() &&` — failing
   earlier would skip a legitimate drift PR, and a table can be stale *and*
   drifted on the same day. A missing/blank verdict is treated as **BROKEN**
   (red), never as fresh.

**A red freshness verdict escalates through the CI failure router and Discord.**
The Discord steps post to the shipping channel (`DISCORD_OPS_WEBHOOK_URL`, set
2026-10-30, GOL-2642), and the router files the red run as a GitHub issue, which
reaches Paperclip. Note the dedupe key is
`d3-ci-failure:rate-check:main`, which is shared with the quote-source failure — so
while the source is down the freshness escalation lands as another comment on that
same open issue. Read the run summary for which alarm actually fired.

To check by hand at any time, without waiting for the schedule:

```bash
gh workflow run rate-check -f dry_run=true   # probe is read-only; freshness still reported
python3 scripts/rate_check/staleness.py      # or just this, locally — no network
```

## Refreshing the table without a quote source (GOL-2641)

When the quote source is unavailable, **do not hand-edit
`shipping_rates.json`.** A direct edit bypasses all three safety gates: the
`ceil(quote + per-box packaging + $2.00)` target formula, the monotonicity guard,
and the `$1` drift gate — on 20 cells, by hand.

Instead, record the **raw carrier quotes** and let the script do the maths:

1. `cp scripts/rate_check/manual_quotes.example.json /tmp/quotes.json`
   (the template is complete but deliberately *not* applyable as-is — zero
   quotes, placeholder date — so it can never stamp the table fresh with no real
   numbers behind it).
2. `python3 scripts/rate_check/probe_states.py` prints each zone's reference
   corner and every box's geometry + representative billable weight.
3. For each of the 20 zone × box cells, read the least-cost **allowlisted
   ground** quote (UPS Ground `03` / UPS Ground Saver `93` / USPS Ground
   Advantage `GroundAdvantage`, residential) and record `quote` plus the
   `carrier` / `service` / `service_title` you read it off. All four are
   required per cell: schema 3 exists so "which carrier set this rate" is always
   answerable, and `rate_feed` shows `service_title` in storefront copy.
4. Set `_quoted_on` to the date you actually read the quotes — it becomes
   `_rates_verified_on`, so it must not be the date you happened to run the
   script.
5. Dry-run, then apply:

```bash
python3 scripts/rate_check/rate_check.py --manual-quotes /tmp/quotes.json --dry-run
python3 scripts/rate_check/rate_check.py --manual-quotes /tmp/quotes.json
```

Then open a normal PR with the rewritten `shipping_rates.json` — the
monotonicity and zone invariants gate it in CI exactly as they gate an automated
run (`python3 -m pytest scripts/rate_check/tests/ -q`).

Exit codes for this path: `0` no material drift (or `--dry-run`) · `2` bad input
(contradictory flags, an incomplete table, a missing/zero quote, a bad
`_quoted_on`) · `3` table rewritten · `4` monotonicity violation. A **partial**
hand refresh is refused (exit 2) rather than published: a dropped cell falls back
in the Odoo loader, which is an under-charge.

## GOL-2114: required checks wedge at `action_required`

**Symptom.** Every morning the rate-check PR sits at `mergeable_state: blocked`
with the required checks (Lint Python, Validate Module Manifests, Validate Tenant
Slug Map, …) missing, until a human or App identity pushes an empty re-trigger
commit.

**Cause.** GitHub deliberately does **not** run `on: pull_request` / `on: push`
workflows for events produced by the automatic `GITHUB_TOKEN` (anti-recursion).
The old workflow pushed the branch and opened the PR with `GITHUB_TOKEN`, so the
`opened` / `synchronize` events never triggered the required checks. Proof:
`ci.yml` runs on `chore/rate-check` show `event=pull_request action_required`
for `github-actions[bot]` pushes and `success` for `agenticos-developer[bot]` /
human pushes.

**Fix (2026-10-14, replaces the PAT design).** The workflow now starts the
required checks itself: after the force-push and PR create/edit it runs

```
gh workflow run ci.yml --ref chore/rate-check
```

with the ambient `github.token` (`permissions: actions: write`). GitHub's
suppression rule has one documented exception — `workflow_dispatch` /
`repository_dispatch` — and a dispatched `ci.yml` run at the branch ref reports
its check-runs on the **same head SHA** the PR points at, so the five required
contexts (`Lint Python`, `Validate Module Manifests`, `Validate Tenant Slug Map`,
`Pure-Python unit tests`, `Install + test module against Odoo 19`) satisfy the
ruleset with no human re-trigger. `ci.yml` carries a `workflow_dispatch:` trigger
for exactly this (every job in it is a read-only lint/validate/test). This is the
pattern odoocker-goldberrygrove already runs for its reconcile PRs (GOL-2504,
#728 + #2519): on odoocker #878 the GITHUB_TOKEN-triggered `CI` run was held
while the `workflow_dispatch` run on the same SHA completed `success`.

The earlier `RATE_CHECK_PR_TOKEN` design (a fine-grained PAT used for checkout +
PR create, PR #182) was **withdrawn** on Josh's direction (GOL-2116, 2026-09-23:
"don't provision a PAT"). The secret was never created, every `secrets.… ||
github.token` fallback in the workflow was dead, and the preflight warned about
it daily. All of that is removed; the PR author stays `github-actions[bot]`,
which is what keeps `auto-approve.yml` from auto-merging a money-adjacent rate
change (it classifies by author — an agent-App author would be auto-merged).

**Fail-soft.** The dispatch step never fails the run: the PR is already pushed
and open, so a failed dispatch only costs a reviewer the manual re-run that was
the status quo. It surfaces as a `::warning::` plus a run-summary line, and the
`Missing required-check sweep` (GOL-1958) still files an issue if a required
context is absent past its grace window.

## Verifying the self-dispatch (after the workflow change merges)

1. On a day where rates drift ≥ $1 (or dispatch `rate-check.yml` by hand with
   a quote source available — see GOL-2605 below while Pirate Ship is closed),
   confirm the run summary shows `Required checks self-dispatched: ci.yml @
   chore/rate-check`.
2. `gh run list -R Goldberry-Playground/grove-odoo-modules -w ci.yml -b chore/rate-check`
   should show a run with `event=workflow_dispatch`, actor `github-actions[bot]`,
   `conclusion=success`, on the PR's head SHA.
3. `gh pr checks chore/rate-check -R Goldberry-Playground/grove-odoo-modules`
   reports all five required contexts, and the PR reaches
   `mergeable_state: clean` (or `blocked` only on the human review) with no
   manual re-trigger.

If the required checks are still missing, re-run them by hand:
`gh workflow run ci.yml --ref chore/rate-check -R Goldberry-Playground/grove-odoo-modules`.

## Quote source refused the query (GOL-2605)

**Symptom.** Every morning's run fails in the `Run rate-checker` step with a
wall of identical lines and then the all-missing guard:

```
pirateship error for zone_1/small @ Wilmington,NC: pirateship graphql errors:
  [{'message': 'This server only executes persisted queries.'}]
  ... (once per zone x box x corner)
Service visibility — allowlisted ground rates returned by Pirate Ship:
  UPS 03 (UPS Ground): 0/20 probe(s)  <-- NEVER RETURNED
```

**What it means.** Pirate Ship switched `https://ship.pirateship.com/api/graphql`
to Apollo's *persisted-queries-only* mode on **2026-09-29**. The endpoint answers
HTTP **200** and refuses any query document it has not pre-registered — which is
every document a third party can send, including the `RatesQuery` this checker
reverse-engineered from the web app (see the `RATES_QUERY` comment). The checker
now detects this class (`is_source_closed`) and says so explicitly:

```
::error::Pirate Ship REFUSED the RatesQuery document on N probe corner(s) ...
```

**This is not a transient and not a workflow fault.** Verified reproducible from
outside CI with a bare `POST` to the same URL. There is nothing to retry:

- a persisted-query allowlist is a *deliberate* block on third-party queries;
- replaying the web app's persisted-query hash would be defeating that control
  and would break again on Pirate Ship's next front-end deploy. **Do not do it** —
  it needs a business/vendor decision, not a code change.

**Blast radius while it is down.** None today, growing with time:

- `grove_headless/data/shipping_rates.json` is **left untouched** — the run exits
  1 *before* any rewrite, so published rates stay the last real probed values
  (last rewritten `2026-09-21`, commit `4a6ace5`). Checkout is orders-of-magnitude
  safer than a zeroed or partially-published table.
- Rates **fossilize** from here. A UPS/USPS increase silently under-bills every
  ship-to order, which is the exact failure GOL-1312's guard exists to surface.
  Treat weeks-scale staleness as a pricing incident, not a CI annoyance.

**Do not "fix" the red by silencing the probe.** Exit 1 here *is* the alarm.

**The `schedule:` cron is removed while the source is down (GOL-2605, Josh
2026-10-14).** The original posture was to leave the daily run as the cheapest
"is the source back?" probe (the CI failure router dedupes on
`<!-- d3-ci-failure:rate-check:main -->`), but after 15 consecutive identical
reds the daily re-fire cost more attention than it returned and no "it is back"
is expected: Pirate Ship's persisted-queries-only mode is deliberate, and the
sanctioned way forward is the Train #4 keyless persisted `RatesQuery` client
(gom#347). The cron lines are kept, commented, in the workflow with the
re-enable condition next to them. **Re-enable only when both hold:**

1. the gom#347 step-1 client (`RATES_QUERY_HASH` pin, watched weekly by
   `pirateship-query-drift.yml`) is merged to main, and
2. a manual `workflow_dispatch` with `dry_run=true` on main exits 0.

While the cron is off, the freshness guard (`staleness.py`, warn 14 d / fail
28 d) only runs on dispatch — refresh the table by hand on the cadence below and
keep `_rates_verified_on` current.

**Restoring automation requires a replacement quote source** (board decision —
money path). The three live options, with the trade-off that matters:

| Option | Cost / effort | Catch |
|---|---|---|
| Quote via **Shippo** again (`SHIPPO_API_KEY` is already provisioned in this repo) | Lowest — the pre-`GOL-2270` quote path is `git show 1207093^:scripts/rate_check/rate_check.py` | Reintroduces quote/purchase divergence: labels are bought on Pirate Ship, so a Shippo quote is no longer the rate we pay (the whole reason GOL-2270 moved quoting) |
| Carrier APIs direct (**UPS** + **USPS** developer APIs) | Highest — two auth flows, two rate schemas, new secrets | Published carrier rates, not Pirate Ship's discounted rates → quotes come out *high*, overcharging unless a discount factor is modelled |
| Ask Pirate Ship for sanctioned API / rate-card access | Unknown — a support request | Historically no public API; may be a flat "no". Cheapest to *ask* and it is the only option with no divergence |

Until one lands, the rate table is maintained by hand: probe pirateship.com in a
browser and edit `shipping_rates.json` through a normal PR (the monotonicity and
zone invariants still gate it via `scripts/rate_check/tests/`).

**Verify a candidate fix offline, no vendor calls:**

```bash
python3 -m pytest grove_headless/tests/ scripts/rate_check/tests/ -q
python3 scripts/rate_check/rate_check.py --fixture \
  scripts/rate_check/fixtures/pirateship_rates_persisted_only.json ; echo "exit=$?"
# expect exit=1 plus the ::error:: refusal annotation (never a rewrite)
```
