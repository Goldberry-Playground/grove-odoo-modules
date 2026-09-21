// Odoo client for the grove_headless label-batch endpoints (bearer auth).
// Uses Node 22 global fetch / FormData / Blob — no dependency.

export class OdooError extends Error {
  constructor(message, { status, body } = {}) {
    super(message);
    this.name = 'OdooError';
    this.status = status;
    this.body = body;
  }
}

export class OdooClient {
  /**
   * @param {object} opts
   * @param {string} opts.baseUrl  e.g. https://odoo.qa.gatheringatthegrove.com
   * @param {string} opts.apiKey   bearer token
   * @param {number} [opts.timeoutMs]
   */
  constructor({ baseUrl, apiKey, timeoutMs = 60_000 }) {
    if (!baseUrl) throw new Error('OdooClient: baseUrl required (GROVE_ODOO_BASE_URL)');
    if (!apiKey) throw new Error('OdooClient: apiKey required (GROVE_ODOO_API_KEY)');
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.apiKey = apiKey;
    this.timeoutMs = timeoutMs;
  }

  get authHeader() {
    return { Authorization: `Bearer ${this.apiKey}` };
  }

  async #fetch(path, init = {}) {
    const url = path.startsWith('http') ? path : `${this.baseUrl}${path}`;
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), this.timeoutMs);
    try {
      return await fetch(url, { ...init, headers: { ...this.authHeader, ...(init.headers || {}) }, signal: ac.signal });
    } catch (err) {
      throw new OdooError(`request to ${path} failed: ${err.message}`);
    } finally {
      clearTimeout(t);
    }
  }

  async #json(path, init) {
    const res = await this.#fetch(path, init);
    const text = await res.text();
    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      throw new OdooError(`${path} returned non-JSON (status ${res.status})`, { status: res.status, body: text.slice(0, 500) });
    }
    if (!res.ok) {
      throw new OdooError(data.error || `${path} → HTTP ${res.status}`, { status: res.status, body: data });
    }
    return data;
  }

  /** GET /grove/api/v1/labels/config → { pirateship_autobuy: boolean } */
  async getConfig() {
    return this.#json('/grove/api/v1/labels/config', { method: 'GET' });
  }

  /**
   * POST /grove/api/v1/labels/batch → build/return the open batch.
   * @returns {Promise<{batch_id:number,name:string,state:string,rows:number,expected_total:number,csv_url:string}>}
   */
  async buildBatch() {
    return this.#json('/grove/api/v1/labels/batch', { method: 'POST' });
  }

  /** Download the batch CSV (bearer-auth'd). @returns {Promise<string>} */
  async downloadCsv(csvUrl) {
    const res = await this.#fetch(csvUrl, { method: 'GET' });
    if (!res.ok) {
      throw new OdooError(`CSV download → HTTP ${res.status}`, { status: res.status });
    }
    return res.text();
  }

  /**
   * POST /grove/api/v1/labels/batch/<id>/tracking with the Pirate Ship export CSV.
   * All-or-nothing on the Odoo side: a 400 means nothing was written.
   * @param {number} batchId
   * @param {string|Buffer} csvContent
   * @param {string} filename
   * @returns {Promise<{batch_id:number,name:string,state:string,orders_advanced:number,skipped_already_tracked:number,total:number}>}
   */
  async postTracking(batchId, csvContent, filename = 'tracking.csv') {
    const form = new FormData();
    const blob = new Blob([csvContent], { type: 'text/csv' });
    form.append('file', blob, filename);
    return this.#json(`/grove/api/v1/labels/batch/${batchId}/tracking`, { method: 'POST', body: form });
  }
}
