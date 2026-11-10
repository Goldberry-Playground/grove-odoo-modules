import { test } from 'node:test';
import assert from 'node:assert/strict';

import { OdooClient } from '../src/odoo.js';
import { parseZone, buildNotices, reconcileNotices } from '../src/zoneBatch.js';
import { postDiscordSummary } from '../src/discord.js';

function stubFetch(t, response) {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(response), { status: 200 });
  });
  return calls;
}

test('parseZone accepts USDA zones 2 to 10 as strings', () => {
  assert.equal(parseZone('2'), '2');
  assert.equal(parseZone('10'), '10');
});

test('parseZone refuses a missing or out-of-range zone', () => {
  for (const bad of [null, undefined, '', '1', '11', '6a', 'six']) {
    assert.throws(() => parseZone(bad), /--zone/);
  }
});

test('buildBatch posts the zone as JSON', async (t) => {
  const calls = stubFetch(t, { batch_id: 7, rows: 1 });
  const odoo = new OdooClient({ baseUrl: 'https://odoo.example', apiKey: 'k' });
  await odoo.buildBatch('6');
  assert.equal(calls.length, 1);
  assert.match(String(calls[0].url), /\/grove\/api\/v1\/labels\/batch$/);
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.body, JSON.stringify({ zone: '6' }));
  assert.equal(calls[0].init.headers['Content-Type'], 'application/json');
});

test('buildNotices reports orders held out and skipped', () => {
  const notices = buildNotices({ needs_confirmation: ['S00001: confirm no label'], skipped: ['S00002: on hold'] });
  assert.deepEqual(notices, [
    'Held out of the CSV until confirmed: S00001: confirm no label',
    'Skipped: S00002: on hold',
  ]);
});

test('buildNotices tolerates absent lists and object entries', () => {
  assert.deepEqual(buildNotices({}), []);
  assert.deepEqual(buildNotices({ skipped: [{ order: 'S1' }] }), ['Skipped: {"order":"S1"}']);
});

test('reconcileNotices reports manual review rows and warnings', () => {
  assert.deepEqual(reconcileNotices({ manual_review: ['S3'], warnings: ['late'] }), [
    'Manual review: S3',
    'Warning: late',
  ]);
  assert.deepEqual(reconcileNotices({}), []);
});

test('the Discord summary carries notices without flagging a problem', async (t) => {
  const calls = stubFetch(t, {});
  await postDiscordSummary('https://discord.example/hook', {
    batchName: 'B1',
    rows: 2,
    total: 10,
    notices: ['Skipped: S2'],
  });
  const { content } = JSON.parse(calls[0].init.body);
  assert.match(content, /dry-run/);
  assert.doesNotMatch(content, /had problems/);
  assert.match(content, /Skipped: S2/);
});
