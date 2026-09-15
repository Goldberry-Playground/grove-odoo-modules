// Buy-safety guards. Spec: "Never buy a label in CI or on QA without Josh's
// explicit go; no Pirate Ship credentials anywhere."
//
// The purchase decision is: buy only when (a) the operator passed --buy OR the
// Odoo autobuy switch is on for a scheduled run, AND (b) none of the hard
// safety gates below trip.

/** Env names that would smell like a stored Pirate Ship credential. We refuse
 * to run if any are set, so nobody wires a password into this tool by mistake —
 * the persistent Chromium profile is the only credential. */
export const FORBIDDEN_CREDENTIAL_ENV = [
  'PIRATESHIP_PASSWORD',
  'PIRATE_SHIP_PASSWORD',
  'PIRATESHIP_USER',
  'PIRATESHIP_USERNAME',
  'PIRATESHIP_EMAIL',
  'PIRATESHIP_API_KEY',
  'PIRATESHIP_TOKEN',
];

/** Throw if any forbidden Pirate Ship credential env var is present. */
export function assertNoPirateShipCredentials(env = process.env) {
  const found = FORBIDDEN_CREDENTIAL_ENV.filter((k) => (env[k] ?? '') !== '');
  if (found.length) {
    throw new Error(
      `Refusing to run: Pirate Ship credentials must never be in the environment ` +
        `(found ${found.join(', ')}). The persistent Chromium profile is the only credential.`,
    );
  }
}

/** True when we appear to be running inside CI. */
export function isCI(env = process.env) {
  return ['CI', 'GITHUB_ACTIONS', 'BUILD_ID', 'CONTINUOUS_INTEGRATION'].some(
    (k) => (env[k] ?? '') !== '' && String(env[k]).toLowerCase() !== 'false',
  );
}

/** True when the Odoo base URL points at a QA/staging host, not production. */
export function isQaTarget(odooBaseUrl) {
  const u = String(odooBaseUrl || '').toLowerCase();
  return /(^|\/\/|\.)(qa|staging|stage|test)\./.test(u);
}

/**
 * Decide whether a purchase may proceed and why. Never returns allow:true in CI.
 * On QA, requires an explicit env opt-in (GROVE_SHIPPER_ALLOW_BUY_QA=1) that
 * stands in for "Josh's explicit go".
 *
 * @param {object} opts
 * @param {boolean} opts.buyRequested   --buy on the CLI
 * @param {boolean} opts.autobuy        Odoo grove_headless.pirateship_autobuy
 * @param {string}  opts.odooBaseUrl
 * @param {NodeJS.ProcessEnv} [opts.env]
 * @returns {{ allow: boolean, reason: string }}
 */
export function resolveBuyDecision({ buyRequested, autobuy, odooBaseUrl, env = process.env }) {
  const wants = Boolean(buyRequested) || Boolean(autobuy);
  if (!wants) {
    return { allow: false, reason: 'dry-run (no --buy and autobuy off): stopping at the review screen' };
  }
  if (isCI(env)) {
    return { allow: false, reason: 'hard block: never buy labels in CI' };
  }
  if (isQaTarget(odooBaseUrl)) {
    const ok = String(env.GROVE_SHIPPER_ALLOW_BUY_QA ?? '') === '1';
    if (!ok) {
      return {
        allow: false,
        reason:
          'QA target: buying requires Josh\'s explicit go — set GROVE_SHIPPER_ALLOW_BUY_QA=1 to authorize a QA purchase',
      };
    }
    return { allow: true, reason: 'QA purchase authorized (GROVE_SHIPPER_ALLOW_BUY_QA=1)' };
  }
  return {
    allow: true,
    reason: buyRequested ? 'production purchase authorized (--buy)' : 'production purchase authorized (autobuy on)',
  };
}
