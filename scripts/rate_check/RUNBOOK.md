# rate-check — operations runbook

The `rate-check` workflow (`.github/workflows/rate-check.yml`) runs daily at
07:00 ET, quotes Shippo for the shipping zones, and — when rates have drifted
≥ $1 — force-pushes `chore/rate-check` and opens/refreshes a PR against `main`.

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
