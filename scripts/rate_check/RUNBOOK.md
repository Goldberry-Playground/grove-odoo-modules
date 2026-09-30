# rate-check — operations runbook

The `rate-check` workflow (`.github/workflows/rate-check.yml`) runs daily at
07:00 ET, quotes **Pirate Ship** (public rate calculator, no auth — GOL-2270)
for the shipping zones, and — when rates have drifted ≥ $1 — force-pushes
`chore/rate-check` and opens/refreshes a PR against `main`. Each published cell
records the winning carrier/service (`shipping_rates.json` `_schema` 3); the
Odoo loader reads `base` only. Shippo is retired from quoting (spec
`docs/superpowers/specs/2026-09-09-pirateship-fulfillment-design.md` §A). No
`SHIPPO_API_KEY` is needed anymore.

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

**A red freshness verdict escalates through the CI failure router, not Discord.**
`DISCORD_OPS_WEBHOOK_URL` does not exist in this repo (GOL-2642), so every Discord
step in this workflow short-circuits to a no-op. The router files the red run as a
GitHub issue, which reaches Paperclip. Note the dedupe key is
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

**Fix.** The workflow now uses `secrets.RATE_CHECK_PR_TOKEN` for BOTH the
`actions/checkout` (so `git push` fires a real `synchronize`) and the PR
create/edit step (so `opened` fires). It falls back to `GITHUB_TOKEN` when the
secret is absent, so the change is a no-op until the secret is provisioned — no
regression, but the daily wedge persists until then.

## Provisioning `RATE_CHECK_PR_TOKEN` (one-time, human step)

This is the arming step. It requires adding a repo Actions secret, which the ops
service account cannot do — Josh / CEO must run it.

Preferred: a **fine-grained PAT** on a bot account (not a human seat), scoped to
**only** `Goldberry-Playground/grove-odoo-modules`:

- Repository access: only `grove-odoo-modules`
- Permissions: **Contents: Read and write**, **Pull requests: Read and write**
- Expiry: set a calendar reminder to rotate before it lapses (fine-grained PATs
  expire; max 1 year).

Then add it as a repo secret:

```bash
gh secret set RATE_CHECK_PR_TOKEN \
  --repo Goldberry-Playground/grove-odoo-modules \
  --body '<the-fine-grained-PAT>'
```

Alternative (more hardened, no user seat, short-lived tokens): a **GitHub App**
installed on the repo with the same two permissions, minted in-workflow via
`actions/create-github-app-token`. Requires two secrets (`APP_ID`,
`APP_PRIVATE_KEY`) instead of one PAT; adopt this if/when the org standardises on
an App identity for bot PRs (the org already uses `agenticos-developer[bot]`).

## Verifying the fix after provisioning

1. Manually dispatch: `gh workflow run rate-check.yml -R Goldberry-Playground/grove-odoo-modules`
   (or wait for the next 07:00 ET cron), on a day where rates drift ≥ $1.
2. Confirm the `chore/rate-check` PR head SHA gets a fresh `ci.yml` run with
   `event=pull_request` and `conclusion=success` (not `action_required`), and the
   required checks report. `gh pr checks chore/rate-check -R Goldberry-Playground/grove-odoo-modules`.
3. The PR should reach `mergeable_state: clean` without any manual re-trigger.
