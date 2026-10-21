#!/usr/bin/env node
// Behavioral test for auto-approve.yml's stacked-PR gate (GOL-3489).
//
// What it protects:
//   `main` has a merge queue, so the workflow's `gh pr merge --squash` is an
//   ENQUEUE there and the queue performs the real merge later. A topic branch
//   has no queue: the very same call is a DIRECT merge, pushed by the default
//   `GITHUB_TOKEN` (the App enqueue identity is still unprovisioned, GOL-3337).
//   That push moves the head of the PARENT PR — the one whose base is the topic
//   branch — under a self-triggered identity, so GitHub materialises every
//   `pull_request` workflow on the parent at `action_required` and the same
//   head move dismisses the parent's approving review. The parent then shows no
//   CI and REVIEW_REQUIRED, silently. Four occurrences: GOL-2504, GOL-2732
//   (#871), GOL-2800 (#898), GOL-3488 (#1110, re-gated by #1122's auto-merge on
//   2026-10-20).
//
//   The invariant: an agent PR whose base is NOT the default branch is APPROVED
//   and NOT merged by this workflow, and the run says so (a `::notice::` naming
//   the base branch and the owner's merge command). A `main`-based agent PR
//   still goes through to `gh pr merge` exactly as before (the contrast twin).
//
//   The step's shell body is read out of the real workflow (no copy of it lives
//   here) and executed under bash with a fake `gh` on PATH that answers the
//   calls the step makes and records every invocation, so a rewrite that drops
//   the base-branch check fails here rather than silently re-gating the next
//   stacked parent. `sleep` is faked too, so the step's poll loops do not wait.
//
// node builtins only — run by the `CI scripts` job (scripts/ci/*.test.mjs).

import { readFileSync, writeFileSync, mkdtempSync, chmodSync, rmSync, existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join, dirname } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const workflowFile = join(repoRoot, ".github", "workflows", "auto-approve.yml");
const workflow = readFileSync(workflowFile, "utf8");

// A changed file that is NOT under the protected-paths carve-out, so the step
// gets past the Tier-0 gate and reaches the approve + merge logic.
const UNPROTECTED_FILE = "grove_catalog/models/product.py";
const PR = "4242";
const REPO = "Goldberry-Playground/grove-odoo-modules";
const HEAD = "f877825f1ee4ba2a9e2c1df0f94de8db1b0b7a21";

let failures = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`  ok   ${name}`);
  } catch (err) {
    failures++;
    console.log(`  FAIL ${name}\n       ${err && err.message}`);
  }
}
function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

// ── Extract the approve + merge step's shell body from the workflow ─────────
// The step is the one whose name starts with "Approve" and carries a `run: |`
// block. Lines of the block are indented 10 spaces; the block ends at the first
// non-blank line indented less than that.
function extractApproveStep(yml) {
  const lines = yml.split("\n");
  const nameAt = lines.findIndex((l) => /^ {6}- name: Approve/.test(l));
  if (nameAt === -1) return null;
  const runAt = lines.findIndex((l, i) => i > nameAt && /^ {8}run: \|/.test(l));
  if (runAt === -1) return null;
  const envLines = [];
  for (let i = nameAt + 1; i < runAt; i++) envLines.push(lines[i]);
  const body = [];
  for (let i = runAt + 1; i < lines.length; i++) {
    const l = lines[i];
    if (l.trim() === "") {
      body.push("");
      continue;
    }
    if (!l.startsWith("          ")) break;
    body.push(l.slice(10));
  }
  return { name: lines[nameAt].trim(), env: envLines.join("\n"), script: body.join("\n") };
}

const step = extractApproveStep(workflow);
if (!step) {
  console.log(
    "FAIL could not find the `- name: Approve...` step with a `run: |` block in " +
      ".github/workflows/auto-approve.yml.\n" +
      "     If the step was renamed or reshaped, update this extractor — do not delete the test.",
  );
  process.exit(1);
}

// ── Fake `gh` and `sleep` ───────────────────────────────────────────────────
// `gh` logs every argv line to $FAKE_GH_LOG, answers `--json ... -q` reads from
// a canned PR document (through the real `jq`, so the step's own jq programs
// run unchanged), flips the PR to MERGED once `pr merge` is called, and accepts
// everything else silently.
const FAKE_GH = `#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> "$FAKE_GH_LOG"
args=("$@")
n=\${#args[@]}
jqprog=""
json=""
for ((i=0;i<n;i++)); do
  case "\${args[i]}" in
    -q|--jq) jqprog="\${args[i+1]:-}" ;;
    --json)  json="\${args[i+1]:-}" ;;
  esac
done
emit() {
  if [ -n "$jqprog" ]; then printf '%s' "$1" | jq -r "$jqprog"; else printf '%s\n' "$1"; fi
}
state=OPEN
[ -f "$FAKE_GH_STATE/merged" ] && state=MERGED
case "\${1:-} \${2:-}" in
  "pr view")
    emit "$(jq -n --arg a "$FAKE_AUTHOR" --arg f "$FAKE_FILE" --arg h "$FAKE_HEAD" --arg b "$FAKE_BASE" --arg s "$state" \
      '{author:{login:$a},files:[{path:$f}],headRefOid:$h,baseRefName:$b,state:$s,mergeStateStatus:"CLEAN",mergeable:"MERGEABLE",id:"PR_kwDOfake"}')" ;;
  "pr merge")
    touch "$FAKE_GH_STATE/merged"; echo "fake: merged/enqueued PR" ;;
  "pr review"|"workflow run"|"pr comment")
    : ;;
  "api graphql")
    emit '{"data":{"repository":{"pullRequest":{"id":"PR_kwDOfake","mergeQueueEntry":null,"reviewThreads":{"nodes":[]}}}}}' ;;
  "api "*)
    case "\${2:-}" in
      *check-runs*) emit '{"check_runs":[{"name":"agent-review/ada","status":"completed","conclusion":"success"}]}' ;;
      *reviews*)    emit '[]' ;;
      *actions/runs*) emit '{"total_count":1,"workflow_runs":[{"name":"CI","status":"completed","conclusion":"success","created_at":"2026-10-20T00:00:00Z"}]}' ;;
      *) emit '{}' ;;
    esac ;;
  *)
    : ;;
esac
exit 0
`;
const FAKE_SLEEP = "#!/usr/bin/env bash\nexit 0\n";

function runStep({ base, author = "app/agenticos-developer", defaultBranch = "main" }) {
  const dir = mkdtempSync(join(tmpdir(), "auto-approve-stacked-"));
  const bin = join(dir, "bin");
  const state = join(dir, "state");
  spawnSync("mkdir", ["-p", bin, state]);
  writeFileSync(join(bin, "gh"), FAKE_GH);
  writeFileSync(join(bin, "sleep"), FAKE_SLEEP);
  chmodSync(join(bin, "gh"), 0o755);
  chmodSync(join(bin, "sleep"), 0o755);
  const script = join(dir, "step.sh");
  writeFileSync(script, step.script);
  const log = join(dir, "gh.log");
  writeFileSync(log, "");
  const res = spawnSync("bash", [script], {
    cwd: repoRoot,
    encoding: "utf8",
    env: {
      ...process.env,
      PATH: `${bin}:${process.env.PATH}`,
      // The step's own `env:` block, as the runner would set it.
      GH_TOKEN: "fake-github-token",
      REPO,
      ENQUEUE_TOKEN: "",
      DEFAULT_BRANCH: defaultBranch,
      // From the "Resolve target PR number" step via $GITHUB_ENV.
      PR,
      // Fake-gh knobs.
      FAKE_GH_LOG: log,
      FAKE_GH_STATE: state,
      FAKE_AUTHOR: author,
      FAKE_FILE: UNPROTECTED_FILE,
      FAKE_HEAD: HEAD,
      FAKE_BASE: base,
    },
  });
  const calls = readFileSync(log, "utf8").split("\n").filter(Boolean);
  rmSync(dir, { recursive: true, force: true });
  return { status: res.status, stdout: res.stdout || "", stderr: res.stderr || "", calls };
}

const approvals = (calls) => calls.filter((c) => c.startsWith("pr review ") && c.includes("--approve"));
const merges = (calls) => calls.filter((c) => c.startsWith("pr merge "));

console.log(`auto-approve stacked-PR gate (GOL-3489) — step "${step.name}"`);

check("the step is handed the repo default branch (DEFAULT_BRANCH env from github.event.repository.default_branch)", () => {
  assert(
    /DEFAULT_BRANCH:\s*\$\{\{\s*github\.event\.repository\.default_branch\s*\}\}/.test(step.env),
    "the approve step's env block does not set DEFAULT_BRANCH from github.event.repository.default_branch",
  );
});

check("the step reads the PR's baseRefName (the gate cannot work without it)", () => {
  assert(step.script.includes("baseRefName"), "the step body never reads baseRefName");
});

const stacked = runStep({ base: "perf/storefronts-fonts-cls-hero" });

check("stacked agent PR (base != default): step exits 0", () => {
  assert(stacked.status === 0, `exit ${stacked.status}\n${stacked.stdout}\n${stacked.stderr}`);
});

check("stacked agent PR: github-actions[bot] still APPROVES it", () => {
  assert(approvals(stacked.calls).length === 1, `approve calls: ${JSON.stringify(approvals(stacked.calls))}`);
});

check("stacked agent PR: the workflow does NOT call `gh pr merge` (the whole fix)", () => {
  assert(
    merges(stacked.calls).length === 0,
    `expected no merge call, got: ${JSON.stringify(merges(stacked.calls))}\n${stacked.stdout}`,
  );
});

check("stacked agent PR: no post-merge build dispatch either", () => {
  assert(!stacked.calls.some((c) => c.startsWith("workflow run ")), "a workflow_dispatch was fired for a PR that was not merged");
});

check("stacked agent PR: a ::notice:: names the base branch and the owner's merge command", () => {
  const notice = stacked.stdout.split("\n").find((l) => l.startsWith("::notice"));
  assert(notice, `no ::notice line in stdout:\n${stacked.stdout}`);
  assert(notice.includes("perf/storefronts-fonts-cls-hero"), `notice does not name the base branch: ${notice}`);
  assert(notice.includes(`gh pr merge ${PR}`), `notice does not give the owner's merge command: ${notice}`);
  assert(/GITHUB_TOKEN/.test(notice), `notice does not say why GITHUB_TOKEN must not do it: ${notice}`);
});

const onMain = runStep({ base: "main" });

check("contrast twin — agent PR on `main`: approved AND handed to `gh pr merge` as before", () => {
  assert(onMain.status === 0, `exit ${onMain.status}\n${onMain.stdout}\n${onMain.stderr}`);
  assert(approvals(onMain.calls).length === 1, `approve calls: ${JSON.stringify(approvals(onMain.calls))}`);
  const m = merges(onMain.calls);
  assert(m.length === 1, `expected exactly one merge call, got: ${JSON.stringify(m)}\n${onMain.stdout}`);
  assert(m[0].includes(`pr merge ${PR} `) && m[0].includes("--squash"), `merge call is not a squash of the PR: ${m[0]}`);
  assert(!onMain.stdout.split("\n").some((l) => l.startsWith("::notice")), "a `main` PR must not raise the stacked-PR notice");
});

check("the gate keys off the repo default branch, not a hard-coded `main`", () => {
  // Same topic base, but the repo's default branch IS that branch: not stacked.
  const r = runStep({ base: "trunk", defaultBranch: "trunk" });
  assert(r.status === 0, `exit ${r.status}\n${r.stdout}\n${r.stderr}`);
  assert(merges(r.calls).length === 1, `expected a merge when base == default branch, got: ${JSON.stringify(merges(r.calls))}`);
});

check("stacked MAINTAINER PR: approve-only path unchanged (no merge, no notice needed)", () => {
  const r = runStep({ base: "perf/storefronts-fonts-cls-hero", author: "EngineeringMoonBear" });
  assert(r.status === 0, `exit ${r.status}\n${r.stdout}\n${r.stderr}`);
  assert(approvals(r.calls).length === 1, `approve calls: ${JSON.stringify(approvals(r.calls))}`);
  assert(merges(r.calls).length === 0, `maintainer PR must never be merged by the bot: ${JSON.stringify(merges(r.calls))}`);
});

if (failures > 0) {
  console.log(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall checks passed");
