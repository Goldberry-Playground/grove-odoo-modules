// Price-guard for the label batch. Spec §B2.3:
//   - each row's quoted service matches its `Service` column
//   - price ≤ Committed Rate + 2.00
//   - batch total ≤ expected_total + 2.00 × rows
// Any breach stops before purchase.

import { parseCsvObjects } from './csv.js';

export const PER_ROW_SLACK = 2.0;

/** Parse a currency-ish cell ("$12.34", "12.34", " 12 ") to a Number, or NaN. */
export function parseMoney(v) {
  if (v == null) return NaN;
  const cleaned = String(v).replace(/[^0-9.\-]/g, '');
  if (cleaned === '' || cleaned === '-' || cleaned === '.') return NaN;
  return Number(cleaned);
}

/** Normalize a service title for tolerant comparison (case/space/punct-insensitive). */
export function normService(s) {
  return String(s ?? '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

/** True when the quoted service is "the same" as the committed one (either contains the other). */
export function serviceMatches(committed, quoted) {
  const a = normService(committed);
  const b = normService(quoted);
  if (!a || !b) return false;
  return a === b || a.includes(b) || b.includes(a);
}

/**
 * Build the expected rows from the Odoo batch CSV. Returns rows keyed by
 * Grove Ref carrying the committed rate + committed service.
 * @param {string} csvText
 * @returns {{ ref: string, order: string, service: string, committedRate: number }[]}
 */
export function committedRowsFromCsv(csvText) {
  const { records } = parseCsvObjects(csvText);
  return records.map((r) => ({
    ref: (r['Grove Ref'] || '').trim(),
    order: (r['Order'] || '').trim(),
    service: (r['Service'] || '').trim(),
    committedRate: parseMoney(r['Committed Rate']),
  }));
}

/**
 * Compare the committed rows against the quotes Pirate Ship shows.
 * @param {{ref:string, service:string, committedRate:number}[]} committed
 * @param {Map<string,{service:string, price:number}>|Record<string,{service:string,price:number}>} quotes keyed by Grove Ref
 * @param {number} expectedTotal  from Odoo (sum of committed rates)
 * @returns {{ ok: boolean, breaches: string[], quotedTotal: number, cap: number }}
 */
export function checkPrices(committed, quotes, expectedTotal) {
  const get = (ref) =>
    quotes instanceof Map ? quotes.get(ref) : quotes[ref];
  const breaches = [];
  let quotedTotal = 0;

  for (const row of committed) {
    if (!row.ref) {
      breaches.push('(row with blank Grove Ref in batch CSV)');
      continue;
    }
    const q = get(row.ref);
    if (!q) {
      breaches.push(`${row.ref}: no quote found on the Pirate Ship review page`);
      continue;
    }
    const price = typeof q.price === 'number' ? q.price : parseMoney(q.price);
    if (!Number.isFinite(price)) {
      breaches.push(`${row.ref}: unreadable quoted price "${q.price}"`);
      continue;
    }
    quotedTotal += price;
    if (!Number.isFinite(row.committedRate)) {
      breaches.push(`${row.ref}: unreadable committed rate in batch CSV`);
      continue;
    }
    const cap = row.committedRate + PER_ROW_SLACK;
    if (price > cap + 1e-9) {
      breaches.push(
        `${row.ref}: quoted $${price.toFixed(2)} > committed $${row.committedRate.toFixed(2)} + $${PER_ROW_SLACK.toFixed(2)}`,
      );
    }
    if (row.service && q.service && !serviceMatches(row.service, q.service)) {
      breaches.push(
        `${row.ref}: quoted service "${q.service}" ≠ committed "${row.service}"`,
      );
    }
  }

  const totalCap = (Number(expectedTotal) || 0) + PER_ROW_SLACK * committed.length;
  if (quotedTotal > totalCap + 1e-9) {
    breaches.push(
      `batch total $${quotedTotal.toFixed(2)} > expected $${Number(expectedTotal).toFixed(2)} + $${PER_ROW_SLACK.toFixed(2)}×${committed.length}`,
    );
  }

  return {
    ok: breaches.length === 0,
    breaches,
    quotedTotal: Number(quotedTotal.toFixed(2)),
    cap: Number(totalCap.toFixed(2)),
  };
}
