# Merge-queue enqueue identity — operations runbook (GOL-2524)

## Symptom

A pull request is **approved, mergeable, and every required check is green** —
and it does not merge. It enters the merge queue, sits at `AWAITING_CHECKS`, and
roughly thirty minutes later is silently ejected, still open, with no red check,
no comment, and no notification. Nothing in the PR's own checks looks wrong,
because nothing about the PR *is* wrong.

The tell is the merge group, not the PR: the merge-group commit has **zero
workflow runs**.

```bash
REPO=Goldberry-Playground/grove-odoo-modules
# Who enqueued, and what is the merge-group commit?
gh api graphql -f query='{repository(owner:"Goldberry-Playground",name:"grove-odoo-modules"){
  mergeQueue(branch:"main"){entries(first:10){nodes{
    position state enqueuedAt enqueuer{login} headCommit{oid} pullRequest{number}}}}}}'
# Zero here on an AWAITING_CHECKS entry older than ~2 min == dead group.
gh api "repos/$REPO/actions/runs?head_sha=<GROUP_COMMIT_OID>" -q '.total_count'
```

## Cause

GitHub never creates workflow runs for events triggered by the automatic
`GITHUB_TOKEN`. This is the documented anti-recursion rule — the same rule that
wedged the daily rate-check PR in GOL-2114 (see
`scripts/rate_check/RUNBOOK.md`), applied to a different event class.

With a merge queue on `main`, `gh pr merge --squash [--auto]` against an
already-green PR does not merge it — it **enqueues** it. When `auto-approve.yml`
made that call with `GITHUB_TOKEN`, the resulting `merge_group` event was
suppressed: no workflow ran on the `gh-readonly-queue/...` commit, so no required
check ever reported, so the entry waited out GitHub's ~30-minute
`checkResponseTimeout` and was ejected.

The failure is worst exactly where it should be best. A PR only reaches the
enqueue because the full-CI gate in `auto-approve.yml` already passed, so **the
healthier the PR, the more likely it silently fails to merge**. And because the
enqueue is also the *last* step, nothing downstream ever notices.

**Evidence — every merge-queue entry across all three repos on 2026-09-23. Same
commits, same required checks; the only variable was the enqueuing identity
(`AddedToMergeQueueEvent.enqueuer` on each PR's timeline):**

| repo / PR | enqueued by | result |
| --- | --- | --- |
| grove-sites #821 | `github-actions[bot]` 22:01:17Z | 0 runs, stuck **30 min**, dequeued by hand |
| grove-sites #821 | `agenticos-developer[bot]` 22:31:27Z | merge_group runs 10 s later → **merged 22:34:36Z** |
| odoocker #729 | `github-actions[bot]` 22:05:24Z | 0 runs in ~5.7 min, dequeued by hand |
| odoocker #729 | `agenticos-developer[bot]` 22:11:21Z | **merged 22:11:58Z — 37 s** |
| grove-odoo-modules #275 | `github-actions[bot]` 21:49:50Z, retried 21:57:25Z | 0 runs both times, ~17 min lost |
| grove-odoo-modules #275 | `agenticos-developer[bot]` 22:07:20Z | **merged 22:12:41Z** |
| grove-odoo-modules #277 | `github-actions[bot]` 22:13:30Z | 0 runs in ~3.8 min, dequeued by hand |
| grove-odoo-modules #277 | `agenticos-developer[bot]` 22:17:19Z | **merged 22:22:33Z** |

Four entries enqueued by `github-actions[bot]`: zero merge-group workflow runs,
zero merges. Four re-enqueued by the App: all four merged, the fastest in 37
seconds. Nothing else changed between the pairs.

(This also corrects an earlier note on #275 that "dequeue + requeue does not
fix it" — the timeline shows that retry was made by `github-actions[bot]` too,
so it was the same token, not a failed refutation.)

## Fix

`auto-approve.yml` mints a GitHub App installation token and uses it for the
enqueue call **only** — App-triggered events do create workflow runs. Approval,
review-thread resolution, and every read stay on `GITHUB_TOKEN`: least
privilege, and the approving identity is deliberately unchanged
(`github-actions[bot]`, distinct from the PR author, is what satisfies the
review requirement).

The App identity is **optional**. When `vars.MERGE_QUEUE_APP_CLIENT_ID` is unset the
mint step is skipped and the enqueue falls back to `GITHUB_TOKEN` exactly as
before — same graceful-degradation shape as `RATE_CHECK_PR_TOKEN` in GOL-2114.
No regression, but the wedge persists until the identity is provisioned.

So that the fallback is never *silent*, the same step carries a **wedge
detector**: once a queue entry has been `AWAITING_CHECKS` past a 180 s grace
window and its merge-group commit still has zero workflow runs, the auto-approve
run prints a full diagnosis (including the rescue commands below) and **fails**.
A red run two minutes in beats a silent ejection thirty minutes in. The detector
is conservative by design — any unreadable API, any other queue state, anything
inside the grace window, and it stays quiet, because a false positive would fail
a healthy PR's merge. `scripts/ci/merge-queue-wedge-detector.test.mjs` asserts
both directions against the real function extracted from the workflow.

## The fix that needs no provisioning: arm auto-merge under the App (GOL-3118 / GOL-3150)

**Auto-merge inherits the identity of whoever enabled it.** When auto-merge is
armed on a PR, GitHub performs the eventual enqueue attributed to the identity
that armed it — and that enqueue creates `merge_group` workflow runs normally.

So the enqueue identity does **not** have to come from inside the workflow. It
never did. Agents already hold a non-`GITHUB_TOKEN` identity: every agent PR is
authored by `agenticos-developer[bot]` using a broker-minted installation token
(`GH_TOKEN_BROKER_URL`). Arming auto-merge with that same token makes every
subsequent enqueue healthy — no Actions variable, no Actions secret, no App
private key anywhere.

**Evidence** (`AutoMergeEnabledEvent.actor` vs the resulting
`AddedToMergeQueueEvent.enqueuer`, grove-sites, 2026-09-30):

| PR | armed by | resulting enqueuer | enqueues needed |
| --- | --- | --- | --- |
| #921 | `agenticos-developer` 19:50:49Z | `agenticos-developer[bot]` 20:10:25Z | **1** → merged |
| #923 | `agenticos-developer` 20:12:22Z | `agenticos-developer[bot]` 20:15:45Z | **1** → merged |
| #933 | `agenticos-developer` 20:26:05Z | `agenticos-developer[bot]` 20:31:54Z | **1** → merged |
| #900 | `EngineeringMoonBear` 19:04:58Z | `EngineeringMoonBear` 19:12:20Z | **1** → merged |

#921's twenty-minute gap is the load-bearing detail: arming happened *before*
the checks were green, GitHub did the waiting, and the enqueue it made twenty
minutes later still carried the arming identity.

**This repo's own A/B.** 2026-09-23, same commits, same required checks; and
again on 2026-10-06 (#318), where the GOL-2524 wedge detector caught it loudly
at 09:16:07Z rather than letting the PR die silently 30 minutes in:

| PR | enqueued by | group commit | `merge_group` runs | result |
| --- | --- | --- | --- | --- |
| #275 | `github-actions[bot]` 21:49:50Z, retried 21:57:25Z | — | **0** both times | ~17 min lost |
| #275 | `agenticos-developer[bot]` 22:07:20Z | — | ran | **merged 22:12:41Z** |
| #277 | `github-actions[bot]` 22:13:30Z | — | **0** in ~3.8 min | dequeued by hand |
| #277 | `agenticos-developer[bot]` 22:17:19Z | — | ran | **merged 22:22:33Z** |
| #318 | `github-actions` 09:13:00Z (2026-10-06) | `8eed18c2` | **0** | wedge detector failed the run |
| #318 | `agenticos-developer` 09:18:30Z (manual GraphQL rescue) | `627f59f7` | **5** | merged |

Nothing changed between each pair but the enqueuing identity.

### Doing it

The steady state is to arm the PR in the same breath as opening it, with the
broker token already in hand. The sweep script is the backstop and the backlog
drain:

```bash
scripts/ci/merge-queue-arm-automerge.sh              # dry run, lists decisions
scripts/ci/merge-queue-arm-automerge.sh --apply      # arm already-APPROVED agent PRs
ARM_UNAPPROVED=1 scripts/ci/merge-queue-arm-automerge.sh --apply   # steady state
REPO=Goldberry-Playground/grove-sites scripts/ci/merge-queue-arm-automerge.sh --apply
```

It runs **from the agent box**, on a broker-minted App token — never from a
GitHub Actions job on the default `GITHUB_TOKEN`, which is the identity that
causes the wedge in the first place. That is why this fix is *not* wired into
`auto-approve.yml`: a workflow arming auto-merge under `GITHUB_TOKEN` would
rebuild exactly the dead group it is meant to prevent.

**The default (approved-only) mode cannot be the steady state.** By the time
`auto-approve.yml` has approved a PR it has *already* performed the enqueue on
`GITHUB_TOKEN` — which is the wedge. Arming only helps if it happens **before**
approval, so anything automated has to run `ARM_UNAPPROVED=1`. The
approved-only default is for draining a backlog by hand and for the case where
the enqueue has not happened yet.

Auto-merge is allowed on all three repos and all three have a merge queue on
`main` (re-measured 2026-10-06 — note REST `GET /repos/...` omits
`allow_auto_merge` for a token without admin read, which reads as "disabled";
GraphQL `autoMergeAllowed` is authoritative).

The script **bypasses no gate**: auto-merge still requires every required review
and every required status check, including the protected-paths human review that
`auto-approve.yml` withholds its approval for. Arming decides *who* enqueues,
not *whether* the PR may merge.

Two guardrails worth knowing, both in
`scripts/ci/merge-queue-arm-automerge.test.mjs`:

- **It never arms a PR it did not author.** `auto-approve.yml` approves
  maintainer PRs but deliberately does not enqueue them, so the human keeps
  control over when their own PR merges.
- **It never re-arms a PR someone else armed**, which would mean disabling their
  auto-merge first and silently taking a merge decision from its owner.

### Pre-approval arming and protected paths

`ARM_UNAPPROVED=1` is a semantic no-op for almost every agent PR:
`auto-approve.yml` was going to approve and enqueue it anyway, so pre-arming
changes only the enqueuing identity. There is exactly **one** class where it is
not a no-op — an agent PR that touches a **protected path**. There
`auto-approve.yml` hard-withholds its approval and a human reviews by hand, and
pre-arming would make that human's approval *be* the merge rather than a
reviewer approving and someone then deciding to enqueue.

So `ARM_UNAPPROVED=1` **skips unapproved protected-path PRs.** It evaluates them
against the **target repo's own** base-branch
`scripts/ci/protected-paths-carveout.mjs` — the same definition
`auto-approve.yml` withholds on, and read from the base branch so a PR cannot
edit the carve-out to un-protect itself. This repo's `PROTECTED_GLOBS` are
`.github/workflows/**`, `**/*payment*.py`, `**/*stripe*` and `**/*webhook*`, so a cross-repo sweep gets each repo's real list rather than
grove-sites'.

That is what makes `ARM_UNAPPROVED=1` **safe to automate with no board decision
attached.** `ARM_PROTECTED=1` is the separate, explicit override for the
protected class — do not set it without the board's sign-off.

It is **fail-closed**: a carve-out it cannot fetch, a `node` it cannot run, or a
truncated changed-file list all skip the unapproved PR. An already-APPROVED PR
is unaffected by any of it — its approval already happened, so there is no
approval-timing semantics left to change.

⚠️ The `files` connection caps `first` at **100**, and asking for more trips
`EXCESSIVE_PAGINATION` — which GitHub returns as a **200 with an `errors` array**
and a nulled field, so `curl -f` does not catch it and a partial response reads
like real data. Fail-closed holds, but silently, so the sweep logs GraphQL
`errors` and treats a missing `repository` as fatal.

## Provisioning the App identity in Actions (optional — not recommended)

> **Not required.** The section above fixes this with no credential at all.
> `vars.MERGE_QUEUE_APP_CLIENT_ID` being unset is now a supported resting state,
> not a pending chore. Kept here because `auto-approve.yml` still honours the
> variables if they ever appear, and because the reasoning should not have to be
> rediscovered. ADR-0001 keeps the App private key in `gh-token-broker` alone
> ("agents never see the root key"); `MERGE_QUEUE_APP_PRIVATE_KEY` would be the
> first long-lived root credential in Actions secrets, in three repos, to buy a
> capability the broker already gives away.

Adding Actions secrets/variables needs repo-admin rights the ops service account
does not have — **Josh / CEO must run this.**

Use the existing agent App, `agenticos-developer` (it already authors the agent
PRs, and it is the identity proven in the A/B above). From the App's settings
page take its **Client ID** and **generate a private key** (`.pem`).

Installation permissions required: **Contents: Read and write** and **Pull
requests: Read and write** — the App already has these in these repos.

```bash
REPO=Goldberry-Playground/grove-odoo-modules
gh variable set MERGE_QUEUE_APP_CLIENT_ID    --repo "$REPO" --body '<APP_CLIENT_ID>'
gh secret   set MERGE_QUEUE_APP_PRIVATE_KEY --repo "$REPO" < /path/to/app-key.pem
```

The client ID is a variable, not a secret — it is not sensitive, and keeping it
a variable is what lets the workflow's `if:` skip the mint step cleanly when the
identity is not provisioned. The minted token is scoped in-workflow to Contents
+ Pull requests write only, so it carries less than the App's full installation. Org-level (`--org Goldberry-Playground --visibility
selected`) covers `grove-odoo-modules`, `odoocker-goldberrygrove` and
`grove-sites` in one step; all three carry the same defect.

Prefer the App over a fine-grained PAT here: PATs expire (max 1 year) and this
code path degrades **silently** back into the bug when they do.

## Verifying the enqueue identity (after arming, or after provisioning)

1. Merge any agent PR normally and watch the auto-approve run: it should log
   `Auto-merge enabled for PR #N`, and no wedge diagnosis.
2. Confirm the enqueuer is the App, not `github-actions`:
   ```bash
   gh api graphql -f query='{repository(owner:"Goldberry-Playground",name:"grove-odoo-modules"){
     mergeQueue(branch:"main"){entries(first:5){nodes{enqueuer{login} headCommit{oid} state}}}}}'
   ```
3. Confirm the merge-group commit gets runs within ~60 s:
   ```bash
   gh api "repos/$REPO/actions/runs?head_sha=<GROUP_COMMIT_OID>" -q '.total_count'   # expect > 0
   ```

## Rescue procedure (works today, with or without the fix)

Re-enqueue the PR under a non-`GITHUB_TOKEN` identity. This **bypasses no gate**
— it makes the required checks actually run, which the dead group never did.

```bash
NODE_ID=$(gh pr view <PR> --repo "$REPO" --json id -q .id)
gh api graphql -f query="mutation{dequeuePullRequest(input:{id:\"$NODE_ID\"}){mergeQueueEntry{state}}}"
gh api graphql -f query="mutation{enqueuePullRequest(input:{pullRequestId:\"$NODE_ID\"}){mergeQueueEntry{position state enqueuer{login}}}}"
```

Run this with an App installation token or a PAT — **not** `GITHUB_TOKEN`, and
not from inside a workflow using the default token, or you simply build another
dead group. Confirm `enqueuer.login` is not `github-actions`, then check that the
new group commit has a non-zero run count within ~60 s.
