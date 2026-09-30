# Stripe Tax cutover & rollback (GOL-2568)

Sales-tax compliance for headless web orders moves from Odoo's computed WV
tax line to **Stripe Tax** (`automatic_tax` on the Checkout Session). The
behaviour is governed per tenant by an environment flag so the cutover and its
rollback are a single, reversible switch.

## The flag

```
GROVE_STRIPE_TAX_{TENANT}   # e.g. GROVE_STRIPE_TAX_NURSERY=1
```

- Keyed by the order's storefront tenant slug (`goldberry` / `ggg` /
  `nursery`), exactly like `SHIPPO_API_KEY` and the `GROVE_PUBLISH_*` vars.
- Truthy values: `1`, `true`, `yes`, `on` (case-insensitive). Anything else —
  including unset, empty, or an unresolvable tenant — is **OFF**.
- Read by `grove_headless.controllers.main._stripe_tax_enabled(order)`.

**OFF is the default AND the rollback.** With the flag off, every Stripe Tax
branch is skipped and the order keeps today's Odoo-computed WV tax line, byte
for byte:

| Seam | Flag ON | Flag OFF (default / rollback) |
|------|---------|-------------------------------|
| Checkout Session | Stripe Customer + `automatic_tax[enabled]=true`; per-line `tax_code` | no Customer, no `automatic_tax` |
| `_build_stripe_line_items` | WV tax line dropped (Stripe adds it) | explicit "Sales tax (WV)" line emitted |
| `checkout.session.completed` webhook | write back `total_details.amount_tax` + jurisdictions | no write-back; Odoo tax stands |
| Ship-time settlement (GOL-2233) | Stripe Tax calculation + `tax/transactions` | Odoo tax on the settlement base |

## Cutover (promote — a separate named step)

1. Merge #284 to `main` (rides Train #2, promote Wed 10-07, manifest GOL-2584).
2. Confirm Stripe **test-mode** WV registration is mirrored before the QA gate
   (issue step 7 / spec §F) — Stripe Tax has independent settings per mode.
3. Set `GROVE_STRIPE_TAX_NURSERY=1` in the tenant's Odoo environment (compose
   `environment:` in odoocker; Odoo only reads `os.environ` from there).
4. Restart Odoo so the new env is live.

## Rollback

Unset (or set to `0`) `GROVE_STRIPE_TAX_{TENANT}` and restart Odoo. The order
path reverts to Odoo's WV tax computation with no code change and no data
migration. In-flight sessions already handed to Stripe with `automatic_tax`
settle normally; new sessions use the Odoo line again.

## QA e2e verification (Gate 4)

Run against QA Odoo with `sk_test` keys and `GROVE_STRIPE_TAX_NURSERY=1`.
Both cases must **execute and assert amounts** — do not gate the assertion on
key presence (no self-skip):

1. **WV address** (e.g. Summersville 26651): Stripe returns 6% destination
   tax; the session `total_details.amount_tax` is non-zero and the webhook
   write-back records `grove_stripe_tax_amount` == Stripe's amount, with "West
   Virginia" in `grove_stripe_tax_jurisdictions`.
2. **Non-WV address** (e.g. an OH ship-to): Stripe returns **$0** tax (no
   nexus registered outside WV) — same outcome as today's
   `_apply_destination_tax`; write-back records `0.00`.

Assert the customer invoice total equals the Stripe charge in both cases.

## Rollback test (Gate 3, on QA)

With `GROVE_STRIPE_TAX_NURSERY` unset, repeat the WV checkout: the Odoo
"Sales tax (WV)" line is present on the charge and no Stripe write-back occurs
— proving the flag-off path falls back to the Odoo computation.
