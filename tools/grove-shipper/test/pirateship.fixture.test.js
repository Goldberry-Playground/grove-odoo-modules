// Playwright fixture tests for the Pirate Ship page-object (GOL-2297 §B2
// acceptance). Drives PirateShip against local HTML fixtures wired to the real
// selectors.json — no live site, no credentials, no purchase. Requires the
// Chromium binary: `npx playwright install chromium` (skips cleanly if absent so
// the pure-JS unit tests still run in a browserless environment).
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { PirateShip, loadSelectors } from '../src/pirateship.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PAGES = path.join(HERE, 'fixtures', 'pages');
const BATCH_CSV = path.join(HERE, 'fixtures', 'batch-export.csv');
const fixtureUrl = (name) => pathToFileURL(path.join(PAGES, name)).href;

let chromium;
let available = true;
try {
  ({ chromium } = await import('playwright'));
} catch {
  available = false;
}

// One persistent context reused across tests; a fresh page per test.
let profileDir;
let context;

before(async () => {
  if (!available) return;
  profileDir = await mkdtemp(path.join(tmpdir(), 'grove-shipper-fixture-'));
  try {
    context = await chromium.launchPersistentContext(profileDir, {
      headless: true,
      acceptDownloads: true,
      viewport: { width: 1440, height: 900 },
    });
    context.setDefaultTimeout(15_000);
  } catch {
    available = false; // no browser binary installed — skip the suite
  }
});

after(async () => {
  if (context) await context.close();
  if (profileDir) await rm(profileDir, { recursive: true, force: true });
});

// Skip the whole suite (not fail) when Playwright's package or its Chromium
// binary/system-deps are missing — a browserless `npm test` still passes on the
// pure-JS units. Run these where the browser is installed:
//   npx playwright install --with-deps chromium && npm test
const browserless = (t) => {
  if (!context) {
    t.skip('Chromium unavailable (run: npx playwright install --with-deps chromium)');
    return true;
  }
  return false;
};

async function open(fixture) {
  const sel = await loadSelectors();
  const page = await context.newPage();
  await page.goto(fixtureUrl(fixture), { waitUntil: 'domcontentloaded' });
  return { ps: new PirateShip(context, page, sel), page };
}

test('assertSignedIn passes when the Ship nav is present', async (t) => {
  if (browserless(t)) return;
  const { ps, page } = await open('ship.html');
  await ps.assertSignedIn(); // resolves = signed in
  await page.close();
});

test('assertSignedIn throws a run-headed hint when not signed in', async (t) => {
  if (browserless(t)) return;
  const { ps, page } = await open('signin.html');
  await assert.rejects(() => ps.assertSignedIn(), /Not signed in/);
  await page.close();
});

test('uploadSpreadsheet reads every review row, its quote, and flags a bad address', async (t) => {
  if (browserless(t)) return;
  const { ps, page } = await open('ship.html');
  const review = await ps.uploadSpreadsheet(BATCH_CSV);
  assert.equal(review.importedRows, 2);
  assert.deepEqual(review.flaggedRefs, ['S01001/2']);
  assert.deepEqual(review.quotes.get('S01001/1'), { service: 'UPS Ground', price: 9.1 });
  assert.deepEqual(review.quotes.get('S01001/2'), { service: 'USPS Ground Advantage', price: 8.36 });
  await page.close();
});

test('buyLabels reads the confirmation total', async (t) => {
  if (browserless(t)) return;
  const { ps, page } = await open('ship.html');
  await ps.uploadSpreadsheet(BATCH_CSV); // walk to the review + Buy
  const { confirmedTotal } = await ps.buyLabels();
  assert.equal(confirmedTotal, 17.46);
  await page.close();
});

test('exportTracking downloads the tracking CSV with the round-trip refs', async (t) => {
  if (browserless(t)) return;
  const { ps, page } = await open('ship.html');
  await ps.uploadSpreadsheet(BATCH_CSV);
  await ps.buyLabels();
  const { filename, content } = await ps.exportTracking();
  assert.equal(filename, 'tracking.csv');
  assert.match(content, /Grove Ref,Tracking Number,Carrier,Cost/);
  assert.match(content, /S01001\/1,1Z999AA10123456784,UPS,9\.10/);
  assert.match(content, /S01001\/2,9400100000000000000000,USPS,8\.36/);
  await page.close();
});
