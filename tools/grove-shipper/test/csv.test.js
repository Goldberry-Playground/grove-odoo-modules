import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import { parseCsv, parseCsvObjects, looseGet } from '../src/csv.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));

test('parseCsv handles quoted commas, doubled quotes and CRLF', () => {
  const rows = parseCsv('a,b,c\r\n1,"two, 2","he said ""hi"""\r\n');
  assert.deepEqual(rows, [
    ['a', 'b', 'c'],
    ['1', 'two, 2', 'he said "hi"'],
  ]);
});

test('parseCsv handles embedded newline inside a quoted field', () => {
  const rows = parseCsv('a,b\n"line1\nline2",x\n');
  assert.deepEqual(rows, [
    ['a', 'b'],
    ['line1\nline2', 'x'],
  ]);
});

test('parseCsvObjects keys records by header, keeps blank cells', async () => {
  const text = await readFile(path.join(HERE, 'fixtures', 'batch-export.csv'), 'utf8');
  const { header, records } = parseCsvObjects(text);
  assert.ok(header.includes('Grove Ref'));
  assert.ok(header.includes('Committed Rate'));
  assert.equal(records.length, 3);
  assert.equal(records[0]['Grove Ref'], 'S00021/1');
  assert.equal(records[0]['Committed Rate'], '14.20');
  assert.equal(records[2]['Name'], 'Bob "Big" Vance'); // doubled-quote survives round trip
  assert.equal(records[2]['Phone'], ''); // blank cell preserved
});

test('looseGet matches Pirate Ship-style non-stable headers', () => {
  const row = { 'Grove Ref': 'S1', 'Tracking #': '1Z999', 'Carrier Name': 'UPS', 'Total Cost': '12.30' };
  assert.equal(looseGet(row, 'grove', 'ref'), 'S1');
  assert.equal(looseGet(row, 'tracking'), '1Z999');
  assert.equal(looseGet(row, 'carrier'), 'UPS');
  assert.equal(looseGet(row, 'cost'), '12.30');
  assert.equal(looseGet(row, 'nope'), undefined);
});
