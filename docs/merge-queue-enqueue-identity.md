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

## Provisioning the App identity (one-time, human step)

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

## Verifying after provisioning

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
