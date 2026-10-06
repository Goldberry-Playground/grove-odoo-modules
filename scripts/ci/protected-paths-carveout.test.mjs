#!/usr/bin/env node
// Behavioral tests for the Tier-0 auto-approve carve-out (GOL-1406-A).
// Run: `node scripts/ci/protected-paths-carveout.test.mjs`
//
// The carve-out is called by auto-approve.yml before it stamps its approval and
// WITHHOLDS approval whenever a PR's changed files intersect PROTECTED_GLOBS —
// so an agent-authored PR touching `.github/workflows/**`, `infra/terraform/**`,
// etc. can never be auto-approved by the bot and sail into the merge queue.
//
// GOL-2013 removed the protected-paths guard workflow and its single-source
// generator; branch protection's `dismiss_stale_reviews_on_push` now closes the
// approve-then-push hole that the guard's SHA-binding used to. The carve-out is
// no longer generated and no longer mirrors a guard, so the old CARVE-OUT ≡ GUARD
// invariants are gone — this test now exercises the module's own behavior.
import assert from 'node:assert/strict';
import { protectedHits, globToRe, PROTECTED_GLOBS } from './protected-paths-carveout.mjs';

// ── Guard-rail: the globs carrying workflow-equivalent authority ────────────
// Losing any of these would let a change to CI's own decision-making be
// auto-approved with no human in the loop. Fail loudly.
//
// GOL-3105: `.github/workflows/**` is NOT self-protecting beyond workflows. It
// covers auto-approve.yml, but it covers NEITHER the carve-out module (which
// lives under `scripts/ci/`) NOR the shared composite actions under
// `.github/actions/**`. The comment here used to claim it "covers this file";
// it never did. Both are now listed explicitly, and the assertion below is what
// keeps them listed.
for (const g of [
  '.github/workflows/**',               // auto-approve.yml and every workflow
  '.github/actions/**',                 // shared composite actions, consumed @main fleet-wide
  'scripts/ci/protected-paths-carveout.mjs', // this gate's own glob list
]) {
  assert.ok(
    PROTECTED_GLOBS.includes(g),
    `PROTECTED_GLOBS must include ${g} — it carries workflow-equivalent authority (GOL-3105)`
  );
}

// GOL-3105 root cause, pinned so nobody "simplifies" the list back: the
// workflows glob does not reach sibling directories under `.github/`.
assert.ok(
  !globToRe('.github/workflows/**').test('.github/actions/ci-failure-router/action.yml'),
  '.github/workflows/** must NOT be assumed to cover .github/actions/** (GOL-3105)'
);

// ── glob → RegExp semantics ─────────────────────────────────────────────────
// `**` matches nested and root; `*` does not cross `/`; literals are anchored.
assert.ok(globToRe('.github/workflows/**').test('.github/workflows/ci.yml'));
assert.ok(globToRe('.github/workflows/**').test('.github/workflows/nested/x.yml'));
assert.ok(!globToRe('a/*.ts').test('a/b/c.ts'), "'*' must not cross '/'");
assert.ok(globToRe('a/*.ts').test('a/b.ts'));
assert.ok(!globToRe('*.md').test('docs/x.md'), "leading '*' does not cross '/'");
assert.ok(globToRe('*.md').test('README.md'));

// ── Behavioral matrix ───────────────────────────────────────────────────────
// AC2: only non-protected paths -> no hits (auto-approve proceeds).
assert.deepEqual(protectedHits(['README.md', 'apps/hub/page.tsx']), []);
// AC1: a protected path -> a hit (auto-approve withholds).
assert.deepEqual(
  protectedHits(['README.md', '.github/workflows/ci.yml']),
  ['.github/workflows/ci.yml']
);
// `**` matches nested and root; `*` does not cross `/`.
assert.deepEqual(protectedHits(['.github/workflows/nested/x.yml']), [
  '.github/workflows/nested/x.yml',
]);
// Blank / whitespace lines from `gh` output are ignored, not treated as hits.
assert.deepEqual(protectedHits(['', '  ', 'README.md']), []);

// ── GOL-3105 regressions: authority-carrying paths withhold ─────────────────
// A shared composite action is workflow-equivalent (it runs in the calling
// job, with that job's token) and is consumed at a MOVING `@main` ref by
// grove-sites, odoocker-goldberrygrove and grove-odoo-modules — so one merge
// changes all three repos' CI. Before GOL-3105 this returned [] and such a PR
// was auto-approved and enqueued with no human review.
assert.deepEqual(
  protectedHits(['.github/actions/ci-failure-router/action.yml']),
  ['.github/actions/ci-failure-router/action.yml']
);
// Nested, and a non-action file inside the directory, both hit.
assert.deepEqual(protectedHits(['.github/actions/a/b/c.sh']), ['.github/actions/a/b/c.sh']);

// A PR that LOOSENS this glob list must itself need a human. auto-approve.yml
// already runs the BASE-branch copy, so a PR cannot edit the carve-out to clear
// ITSELF (GOL-1406-A); this closes the one-merge-then-everything-after case.
assert.deepEqual(
  protectedHits(['scripts/ci/protected-paths-carveout.mjs']),
  ['scripts/ci/protected-paths-carveout.mjs']
);

// ...but THIS file is intentionally NOT protected: it cannot change the gate's
// behaviour, so protecting it would add review friction and buy nothing.
assert.deepEqual(protectedHits(['scripts/ci/protected-paths-carveout.test.mjs']), []);
// Other scripts/ci/ helpers are likewise unprotected — the glob is one exact
// file, not `scripts/ci/**`.
assert.deepEqual(protectedHits(['scripts/ci/merge-queue-wedge-detector.test.mjs']), []);

console.log('protected-paths-carveout: all assertions passed');
