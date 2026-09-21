import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import {
  parseMoney,
  serviceMatches,
  committedRowsFromCsv,
  checkPrices,
  PER_ROW_SLACK,
} from '../src/priceCheck.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));

async function committed() {
  const text = await readFile(path.join(HERE, 'fixtures', 'batch-export.csv'), 'utf8');
  return committedRowsFromCsv(text);
}

test('parseMoney strips currency symbols', () => {
  assert.equal(parseMoney('$14.20'), 14.2);
  assert.equal(parseMoney(' 21.75 '), 21.75);
  assert.ok(Number.isNaN(parseMoney('n/a')));
});

test('serviceMatches is tolerant of case/punctuation and substrings', () => {
  assert.ok(serviceMatches('USPS Ground Advantage', 'usps ground advantage'));
  assert.ok(serviceMatches('UPS Ground', 'UPS® Ground'));
  assert.ok(serviceMatches('USPS Ground Advantage', 'Ground Advantage'));
  assert.equal(serviceMatches('UPS Ground', 'USPS Priority'), false);
});

test('committedRowsFromCsv reads ref/service/rate', async () => {
  const rows = await committed();
  assert.equal(rows.length, 3);
  assert.deepEqual(rows[0], {
    ref: 'S00021/1',
    order: 'S00021',
    service: 'USPS Ground Advantage',
    committedRate: 14.2,
  });
});

test('checkPrices passes when every quote is within committed + slack', async () => {
  const rows = await committed();
  const quotes = new Map([
    ['S00021/1', { service: 'USPS Ground Advantage', price: 14.2 }],
    ['S00021/2', { service: 'UPS Ground', price: 22.5 }], // +0.75, under +2 slack
    ['S00034/1', { service: 'USPS Ground Advantage', price: 16.4 }],
  ]);
  const res = checkPrices(rows, quotes, 52.35);
  assert.ok(res.ok, res.breaches.join('; '));
  assert.equal(res.quotedTotal, 53.1);
});

test('checkPrices flags a per-row overage beyond slack', async () => {
  const rows = await committed();
  const quotes = new Map([
    ['S00021/1', { service: 'USPS Ground Advantage', price: 14.2 }],
    ['S00021/2', { service: 'UPS Ground', price: 24.0 }], // +2.25 > slack
    ['S00034/1', { service: 'USPS Ground Advantage', price: 16.4 }],
  ]);
  const res = checkPrices(rows, quotes, 52.35);
  assert.equal(res.ok, false);
  assert.ok(res.breaches.some((b) => b.includes('S00021/2')));
});

test('checkPrices flags a service mismatch', async () => {
  const rows = await committed();
  const quotes = new Map([
    ['S00021/1', { service: 'UPS Priority', price: 14.2 }], // wrong service
    ['S00021/2', { service: 'UPS Ground', price: 21.75 }],
    ['S00034/1', { service: 'USPS Ground Advantage', price: 16.4 }],
  ]);
  const res = checkPrices(rows, quotes, 52.35);
  assert.equal(res.ok, false);
  assert.ok(res.breaches.some((b) => b.includes('S00021/1') && b.toLowerCase().includes('service')));
});

test('checkPrices flags a missing quote', async () => {
  const rows = await committed();
  const quotes = new Map([['S00021/1', { service: 'USPS Ground Advantage', price: 14.2 }]]);
  const res = checkPrices(rows, quotes, 52.35);
  assert.equal(res.ok, false);
  assert.ok(res.breaches.some((b) => b.includes('S00021/2') && b.includes('no quote')));
});

test('checkPrices flags a batch-total breach even when each row is fine', async () => {
  // 3 rows, slack 2 each = 6 total slack. Push every row just under per-row cap
  // but arrange expectedTotal low so the aggregate cap is exceeded.
  const rows = await committed();
  const quotes = new Map([
    ['S00021/1', { service: 'USPS Ground Advantage', price: 16.19 }], // +1.99
    ['S00021/2', { service: 'UPS Ground', price: 23.74 }], // +1.99
    ['S00034/1', { service: 'USPS Ground Advantage', price: 18.39 }], // +1.99
  ]);
  const lowExpected = 10; // aggregate cap = 10 + 6 = 16 < quoted ~58
  const res = checkPrices(rows, quotes, lowExpected);
  assert.equal(res.ok, false);
  assert.ok(res.breaches.some((b) => b.includes('batch total')));
  assert.equal(PER_ROW_SLACK, 2.0);
});
