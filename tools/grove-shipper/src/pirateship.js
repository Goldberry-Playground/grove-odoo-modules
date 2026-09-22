// Pirate Ship page-object (Playwright). The runner "clicks what a human clicks"
// — no reverse-engineered purchase API. Every step asserts the page it expects
// BEFORE acting, so a UI change fails loudly before money moves. All selectors
// come from selectors.json (calibration-pending — confirmed once by Josh during
// the first headed sign-in). Playwright is imported lazily so the pure modules
// (and their unit tests) don't require the browser binary.

import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ACTION_TIMEOUT = 60_000;

/** Resolve a selector spec (string OR {role,name}) to a Playwright Locator. */
function loc(scope, spec) {
  if (!spec) throw new Error('missing selector spec');
  if (typeof spec === 'string') return scope.locator(spec);
  if (spec.role) return scope.getByRole(spec.role, spec.name ? { name: spec.name } : undefined);
  throw new Error(`unrecognized selector spec: ${JSON.stringify(spec)}`);
}

export async function loadSelectors(file) {
  const p = file || path.join(HERE, 'selectors.json');
  return JSON.parse(await readFile(p, 'utf8'));
}

/**
 * Extra Chromium launch flags from `PW_CHROMIUM_ARGS` (whitespace-separated).
 * Empty by default so Josh's real headed sign-in launches a normal browser. A
 * rootless container (CI, the fleet browser-runtime) sets
 * `PW_CHROMIUM_ARGS="--no-sandbox --disable-dev-shm-usage --disable-gpu"` so the
 * same code — and the fixture suite — can run headless without a sandbox.
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {string[]}
 */
export function chromiumArgsFromEnv(env = process.env) {
  return String(env.PW_CHROMIUM_ARGS || '')
    .trim()
    .split(/\s+/)
    .filter(Boolean);
}

export class PirateShip {
  /** @param {import('playwright').BrowserContext} context @param {object} selectors */
  constructor(context, page, selectors) {
    this.context = context;
    this.page = page;
    this.sel = selectors;
  }

  /**
   * Launch a persistent-profile Chromium and open Pirate Ship. Headed the first
   * time so Josh can sign in + enter 2FA; the profile IS the credential.
   * @returns {Promise<PirateShip>}
   */
  static async launch({ profileDir, headed = true, selectors, timeoutMs = ACTION_TIMEOUT, args }) {
    const { chromium } = await import('playwright');
    const sel = selectors || (await loadSelectors());
    const launchArgs = args ?? chromiumArgsFromEnv();
    const context = await chromium.launchPersistentContext(profileDir, {
      headless: !headed,
      acceptDownloads: true,
      viewport: { width: 1440, height: 900 },
      ...(launchArgs.length ? { args: launchArgs } : {}),
    });
    context.setDefaultTimeout(timeoutMs);
    const page = context.pages()[0] || (await context.newPage());
    await page.goto(sel.baseUrl + (sel.shipPath || '/'), { waitUntil: 'domcontentloaded' });
    return new PirateShip(context, page, sel);
  }

  /** Assert we are signed in; if not, throw a message telling the operator to run headed. */
  async assertSignedIn() {
    const signedIn = loc(this.page, this.sel.signedInProbe);
    try {
      await signedIn.first().waitFor({ state: 'visible', timeout: 15_000 });
    } catch {
      throw new Error(
        'Not signed in to Pirate Ship. Run once headed so Josh can log in and tick "Stay signed in"; ' +
          'the persistent profile then keeps the session.',
      );
    }
  }

  /**
   * Upload the batch CSV and apply the saved "Grove batch v1" mapping.
   * Asserts every row imported with no address-validation flag.
   * @returns {Promise<{ importedRows: number, flaggedRefs: string[] }>}
   */
  async uploadSpreadsheet(csvPath) {
    const u = this.sel.upload;
    await loc(this.page, u.openSpreadsheet).click();
    await loc(this.page, u.fileInput).setInputFiles(csvPath);
    // Apply the saved mapping by name if the UI exposes a picker.
    if (u.mappingSelect) {
      const picker = loc(this.page, u.mappingSelect);
      if (await picker.count()) {
        await picker.selectOption({ label: u.mappingName }).catch(() => {});
      }
    }
    if (u.applyMapping) {
      const apply = loc(this.page, u.applyMapping);
      if (await apply.count()) await apply.first().click();
    }
    if (u.continue) {
      const cont = loc(this.page, u.continue);
      if (await cont.count()) await cont.first().click();
    }
    return this.readReview();
  }

  /**
   * Read the review screen: one entry per row with ref/service/price, plus which
   * refs carry an address-validation flag.
   * @returns {Promise<{ importedRows:number, flaggedRefs:string[], quotes: Map<string,{service:string,price:number}> }>}
   */
  async readReview() {
    const r = this.sel.review;
    const rows = loc(this.page, r.rowSelector);
    await rows.first().waitFor({ state: 'visible' });
    const count = await rows.count();
    const quotes = new Map();
    const flaggedRefs = [];
    for (let i = 0; i < count; i++) {
      const row = rows.nth(i);
      const ref = (await this.#cellText(row, r.refCell)).trim();
      const service = (await this.#cellText(row, r.serviceCell)).trim();
      const price = this.#money(await this.#cellText(row, r.priceCell));
      if (ref) quotes.set(ref, { service, price });
      const flagCount = await row.locator(cssOf(r.addressFlag)).count().catch(() => 0);
      if (flagCount > 0 && ref) flaggedRefs.push(ref);
    }
    return { importedRows: count, flaggedRefs, quotes };
  }

  /** Click Buy and wait for the confirmation page; return the confirmed total. */
  async buyLabels() {
    await loc(this.page, this.sel.buy.buyButton).click();
    await loc(this.page, this.sel.buy.confirmProbe).first().waitFor({ state: 'visible' });
    const total = this.#money(await this.#cellText(this.page, this.sel.buy.confirmTotal));
    return { confirmedTotal: total };
  }

  /** Save a screenshot of the current page (used by --dry-run at the review screen). */
  async screenshot(file) {
    await this.page.screenshot({ path: file, fullPage: true });
  }

  /**
   * Click "Export Tracking Data" and return the downloaded CSV text.
   * @returns {Promise<{ filename: string, content: string }>}
   */
  async exportTracking() {
    const [download] = await Promise.all([
      this.page.waitForEvent('download'),
      loc(this.page, this.sel.export.exportTracking).click(),
    ]);
    const stream = await download.createReadStream();
    const chunks = [];
    for await (const c of stream) chunks.push(c);
    return { filename: download.suggestedFilename(), content: Buffer.concat(chunks).toString('utf8') };
  }

  async downloadLabels(destPath) {
    const link = loc(this.page, this.sel.export.downloadLabels);
    if (!(await link.count())) return null;
    const [download] = await Promise.all([this.page.waitForEvent('download'), link.first().click()]);
    await download.saveAs(destPath);
    return destPath;
  }

  async close() {
    await this.context.close();
  }

  async #cellText(scope, spec) {
    const l = scope.locator(cssOf(spec));
    if (!(await l.count())) return '';
    return (await l.first().innerText().catch(() => '')) || '';
  }

  #money(v) {
    const cleaned = String(v ?? '').replace(/[^0-9.\-]/g, '');
    const n = Number(cleaned);
    return Number.isFinite(n) ? n : NaN;
  }
}

/** selectors.json review cells are CSS strings; normalize a spec to a CSS selector. */
function cssOf(spec) {
  return typeof spec === 'string' ? spec : '';
}
