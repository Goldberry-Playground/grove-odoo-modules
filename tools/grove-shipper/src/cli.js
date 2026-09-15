#!/usr/bin/env node
// grove-shipper — Pirate Ship label-batch runner (GOL-2271 B2).
//
//   grove-shipper run [--buy | --dry-run] [--batch <id>] [--headed]
//
// Dry-run is the default: pull the batch from Odoo, upload the CSV to Pirate
// Ship, price-check every row, and stop at the review screen with a screenshot.
// --buy proceeds through purchase → Export Tracking Data → reconcile in Odoo,
// subject to the buy-safety guards (never in CI; QA needs GROVE_SHIPPER_ALLOW_BUY_QA=1).

import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';

import { OdooClient } from './odoo.js';
import { committedRowsFromCsv, checkPrices } from './priceCheck.js';
import { assertNoPirateShipCredentials, resolveBuyDecision } from './guards.js';
import { postDiscordSummary } from './discord.js';

function parseArgs(argv) {
  const args = { command: argv[0], buy: false, dryRun: false, headed: false, batch: null };
  for (let i = 1; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--buy') args.buy = true;
    else if (a === '--dry-run') args.dryRun = true;
    else if (a === '--headed') args.headed = true;
    else if (a === '--batch') args.batch = argv[++i];
    else if (a.startsWith('--batch=')) args.batch = a.slice('--batch='.length);
  }
  return args;
}

function env(name, fallback) {
  const v = process.env[name];
  return v == null || v === '' ? fallback : v;
}

function log(step, msg) {
  console.log(`[grove-shipper] ${step}: ${msg}`);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.command !== 'run') {
    console.error('usage: grove-shipper run [--buy|--dry-run] [--batch <id>] [--headed]');
    process.exit(2);
  }
  if (args.buy && args.dryRun) {
    console.error('--buy and --dry-run are mutually exclusive');
    process.exit(2);
  }

  // Safety: no Pirate Ship credentials may ever live in the environment.
  assertNoPirateShipCredentials();

  const baseUrl = env('GROVE_ODOO_BASE_URL');
  const apiKey = env('GROVE_ODOO_API_KEY');
  const discordUrl = env('DISCORD_ORDERS_WEBHOOK_URL');
  const profileDir = env('GROVE_SHIPPER_PROFILE_DIR', path.join(os.homedir(), '.grove-shipper', 'profile'));
  const outDir = env('GROVE_SHIPPER_OUT_DIR', path.join(process.cwd(), 'out'));

  const odoo = new OdooClient({ baseUrl, apiKey });

  // ── 1. pull ────────────────────────────────────────────────────────────
  const cfg = await odoo.getConfig();
  const autobuy = Boolean(cfg.pirateship_autobuy);
  log('config', `autobuy=${autobuy}`);

  const batch = await odoo.buildBatch();
  log('pull', `${batch.name} (id=${batch.batch_id}) state=${batch.state} rows=${batch.rows} expected=$${Number(batch.expected_total).toFixed(2)}`);

  if (batch.state === 'purchased') {
    log('pull', 'batch already purchased — nothing to do');
    process.exit(0);
  }
  if (!batch.rows) {
    log('pull', 'nothing to ship (0 rows)');
    process.exit(0);
  }

  const csvText = await odoo.downloadCsv(batch.csv_url);
  const committed = committedRowsFromCsv(csvText);
  const batchOut = path.join(outDir, batch.name);
  await mkdir(batchOut, { recursive: true });
  const csvPath = path.join(batchOut, 'batch.csv');
  await writeFile(csvPath, csvText);
  log('pull', `saved ${csvPath} (${committed.length} rows)`);

  const buyDecision = resolveBuyDecision({ buyRequested: args.buy, autobuy, odooBaseUrl: baseUrl });
  log('buy-gate', buyDecision.reason);

  // ── 2–6 need the browser. Lazy import so pure paths need no Playwright. ──
  const { PirateShip } = await import('./pirateship.js');
  const ps = await PirateShip.launch({ profileDir, headed: args.headed || !buyDecision.allow });
  const problems = [];
  let mode = 'dry-run';
  let finalTotal = batch.expected_total;

  try {
    await ps.assertSignedIn();

    // ── 2. upload ──────────────────────────────────────────────────────
    const review = await ps.uploadSpreadsheet(csvPath);
    log('upload', `imported ${review.importedRows} rows, ${review.flaggedRefs.length} address flag(s)`);
    if (review.importedRows !== committed.length) {
      problems.push(`imported ${review.importedRows} rows but batch has ${committed.length}`);
    }
    if (review.flaggedRefs.length) {
      problems.push(`address-validation flag(s): ${review.flaggedRefs.join(', ')}`);
    }

    // ── 3. price check ─────────────────────────────────────────────────
    const price = checkPrices(committed, review.quotes, batch.expected_total);
    log('price', `quoted total $${price.quotedTotal.toFixed(2)} (cap $${price.cap.toFixed(2)})`);
    if (!price.ok) problems.push(...price.breaches);

    // Any problem stops before purchase.
    if (problems.length) {
      await ps.screenshot(path.join(batchOut, 'review-blocked.png'));
      throw new StopBeforeBuy(problems);
    }

    // ── 4. buy ─────────────────────────────────────────────────────────
    if (!buyDecision.allow) {
      await ps.screenshot(path.join(batchOut, 'review-dryrun.png'));
      log('dry-run', `stopped at the review screen — screenshot in ${batchOut}`);
    } else {
      const { confirmedTotal } = await ps.buyLabels();
      mode = 'bought';
      finalTotal = Number.isFinite(confirmedTotal) ? confirmedTotal : price.quotedTotal;
      log('buy', `labels created, confirmed total $${Number(finalTotal).toFixed(2)}`);

      // ── 5. export → reconcile ───────────────────────────────────────
      const exp = await ps.exportTracking();
      const trackPath = path.join(batchOut, 'tracking.csv');
      await writeFile(trackPath, exp.content);
      await ps.downloadLabels(path.join(batchOut, 'labels.pdf')).catch(() => null);
      const recon = await odoo.postTracking(batch.batch_id, exp.content, exp.filename || 'tracking.csv');
      log('reconcile', `state=${recon.state} orders_advanced=${recon.orders_advanced} skipped=${recon.skipped_already_tracked} total=$${Number(recon.total).toFixed(2)}`);

      // ── 6. verify ───────────────────────────────────────────────────
      if (recon.state !== 'purchased') problems.push(`batch state is ${recon.state}, expected purchased`);
      if (Math.abs(Number(recon.total) - Number(finalTotal)) > 0.01) {
        problems.push(`Odoo total $${Number(recon.total).toFixed(2)} ≠ Pirate Ship total $${Number(finalTotal).toFixed(2)}`);
      }
      finalTotal = recon.total;
    }
  } catch (err) {
    if (err instanceof StopBeforeBuy) {
      // problems already collected
    } else {
      problems.push(`runner error: ${err.message}`);
    }
  } finally {
    await ps.close().catch(() => {});
  }

  const batchUrl = `${odoo.baseUrl}/odoo/action-base_setup.action_general_configuration#id=${batch.batch_id}&model=grove.label.batch`;
  await postDiscordSummary(discordUrl, {
    batchName: batch.name,
    rows: batch.rows,
    total: finalTotal,
    batchUrl,
    mode,
    problems,
  });

  if (problems.length) {
    console.error(`[grove-shipper] FAILED with ${problems.length} problem(s):`);
    for (const p of problems) console.error(`  - ${p}`);
    process.exit(1);
  }
  log('done', mode === 'bought' ? 'batch purchased and reconciled' : 'dry-run complete');
  process.exit(0);
}

class StopBeforeBuy extends Error {
  constructor(problems) {
    super('stopped before purchase');
    this.problems = problems;
  }
}

main().catch((err) => {
  console.error(`[grove-shipper] fatal: ${err.stack || err.message}`);
  process.exit(1);
});
