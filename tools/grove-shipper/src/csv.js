// Minimal RFC-4180 CSV parse/format, compatible with Python's csv.writer default
// dialect (QUOTE_MINIMAL, doubled quotes, \r\n or \n line endings). No dependency.

/**
 * Parse CSV text into an array of rows (each row an array of string cells).
 * Handles quoted fields containing commas, embedded newlines and doubled quotes.
 * Skips a trailing blank line. Accepts \r\n and \n.
 * @param {string} text
 * @returns {string[][]}
 */
export function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = '';
  let inQuotes = false;
  let sawAny = false;
  const s = String(text ?? '');
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (inQuotes) {
      if (c === '"') {
        if (s[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        field += c;
      }
      continue;
    }
    if (c === '"') {
      inQuotes = true;
      sawAny = true;
    } else if (c === ',') {
      row.push(field);
      field = '';
      sawAny = true;
    } else if (c === '\r') {
      // swallow; the \n (if any) closes the row
    } else if (c === '\n') {
      row.push(field);
      rows.push(row);
      row = [];
      field = '';
      sawAny = false;
    } else {
      field += c;
      sawAny = true;
    }
  }
  if (sawAny || field.length || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

/**
 * Parse a CSV into { header, records } where records are objects keyed by the
 * header row. Header cells are used verbatim (trimmed).
 * @param {string} text
 * @returns {{ header: string[], records: Record<string,string>[] }}
 */
export function parseCsvObjects(text) {
  const rows = parseCsv(text).filter((r) => r.some((c) => c.trim() !== ''));
  if (rows.length === 0) return { header: [], records: [] };
  const header = rows[0].map((h) => h.trim());
  const records = rows.slice(1).map((r) => {
    const obj = {};
    header.forEach((h, idx) => {
      obj[h] = r[idx] ?? '';
    });
    return obj;
  });
  return { header, records };
}

/**
 * Case/space-insensitive lookup of a value across an object's keys where every
 * needle is contained in the key (mirrors the loose header matching Odoo's
 * import_tracking uses for Pirate Ship's non-stable export headers).
 * @param {Record<string,string>} obj
 * @param  {...string} needles
 * @returns {string|undefined}
 */
export function looseGet(obj, ...needles) {
  const wants = needles.map((n) => n.toLowerCase());
  for (const key of Object.keys(obj)) {
    const k = key.toLowerCase();
    if (wants.every((w) => k.includes(w))) return obj[key];
  }
  return undefined;
}
