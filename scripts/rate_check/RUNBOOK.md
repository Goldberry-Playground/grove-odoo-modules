# rate-check — operations runbook

The `rate-check` workflow (`.github/workflows/rate-check.yml`) runs daily at
07:00 ET, quotes **Pirate Ship** (public rate calculator, no auth — GOL-2270)
for the shipping zones, and — when rates have drifted ≥ $1 — force-pushes
`chore/rate-check` and opens/refreshes a PR against `main`. Each published cell
records the winning carrier/service (`shipping_rates.json` `_schema` 3); the
Odoo loader reads `base` only. Shippo is retired from quoting (spec
`docs/superpowers/specs/2026-09-09-pirateship-fulfillment-design.md` §A). No
`SHIPPO_API_KEY` is needed anymore.

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

**Do not "fix" the red by silencing it.** Exit 1 here *is* the alarm. Likewise do
not pause the `schedule:` cron: the CI failure router dedupes on
`<!-- d3-ci-failure:rate-check:main -->`, so a still-broken source only adds a
"Failed again." comment to the one open issue, and the router auto-closes that
issue the moment a run goes green. The daily run is the cheapest possible probe
for "is the source back?".

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
