# grove-shipper

Pirate Ship label-batch runner for Goldberry Grove (GOL-2271 B2). Odoo owns the
batch; this tool "clicks what a human clicks" in the Pirate Ship web UI. There is
**no reverse-engineered Pirate Ship purchase API** and **no Pirate Ship credential
anywhere in code or env** — a persistent Chromium profile is the only credential.

Spec: `docs/superpowers/specs/2026-09-09-pirateship-fulfillment-design.md` §B2.
The **manual path stays first-class**: everything the runner does can be done by
hand with the same files and the same Odoo endpoints (see below).

## What it does

```
grove-shipper run [--buy | --dry-run] [--batch <id>] [--headed]
```

Dry-run is the **default**. Steps:

1. **pull** — `POST /grove/api/v1/labels/batch` (idempotent). Exits `0` "nothing to
   ship" on zero rows; refuses a batch already `purchased`. Saves the CSV to
   `out/<batch>/batch.csv`.
2. **upload** — Ship → *Upload a spreadsheet* → attach the CSV → apply the saved
   field mapping (`Grove batch v1`). Asserts every row imported with no
   address-validation flag.
3. **price check** — for each row: quoted service matches the `Service` column and
   `price ≤ Committed Rate + $2.00`; batch total `≤ expected_total + $2.00 × rows`.
   Any breach stops **before** purchase (screenshot to `out/`).
4. **buy** — only with `--buy` (or the Odoo autobuy switch on for a scheduled run),
   and only when the safety guards allow it. `--dry-run` stops at the review screen
   with a screenshot.
5. **export** — *Export Tracking Data* → `POST /grove/api/v1/labels/batch/<id>/tracking`
   (all-or-nothing reconcile in Odoo) → download the label PDF to `out/<batch>/labels.pdf`.
6. **verify** — batch is `purchased`, Odoo total within $0.01 of the Pirate Ship
   confirmation; post one summary to `DISCORD_ORDERS_WEBHOOK_URL`. A mismatch exits
   non-zero and leaves affected orders in `awaiting_label`.

## Safety guards (never buy where you shouldn't)

- **Never in CI.** `--buy` is hard-blocked when `CI`/`GITHUB_ACTIONS` is set.
- **QA needs an explicit go.** When `GROVE_ODOO_BASE_URL` points at a QA/staging
  host, buying requires `GROVE_SHIPPER_ALLOW_BUY_QA=1` (stands in for Josh's go).
- **No Pirate Ship credentials.** The runner refuses to start if any
  `PIRATESHIP_*` credential env var is present.
- Re-running after a failure is safe: the batch id is the idempotency key and the
  Odoo reconcile refuses to re-write already-tracked orders.

## Setup

```
cd tools/grove-shipper
npm install
npx playwright install chromium
```

Config comes from the environment (use `op run` in practice):

| var | meaning |
| --- | --- |
| `GROVE_ODOO_BASE_URL` | e.g. `https://odoo.qa.gatheringatthegrove.com` |
| `GROVE_ODOO_API_KEY` | bearer token for the label endpoints |
| `DISCORD_ORDERS_WEBHOOK_URL` | run-summary webhook |
| `GROVE_SHIPPER_PROFILE_DIR` | optional; default `~/.grove-shipper/profile` |
| `GROVE_SHIPPER_OUT_DIR` | optional; default `./out` |
| `GROVE_SHIPPER_ALLOW_BUY_QA` | set `1` to authorize a QA purchase |

### First run (once, headed — Josh signs in)

The first run opens a real Chromium window. **Josh logs into Pirate Ship, enters
the emailed 2FA code, and ticks "Stay signed in".** The profile then holds the
session; subsequent runs reuse it. The runner never sees the password.

```
GROVE_ODOO_BASE_URL=… GROVE_ODOO_API_KEY=… \
  node src/cli.js run --dry-run --headed
```

### One-time Pirate Ship setup (manual, by Josh)

Per the spec rollout, one manual spreadsheet upload is needed to (a) create and
name the saved field mapping **`Grove batch v1`** and (b) confirm `Grove Ref`
survives into *Export Tracking Data*.

## ⚠️ Selectors are calibration-pending

`src/selectors.json` holds every Pirate Ship DOM selector the runner uses. These
are **best-effort guesses** — they cannot be verified without an authenticated
Pirate Ship session, which only Josh has. During the first headed run, confirm/fix
each selector against the live UI. Keeping all UI knowledge in this one file means
a Pirate Ship redesign is a one-file fix, not a code change.

The **pure logic** (CSV parse, price guard, buy-safety gates, Odoo client) is fully
unit-tested and does not depend on the selectors:

```
npm test        # node --test — no browser needed
```

## Manual path (always available)

1. In Odoo: Fulfillment → build the label batch → download its CSV (the same file
   the runner uses).
2. In Pirate Ship: *Upload a spreadsheet* → apply `Grove batch v1` → review → Buy.
3. *Export Tracking Data* → in Odoo, import that CSV on the batch (same
   `/labels/batch/<id>/tracking` endpoint the runner POSTs to).

Same file formats, same endpoints, same outcome.
