import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  assertNoPirateShipCredentials,
  isCI,
  isQaTarget,
  resolveBuyDecision,
} from '../src/guards.js';

test('assertNoPirateShipCredentials throws when a PS credential env is set', () => {
  assert.throws(() => assertNoPirateShipCredentials({ PIRATESHIP_PASSWORD: 'hunter2' }), /credentials must never/);
  assert.doesNotThrow(() => assertNoPirateShipCredentials({ GROVE_ODOO_API_KEY: 'ok' }));
});

test('isCI detects common CI env markers', () => {
  assert.equal(isCI({ CI: 'true' }), true);
  assert.equal(isCI({ GITHUB_ACTIONS: 'true' }), true);
  assert.equal(isCI({ CI: 'false' }), false);
  assert.equal(isCI({}), false);
});

test('isQaTarget matches qa/staging hosts, not prod', () => {
  assert.equal(isQaTarget('https://odoo.qa.gatheringatthegrove.com'), true);
  assert.equal(isQaTarget('https://staging.example.com'), true);
  assert.equal(isQaTarget('https://odoo.gatheringatthegrove.com'), false);
});

test('resolveBuyDecision: dry-run default when nothing requests a buy', () => {
  const d = resolveBuyDecision({ buyRequested: false, autobuy: false, odooBaseUrl: 'https://odoo.prod', env: {} });
  assert.equal(d.allow, false);
  assert.match(d.reason, /dry-run/);
});

test('resolveBuyDecision: never buys in CI even with --buy', () => {
  const d = resolveBuyDecision({ buyRequested: true, autobuy: false, odooBaseUrl: 'https://odoo.prod', env: { CI: 'true' } });
  assert.equal(d.allow, false);
  assert.match(d.reason, /never buy labels in CI/);
});

test('resolveBuyDecision: QA needs explicit opt-in', () => {
  const base = 'https://odoo.qa.gatheringatthegrove.com';
  const blocked = resolveBuyDecision({ buyRequested: true, autobuy: false, odooBaseUrl: base, env: {} });
  assert.equal(blocked.allow, false);
  assert.match(blocked.reason, /GROVE_SHIPPER_ALLOW_BUY_QA/);

  const allowed = resolveBuyDecision({ buyRequested: true, autobuy: false, odooBaseUrl: base, env: { GROVE_SHIPPER_ALLOW_BUY_QA: '1' } });
  assert.equal(allowed.allow, true);
});

test('resolveBuyDecision: production --buy or autobuy allowed', () => {
  const buy = resolveBuyDecision({ buyRequested: true, autobuy: false, odooBaseUrl: 'https://odoo.gatheringatthegrove.com', env: {} });
  assert.equal(buy.allow, true);
  const auto = resolveBuyDecision({ buyRequested: false, autobuy: true, odooBaseUrl: 'https://odoo.gatheringatthegrove.com', env: {} });
  assert.equal(auto.allow, true);
});
