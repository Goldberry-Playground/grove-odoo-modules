# Stripe Tax on the headless checkout (design)

**Status:** approved in brainstorm with Josh, 2026-09-29. Board: GOL-2568 (Ada). Supersedes the tax-line rules in the 2026-09-22 promo/Review & pay ruling (single `WV Sales Tax (6%)` line computed by Odoo) for the web checkout path only.

## Decision (Josh, 2026-09-29)

Sales-tax compliance for the storefronts moves to **Stripe Tax**. Stripe calculates destination tax on every Checkout Session from the ship-to address and per-line tax codes; Odoo stops computing sales tax for web orders and records what Stripe charged. Stripe's threshold monitoring watches every state; an Odoo cron adds an early warning.

Stripe Tax is **already active** on the At The Grove Nursery Stripe account (`acct_16cKwAEkDEtb2GgD`), configured via the Tax API on 2026-09-29:

- registration: US / West Virginia, `state_sales_tax`, active from 2026-09-29 (`taxreg_1UL3jYEkDEtb2GgD0cn1g28P`);
- head office: 2291 Armstrong Road, Summersville WV 26651;
- defaults: tax code `txcd_99999999` (General – Tangible Goods), tax behavior `exclusive`.

No other state is registered, so Stripe returns zero tax for out-of-state ship-tos, which matches today's `_apply_destination_tax` outcome. Goldberry Grove and GGG have their own Stripe accounts and get the same setup when their storefronts take orders.

Ruled during brainstorm, do not re-open:

- **Address route:** the backend creates or updates a Stripe **Customer** carrying the ship-to address from our own checkout form and passes it to the session. `shipping_address_collection` stays off; the customer never types the address twice.
- **Odoo representation:** on payment, the order's Odoo-computed taxes are replaced by one tax line per jurisdiction in Stripe's breakdown, amount exactly as charged, mapped to the existing WV state tax account. The invoice equals the Stripe charge to the cent.
- **Deposits:** the off-session balance at ship time is taxed by a Stripe Tax Calculation at settlement and recorded as a Stripe tax transaction.
- **Cutover:** a per-tenant env flag (`GROVE_STRIPE_TAX_{TENANT}=1`). Off is today's behavior byte for byte; the flag is also the rollback. Nursery flips on at Train #2 after the QA gate.
- **Monitoring:** Stripe's built-in threshold monitoring (all supported states, home-state WV excluded) is the source of truth; an Odoo nightly cron gives an 80% early warning to Discord.

## Non-goals

- POS and manual quotations keep Odoo's own tax computation.
- Threshold monitoring settings and notification preferences (Stripe dashboard, account owner).
- Filing: WV returns stay manual on MyTaxes until a second state registers, then Stripe filing (TaxJar) is evaluated.
- Removing the WV tax records or the `setup_wv_sales_tax` hook (still needed for POS and for the flag-off path).
- International, VAT, tax-ID collection.

---

## A. Checkout Session

**Where:** `grove_headless/models/stripe_gateway.py` (`create_checkout_session`, new `ensure_customer`), `grove_headless/controllers/main.py` (`_build_stripe_line_items`, `_create_draft_order`, session creation).

1. **Flag.** `stripe_tax_enabled(tenant)` reads `GROVE_STRIPE_TAX_{TENANT}` (`1`/`true`). When off, nothing in this section runs.
2. **Customer.** Before creating the session, `ensure_customer(secret_key, email, name, shipping_address)` searches Stripe Customers by email (`/v1/customers/search`, `email:'…'`), creates one if absent, and sets `shipping[address]` (line1, city, state, postal_code, country=US) and `shipping[name]` from the order's ship-to. Pickup orders use the farm address (company 3 partner). The session then passes `customer=<id>` and `customer_update[shipping]=auto`; `customer_email` is dropped when a customer id is passed. The Stripe customer id is stored on `sale.order.grove_stripe_customer_id` for the settlement path.
3. **Line items.** Every goods line carries `price_data[product_data][tax_code]`: `txcd_99999999` by default; gift cards `txcd_10502000`; bundles inherit goods. The `GROVE-SHIP` line carries `txcd_92010001` (Shipping) so Stripe applies each state's shipping-taxability rule. `price_data[tax_behavior]=exclusive` on every line.
4. **No Odoo tax line.** `_build_stripe_line_items` emits no `kind: "tax"` item when the flag is on; the `tax_today` accumulation is skipped. Discount handling (one-time coupon on the discounted base) is unchanged; Stripe taxes the post-coupon amount.
5. **Session params.** `automatic_tax[enabled]=true`. `payment_intent_data[setup_future_usage]=off_session` stays (deposits).
6. **Draft order.** `_create_draft_order` still applies Odoo's WV tax to the draft so the Review & pay estimate and the fulfilment code paths keep working. That amount is labelled an estimate (section E) and is overwritten on payment (section B).

**Error handling:** `ensure_customer` failure (Stripe 4xx/5xx) falls back to the flag-off session (Odoo tax line, `customer_email`) and logs at WARNING with the order name, so checkout never blocks on the customer call. A session created with `automatic_tax` that Stripe cannot calculate (missing address) fails at Stripe with a clear error; the storefront shows the existing "try again" message and the incident is logged.

## B. Write-back on payment

**Where:** `controllers/main.py` webhook handler for `checkout.session.completed` (~L1691, `_handle_*`), `models/sale_order.py`.

1. Retrieve the session with `expand[]=total_details.breakdown` (or read `total_details.breakdown.taxes` from the event payload when present).
2. New `sale.order._grove_apply_stripe_tax(breakdown)`: remove all sale taxes from the order lines (purchase taxes untouched), then for each jurisdiction in the breakdown find-or-create a percent `account.tax` named `Stripe Tax · <jurisdiction display_name>` with the effective rate `amount_tax / taxable_amount` rounded to 4 dp, `company_id` of the tenant, `tax_group_id` = the existing WV group so accounting reports are unchanged, and apply it to every taxable line (Odoo fixed-amount taxes are per unit, so a percent tax is the only shape that reproduces Stripe's total). Then assert `abs(order.amount_tax − stripe_amount_tax) < 0.01`; if rounding leaves a cent, add a one-line rounding adjustment so the invoice total equals the Stripe charge exactly.
3. Store `grove_stripe_tax_amount`, `grove_stripe_tax_breakdown` (Json) and `grove_stripe_tax_transaction` (Stripe `tax.transaction` id when Stripe creates one for the session) on the order; chatter posts "Tax per Stripe: $X (WV 6.00%)".
4. Zero-tax sessions (out-of-state) clear the order's sale taxes.

## C. Deposits and ship-time settlement

**Where:** `controllers/main.py` `settle_order_at_ship`, `_settlement_shipping_line`, `stripe_gateway.create_payment_intent`.

1. When the flag is on, before creating the off-session PaymentIntent, call `POST /v1/tax/calculations` with `customer=<grove_stripe_customer_id>` (address on the customer), the balance line items with their tax codes, and the actual shipping line with `txcd_92010001`. Use `tax_amount_exclusive` as the tax to charge; the PaymentIntent amount = balance + shipping + that tax.
2. After the PaymentIntent succeeds, `POST /v1/tax/transactions/create_from_calculation` with the calculation id and the order name as `reference`, and store the transaction id on the order. On refund, `create_reversal` (out of scope for v1 beyond a TODO and a test that asserts the id is stored).
3. The settlement email lines already itemise tax; they read the Stripe amount.

## D. Nexus early warning

**Where:** `models/sale_order.py` `_cron_nexus_watch`, `data/nexus_watch_cron.xml`, `data/nexus_thresholds.json`.

1. Nightly 07:00 America/New_York. For each ship-to state (excluding WV), sum `amount_total` of paid web orders minus refunds over (a) the current calendar year and (b) the trailing 12 months, and count orders.
2. Compare to `nexus_thresholds.json`: `{state: {amount, transactions|null, window}}`, seeded from Stripe's supported-locations table and reviewed once a year (a comment in the file records the review date).
3. Post one Discord ops message when any state is at ≥80% or ≥100% of either measure, listing state, sales, threshold, window, and a link to Stripe's Tax → Locations page. No message otherwise. Stripe's own monitoring remains the authority; this is a heads-up.

## E. Storefront (grove-sites)

Review & pay: the tax row label becomes `Estimated tax` when the tenant flag is on (the BFF passes `taxEstimated: true` from the quote endpoint); the Stripe page shows the charged figure. No other change. Iris, small follow-up.

## F. QA and test mode

Stripe test mode has its own Tax settings. Before the QA gate, mirror the prod setup on the nursery **test** keys via the same three API calls (registration WV, head office, defaults). The e2e promo/checkout specs on QA then exercise real Stripe Tax calculations in test mode.

## Testing

- **Unit (pytest, fixtures):** session payload with flag on has `automatic_tax`, `customer`, no `kind: tax` item, tax codes on every line; flag off is byte-identical to today; `ensure_customer` search/create/update paths and the fallback on error.
- **Odoo tests:** `_grove_apply_stripe_tax` on a WV order (6% goods + shipping), an OH order (zero, taxes cleared), a FLATWOODS-discounted WV order (tax on the discounted base), a WV pickup; invoice total equals the Stripe amount to the cent, including a rounding case.
- **Settlement:** calculation request shape, PaymentIntent amount includes the tax, transaction id stored.
- **Nexus cron:** threshold math incl. refunds, the 80%/100% messages, no message under 80%, WV excluded.
- **QA gate:** the four scenarios above run against Stripe test mode with Tax mirrored.

## Rollout

1. Manifest bump; new fields are nullable; no data migration.
2. odoocker env passthrough for `GROVE_STRIPE_TAX_NURSERY` (QA + prod), default empty.
3. Train #2 (Oct 5–7): flag on for nursery on QA → gate → promote → flag on for prod in the same touch as the pin bump. Flag off = rollback.
4. Josh: keep the WV registration current (account number when confirmed on GOL-67); confirm the Stripe account-owner email receives threshold alerts.
5. Vault: `Software/Grove Shipping.md` and `Odoo ERP.md` get a "Sales tax = Stripe Tax" section after the promote.

## Open items

- Gift-card tax code: confirm `txcd_10502000` is the Stripe code for gift cards at build time (verify against `/v1/tax_codes`).
- Goldberry / GGG Stripe accounts need the same Tax setup before their flags flip.
- Refund reversals for tax transactions (v1 stores the id; reversal is a follow-up).
