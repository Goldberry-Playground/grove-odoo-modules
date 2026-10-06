#!/usr/bin/env node
// protected-paths-carveout.mjs — Tier-0 carve-out for auto-approve (GOL-1406-A).
//
// Standalone, hand-maintained module. (GOL-2013 removed the protected-paths
// guard workflow and its single-source generator; this file used to be
// generated alongside the guard, but it now stands on its own. Only
// PROTECTED_GLOBS differs per repo — edit that list directly here.)
//
// Why this exists: branch protection requires a human review + dismisses stale
// approvals on push, but the agent auto-approve bot (auto-approve.yml) still
// stamps its OWN approving review on green agent PRs. Without this carve-out an
// agent PR touching `.github/workflows/**`, `infra/terraform/**`, etc. would be
// auto-approved + merged with no human in the loop. This script, called by
// auto-approve.yml before it stamps its approval, WITHHOLDS that approval
// whenever the PR's changed files intersect PROTECTED_GLOBS. Defense-in-depth,
// strictly one-way-tighter: it only ever withholds, never loosens author/size
// gates, so a sensitive-path PR falls back to a real human review.
//
// Contract (CLI):
//   env PR_FILES = newline-separated changed paths (`gh pr view --json files
//                  -q '.files[].path'`).
//   exit 0 -> no protected path touched; caller may proceed to approve.
//   exit 1 -> a protected path is touched; reason on stdout; caller WITHHOLDS.
// Fail-closed: the workflow runs this inside an `if` whose false branch
// withholds, so a throw / missing file / nonzero exit all withhold approval.
import { pathToFileURL } from "node:url";

// PROTECTED_GLOBS for THIS repo — the only thing that differs between repos.
//
// `.github/workflows/**` covers auto-approve.yml itself. It does NOT cover this
// file (`scripts/ci/...`) and it does NOT cover `.github/actions/**` —
// `globToRe('.github/workflows/**')` is `^\\.github\\/workflows\\/.*$`. Both are
// listed explicitly below; see GOL-3105. Protect the AUTHORITY, not the
// filename:
//   - `.github/actions/**` is a shared composite action, consumed as
//     `Goldberry-Playground/grove-sites/.github/actions/...@main` (a MOVING ref,
//     5 live `uses:` per repo) by grove-sites, odoocker-goldberrygrove and
//     grove-odoo-modules. A composite action runs inside the CALLING workflow's
//     job with that job's token and permissions, so editing it is
//     workflow-equivalent, and one merge retargets all three repos' CI at once.
//   - this file is the carve-out. auto-approve.yml runs the BASE-branch copy, so
//     a PR cannot edit it to approve ITSELF (GOL-1406-A) — but a PR that LOOSENS
//     this list was, until GOL-3105, itself auto-approvable, and every
//     subsequent agent PR would then run against the loosened list. One merge,
//     not one PR.
// The `.test.mjs` beside this file is deliberately NOT protected: it cannot
// change the gate's behaviour, so protecting it would add review friction and
// buy nothing.
export const PROTECTED_GLOBS = [
  '.github/workflows/**',
  '.github/actions/**',
  'scripts/ci/protected-paths-carveout.mjs',
  '**/*payment*.py',
  '**/*stripe*',
  '**/*webhook*',
];

// glob -> RegExp (supports **, *, and literals; '/' is literal).
export function globToRe(g) {
  let re = '^';
  for (let i = 0; i < g.length; i++) {
    const c = g[i];
    if (c === '*') {
      if (g[i + 1] === '*') {
        i++;
        if (g[i + 1] === '/') { i++; re += '(?:.*/)?'; }
        else { re += '.*'; }
      } else {
        re += '[^/]*';
      }
    } else if ('\\^$.|?+()[]{}/'.includes(c)) {
      re += '\\' + c;
    } else {
      re += c;
    }
  }
  return new RegExp(re + '$');
}

// The protected paths a change set touches (empty => safe to auto-approve).
export function protectedHits(files, globs = PROTECTED_GLOBS) {
  const matchers = globs.map(globToRe);
  return files
    .map((f) => (f || "").trim())
    .filter(Boolean)
    .filter((fn) => matchers.some((re) => re.test(fn)));
}

// CLI entrypoint — only when executed directly, not when imported by the test.
// pathToFileURL (not a naive `file://${path}`) so a script path needing
// URL-encoding still matches, mirroring automerge-gate.mjs.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const files = (process.env.PR_FILES || "").split("\n");
  const hits = protectedHits(files);
  if (hits.length === 0) {
    process.stdout.write("no protected path touched — carve-out clear");
    process.exit(0);
  }
  process.stdout.write(
    "protected path(s) touched, withholding auto-approval (GOL-1406-A carve-out): " +
      hits.join(", ") +
      " — needs a human maintainer's review under branch protection (stale approvals are dismissed on push)."
  );
  process.exit(1);
}
