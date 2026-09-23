#!/usr/bin/env node
// Behavioral test for the merge-queue dispatch check in
// .github/workflows/auto-approve.yml (GOL-2524) — `detect_queue_wedge` and its
// early-exit counterpart `queue_group_dispatched`.
//
// What it protects:
//   The detector is the only thing that makes a dead merge group LOUD. A dead
//   group has no red check and no notification — the PR is silently ejected 30
//   minutes later — so a detector that quietly stopped working would be
//   invisible, exactly like the bug it detects. It is also easy to break by
//   accident: the diagnosis is a shell heredoc inside a YAML block scalar, and
//   a single wrong indent turns it into a syntax error or, worse, a `run:` that
//   parses but never fires.
//
//   So this test extracts the real functions out of the real workflow file (no
//   copy of the logic lives here), runs them under bash against a stub `gh`, and
//   asserts BOTH directions: it fires on the dead-group signature, and it stays
//   quiet on every adjacent shape — a queue entry still inside the grace
//   window, a healthy group whose runs exist, a state that cannot be wedged, an
//   unreadable API. False positives matter as much as false negatives here: the
//   detector FAILS the auto-approve run, so crying wolf would block merges.
//
// node builtins only — run by the `CI scripts` job (scripts/ci/*.test.mjs).

import { mkdtempSync, writeFileSync, chmodSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const workflow = join(repoRoot, ".github", "workflows", "auto-approve.yml");

// ── Extract the block straight out of the workflow ─────────────────────────
// The `run:` body is a YAML block scalar indented by 10 spaces; strip exactly
// that so bash sees the script as the runner would. Doing it by text (rather
// than with a YAML parser) keeps this test on node builtins AND makes a broken
// heredoc indent show up as a bash failure below.
const BLOCK_INDENT = 10;
const START = "GOL-2524: merge-queue dispatch check";
const END = "end GOL-2524 merge-queue dispatch check";

const lines = readFileSync(workflow, "utf8").split("\n");
const start = lines.findIndex((l) => l.includes(START));
const end = lines.findIndex((l, i) => i > start && l.includes(END));
if (start < 0 || end < 0) {
  console.error(`FAIL: could not locate the wedge detector in ${workflow} (start=${start}, end=${end}).`);
  console.error("If the detector was renamed or removed, update or delete this test deliberately.");
  process.exit(1);
}
const block = lines
  .slice(start, end)
  .map((l) => (l.length > BLOCK_INDENT ? l.slice(BLOCK_INDENT) : l.trimStart()))
  .join("\n");

// ── Stub `gh`: answers from fixture env vars, never touches the network ────
const dir = mkdtempSync(join(tmpdir(), "wedge-test-"));
const bin = join(dir, "bin");
spawnSync("mkdir", ["-p", bin]);
const ghStub = `#!/usr/bin/env bash
case "$1 $2" in
  "api graphql") [ "\${FX_ENTRY_FAIL:-0}" = "1" ] && exit 1; printf '%s' "\${FX_ENTRY:-}" ;;
  "pr view")     printf '%s' "\${FX_PR_HEAD:-}" ;;
  "api "*)       [ "\${FX_RUNS_FAIL:-0}" = "1" ] && exit 1; printf '%s' "\${FX_RUNS:-}" ;;
esac
exit 0
`;
writeFileSync(join(bin, "gh"), ghStub);
chmodSync(join(bin, "gh"), 0o755);

const iso = (secondsAgo) => new Date(Date.now() - secondsAgo * 1000).toISOString().replace(/\.\d+Z$/, "Z");
const AGED = iso(600); // well past the 180s grace window
const FRESH = iso(10);
const entry = (state, at, enqueuer, groupSha) => [state, at, enqueuer, groupSha].join("\t");

/** Runs one of the extracted functions; `ok` mirrors its exit status. */
function run(fn, fixtures) {
  const script = [
    "set -uo pipefail",
    `export PATH="${bin}:$PATH"`,
    'OWNER=test-owner; NAME=test-repo; PR=275; REPO="$OWNER/$NAME"',
    block,
    fn,
  ].join("\n");
  const res = spawnSync("bash", ["-c", script], {
    encoding: "utf8",
    env: { ...process.env, ...fixtures },
  });
  if (res.status !== 0 && res.status !== 1) {
    throw new Error(`${fn} exited ${res.status} (bash error?):\n${res.stderr}`);
  }
  return { ok: res.status === 0, stdout: res.stdout };
}
const detects = (fixtures) => {
  const { ok, stdout } = run("detect_queue_wedge", fixtures);
  return { wedged: ok, stdout };
};

const HEAD = "aaaaaaaaaaaaaaaa";
const GROUP = "cbdbfedd04321111";

const cases = [
  ["dead group: aged, AWAITING_CHECKS, zero runs", true,
    { FX_PR_HEAD: HEAD, FX_RUNS: "0", FX_ENTRY: entry("AWAITING_CHECKS", AGED, "github-actions", GROUP) }],
  ["still inside the grace window", false,
    { FX_PR_HEAD: HEAD, FX_RUNS: "0", FX_ENTRY: entry("AWAITING_CHECKS", FRESH, "github-actions", GROUP) }],
  ["healthy group: merge_group runs exist", false,
    { FX_PR_HEAD: HEAD, FX_RUNS: "6", FX_ENTRY: entry("AWAITING_CHECKS", AGED, "agenticos-developer", GROUP) }],
  ["QUEUED: merge group not formed yet", false,
    { FX_PR_HEAD: HEAD, FX_RUNS: "0", FX_ENTRY: entry("QUEUED", AGED, "github-actions", GROUP) }],
  ["MERGEABLE: checks already reported", false,
    { FX_PR_HEAD: HEAD, FX_RUNS: "0", FX_ENTRY: entry("MERGEABLE", AGED, "github-actions", GROUP) }],
  ["PR is not in the queue at all", false,
    { FX_PR_HEAD: HEAD, FX_RUNS: "0", FX_ENTRY: "" }],
  ["group commit == PR head (no merge commit built)", false,
    { FX_PR_HEAD: HEAD, FX_RUNS: "0", FX_ENTRY: entry("AWAITING_CHECKS", AGED, "github-actions", HEAD) }],
  ["GraphQL unreadable: stay quiet, never cry wolf", false,
    { FX_PR_HEAD: HEAD, FX_RUNS: "0", FX_ENTRY_FAIL: "1", FX_ENTRY: entry("AWAITING_CHECKS", AGED, "github-actions", GROUP) }],
  ["runs API unreadable: stay quiet", false,
    { FX_PR_HEAD: HEAD, FX_RUNS_FAIL: "1", FX_ENTRY: entry("AWAITING_CHECKS", AGED, "github-actions", GROUP) }],
  ["runs API returns nothing: stay quiet", false,
    { FX_PR_HEAD: HEAD, FX_RUNS: "", FX_ENTRY: entry("AWAITING_CHECKS", AGED, "github-actions", GROUP) }],
  ["unparseable enqueuedAt: stay quiet", false,
    { FX_PR_HEAD: HEAD, FX_RUNS: "0", FX_ENTRY: entry("AWAITING_CHECKS", "not-a-date", "github-actions", GROUP) }],
];

let failed = 0;
for (const [name, expected, fixtures] of cases) {
  const { wedged } = detects(fixtures);
  if (wedged === expected) {
    console.log(`ok   ${name}`);
  } else {
    console.error(`FAIL ${name}: expected wedged=${expected}, got ${wedged}`);
    failed++;
  }
}

// `queue_group_dispatched` is the early-exit signal, so its bias is the
// opposite of the detector's: it must only say "dispatched" when it has
// genuinely seen runs. Saying so wrongly would stop the watch loop early and
// let a dead group through unreported.
const dispatchCases = [
  ["dispatched: the group has runs", true,
    { FX_PR_HEAD: HEAD, FX_RUNS: "6", FX_ENTRY: entry("AWAITING_CHECKS", AGED, "agenticos-developer", GROUP) }],
  ["not dispatched: zero runs", false,
    { FX_PR_HEAD: HEAD, FX_RUNS: "0", FX_ENTRY: entry("AWAITING_CHECKS", AGED, "github-actions", GROUP) }],
  ["not dispatched: PR is not queued", false,
    { FX_PR_HEAD: HEAD, FX_RUNS: "6", FX_ENTRY: "" }],
  ["not dispatched: runs API unreadable", false,
    { FX_PR_HEAD: HEAD, FX_RUNS_FAIL: "1", FX_ENTRY: entry("AWAITING_CHECKS", AGED, "github-actions", GROUP) }],
  ["dispatched while still QUEUED (group already built)", true,
    { FX_PR_HEAD: HEAD, FX_RUNS: "2", FX_ENTRY: entry("QUEUED", FRESH, "agenticos-developer", GROUP) }],
];
for (const [name, expected, fixtures] of dispatchCases) {
  const { ok } = run("queue_group_dispatched", fixtures);
  if (ok === expected) {
    console.log(`ok   ${name}`);
  } else {
    console.error(`FAIL ${name}: expected dispatched=${expected}, got ${ok}`);
    failed++;
  }
}

// The diagnosis is the whole point of firing — a bare non-zero exit would leave
// whoever reads the run log with no idea what happened or what to do.
const { stdout } = detects({
  FX_PR_HEAD: HEAD, FX_RUNS: "0",
  FX_ENTRY: entry("AWAITING_CHECKS", AGED, "github-actions", GROUP),
});
for (const needed of ["::error", GROUP, "github-actions", "dequeuePullRequest", "enqueuePullRequest", "GOL-2524"]) {
  if (stdout.includes(needed)) {
    console.log(`ok   diagnosis mentions ${needed}`);
  } else {
    console.error(`FAIL diagnosis is missing ${needed}:\n${stdout}`);
    failed++;
  }
}

if (failed) {
  console.error(`\n${failed} check(s) failed.`);
  process.exit(1);
}
console.log("\nAll merge-queue dispatch-check assertions passed.");
