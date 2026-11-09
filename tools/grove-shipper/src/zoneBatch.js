// Pure helpers for zone-scoped label batches: the Odoo build route takes one
// USDA zone at a time and reports the orders it left out of the CSV.

const USDA_ZONES = new Set(['2', '3', '4', '5', '6', '7', '8', '9', '10']);

/** @returns {string} the validated zone. @throws when not a USDA zone 2 to 10. */
export function parseZone(raw) {
  const zone = raw == null ? '' : String(raw);
  if (!USDA_ZONES.has(zone)) {
    throw new Error(`--zone must be a USDA zone from 2 to 10 (got "${zone}")`);
  }
  return zone;
}

function text(entry) {
  return typeof entry === 'string' ? entry : JSON.stringify(entry);
}

function prefixed(label, entries) {
  return (entries || []).map((entry) => `${label}: ${text(entry)}`);
}

/** Orders the batch build left out. Informational: the CSV is still safe to buy. */
export function buildNotices(batch) {
  return [
    ...prefixed('Held out of the CSV until confirmed', batch.needs_confirmation),
    ...prefixed('Skipped', batch.skipped),
  ];
}

/** Rows the tracking import could not settle on its own. */
export function reconcileNotices(recon) {
  return [...prefixed('Manual review', recon.manual_review), ...prefixed('Warning', recon.warnings)];
}
