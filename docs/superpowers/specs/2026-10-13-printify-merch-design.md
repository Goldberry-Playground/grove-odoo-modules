# Printify T-shirts on the farm storefront (design)

**Status:** approved in brainstorm with Josh, 2026-10-13. Target: Release Train #3 (freeze Fri 2026-10-16, QA Mon 10-19, promote Wed 10-21; manifest GOL-2735). Builds on the headless checkout (`grove_headless`), per-tenant Stripe (`stripe_secret_key_{slug}`), Stripe Tax (GOL-2568) and the shipment status path (`_apply_delivery_status`). Follows the seed spec's pattern of a non-tree product that skips tree rules (2026-10-09-seed-preorders-design.md).

## Decision (Josh, 2026-10-13)

Sell three print-on-demand T-shirts on goldberrygrove.farm/shop. Odoo is the source of truth for what is listed and what it costs; **Printify prints and ships** every shirt order with no human step. The farm shop shows **only** Printify-routed products. The demo catalog goes away.

Ruled during brainstorm, do not re-open:

- **Build in-house in gom** (approach A). Not the OPL "Printify Connector" app (vendor code on a fragile prod, assumes the standard eCommerce flow) and not a storefront-side integration (breaks "Odoo is the source of truth").
- **Products sync from Printify into Odoo** with a re-runnable action. Some shirts exist in Printify today and some do not yet, so the sync runs whenever a new one is ready. Synced products land **unpublished**; Josh publishes.
- **Automatic send to production** on payment. No approval step, no delay window.
- **Shipping = live Printify quote** at checkout. If Printify does not answer, checkout refuses with a plain message. Never a guessed or fallback price.
- **Shirts only on the farm shop.** Remove the mock catalog (flour, freeze-dried, jams, shiitake kit) and stop shared (`company_id = False`) trees from appearing there.
- **Stripe Tax on the farm Stripe account**, set up in this train with the same recipe as the nursery (09-29).
- **One fulfillment route per order.** A cart mixing Printify lines with in-house lines is refused.
- **US only:** 50 states + DC. Printify lines skip the live-tree green list.

Reference for the API: developers.printify.com is the source of truth. `github.com/api-evangelist/printify` (third-party OpenAPI + Postman profile, not an SDK) is a reference and a fixture source for tests only. Use plain `requests`; no new Python dependency.

## Non-goals

- No mixed carts or split shipments.
- No international shipping.
- No vendor bill or Printify-charge reconciliation in Odoo. Printify bills Josh's card on file; v1 records the Printify cost as the product's cost only.
- No Printify product creation or artwork upload from Odoo. Designs are built in Printify.
- No category tabs on the farm shop (three products). Comes back with more products.
- No change to nursery or woodworking storefronts, tree checkout, deposits, waves, Box Engine or compliance for in-house products.

## Odoo (`grove_headless`, Ada)

Everything lives in `grove_headless` (new files `models/printify_client.py`, `models/printify_sync.py`, plus controller routes), not a separate module, because checkout must call the Printify quote and a separate module would need hook plumbing for no gain.

### 1. Configuration

Per-environment env vars, same pattern as `SHIPPO_API_KEY`:

| Env var | Notes |
|---|---|
| `PRINTIFY_API_TOKEN` | Personal access token. 1Password, `AgenticOS Infra` item. |
| `PRINTIFY_SHOP_ID` | The farm's Printify shop (API / custom integration channel). |
| `PRINTIFY_WEBHOOK_SECRET` | Shared secret for webhook signature checks. |
| `PRINTIFY_DRY_RUN` | `1` on QA: create orders but never call `send_to_production`. Unset on prod. |
| `GROVE_STRIPE_TAX_GOLDBERRY` | Existing per-tenant flag. Turned on for the farm at promote. |

Missing token or shop ID: the sync action and checkout raise a clear error; nothing silently degrades.

### 2. Fulfillment route field

`grove_fulfillment_route` Selection on `product.template`: `in_house` (default) | `printify`. Every existing product stays `in_house`, so all current behavior is unchanged.

Mapping fields:

| Model | Field | Notes |
|---|---|---|
| `product.template` | `printify_product_id` (Char, indexed) | Printify product id. |
| `product.product` | `printify_variant_id` (Integer) | Printify variant id (one per size/color). |
| `product.template` | `printify_lead_time` (Char, default "3 to 7 business days") | Production time shown on the PDP and confirmation. Editable per shirt: the Printify product payload carries no production time. |
| `sale.order` | `grove_is_printify` (Boolean, computed, stored) | True when any goods line is on a `printify` product. Used to keep Printify orders out of in-house fulfillment (section 8). |

### 3. Sync from Printify (action, re-runnable)

Button "Sync from Printify" on the product list (farm company). Calls `GET /shops/{shop_id}/products.json` (paginated) and, per Printify product:

- Upserts the template by `printify_product_id`: name, description, images, `grove_fulfillment_route = printify`, `company_id = 1`, `type = consu`, `grove_gate_exempt = True` (not a plant listing).
- Builds Size and Color attributes from the enabled Printify variants and upserts each `product.product` by `printify_variant_id`. Cost = Printify variant cost. Sale price = Printify retail price on first create only; after that Odoo's price wins (Josh edits prices in Odoo).
- A variant disabled or deleted in Printify is **archived** in Odoo, never deleted.
- New templates are created with `website_published = False`.
- Idempotent: a second run with no Printify changes writes nothing.

Respect Printify limits (600 req/min global, 5% error-rate cap): no tight retry loops; a failed page aborts the run with a message.

### 4. Catalog API (farm tenant)

`build_product_domain` gets one rule for the goldberry tenant: a product with `company_id = False` is included only if `grove_fulfillment_route = printify`. Shared trees (American Chestnut, PawPaw) stop appearing on the farm shop with no change to their company. Nursery and woodworking domains are unchanged.

Printify product payloads carry `fulfillment_route: "printify"` and `lead_time` (from `printify_lead_time`) through `_fulfillment_flags`, on both list and detail. Each serialized variant gains `color` and `size` from its Color and Size attribute values (empty string when absent), next to `cultivar` / `format` / `rootstock`. The farm-tenant rule applies to `product_detail`'s inline domain too.

### 5. Cart and checkout

In `_create_draft_order` and `/checkout/quote`:

- **Skip plant rules:** `printify` lines are skipped by the tier/box loop, genus compliance, deposit/preorder logic and the leafed-season gate, the same way `service` lines are skipped today.
- **Destinations:** an all-Printify order may ship to any of the 50 states + DC; it skips the 32-state live-tree green list. `canonical_state_code` also accepts PR, VI, GU, AS and MP, so the check is an explicit 51-code set. Territories, non-US or unrecognized destinations get a plain 400 "Shirts ship to the 50 US states and DC."
- **Mixed carts:** an order with both `printify` and non-`printify` goods lines is refused with 409 "Shirts check out separately from trees."
- **Shipping line:** for an all-Printify order, call `POST /shops/{shop_id}/orders/shipping.json` with the line variant ids, quantities and address; use the `standard` amount as the shipping line. This satisfies the existing "no $0 shipping" 409 check unchanged.
- **Quote twice, charge the server's:** `/checkout/quote` returns `shipping_amount` (float, or null for non-Printify carts, whose response is otherwise unchanged) and `fulfillment_route` for display, and quotes Printify carts once the request's `shipping` carries `state` and `zip` (null until then); `checkout_session` quotes again server-side and that amount is charged. `_apply_shipping_line` is not used for Printify orders; a dedicated branch writes the GROVE-SHIP line.
- **Timeout:** 5 s. Timeout or non-2xx → 503 "Shipping is temporarily unavailable. Try again in a few minutes." No fallback price.
- **Tax:** Stripe Tax through the existing `automatic_tax` path when `GROVE_STRIPE_TAX_GOLDBERRY` is on. Apparel lines use tax code `txcd_30011000`; the shipping line follows the existing shipping tax code.

### 6. Order push (after payment)

On `checkout.session.completed` for an order with Printify lines:

1. Queue a push job (cron-driven, not inline in the webhook, so the Stripe webhook stays fast).
2. `POST /shops/{shop_id}/orders.json` with `external_id = sale_order.name`, the ship-to address, `line_items: [{product_id, variant_id, quantity}]`, `shipping_method = 1` (standard), `send_shipping_notification = false` (the Grove sends its own shipment email).
3. Unless `PRINTIFY_DRY_RUN`, `POST /orders/{id}/send_to_production.json`.
4. Store `printify_order_id`, `printify_state` on `sale.order`.

`external_id` makes a retried push idempotent: before creating, look up the order by `external_id`; if it exists, reuse it.

Failure: retry with backoff (5 min, 30 min, 2 h), then stop, set `printify_state = push_failed`, and post a Discord alert through the existing order-alert channel. A "Retry Printify push" button on the sale order re-queues it.

The existing new-order alert and merchant email fire as today.

### 7. Printify webhooks

New route `POST /grove/api/v1/printify/webhook`, auth = HMAC-SHA256 signature of the body with `PRINTIFY_WEBHOOK_SECRET` (reject with 401 on mismatch; verify the exact header name against developers.printify.com). Match the order by `printify_order_id` (fallback `external_id`).

| Event | Action |
|---|---|
| `order:sent-to-production` | `printify_state = in_production` |
| `order:shipment:created` | Write `grove_tracking_numbers` and `grove_shipping_carriers` (newline-joined, index-aligned, as the hand-label path does) FIRST, then call `_apply_delivery_status(env, order, "transit", tracking, source="printify")`. `"transit"` (not `"shipped"`, which the function does not rank) moves the order to shipped and sends the existing shipment email. |
| `order:shipment:delivered` | `_apply_delivery_status(..., "delivered", ..., source="printify")` |
| `order:updated` with canceled / on-hold | Update `printify_state`, Discord alert. |

Unknown events: 200 and ignore. Duplicate events are harmless (status writes are idempotent).

A one-shot script (or server action) registers these webhooks through `POST /shops/{shop_id}/webhooks.json` with the prod URL and secret. No dashboard clicking.

### 8. Keep Printify orders out of in-house fulfillment

- Pirate Ship label batch (`label_batch._eligible_orders`): add `("grove_is_printify", "=", False)`. A paid shirt order sits at `awaiting_label` and would otherwise be packed in-house.
- UPS/USPS carrier poll (`sale_order._poll_carrier_tracking`): add the same term. Printify webhooks own those orders' status.

### 9. Back office

- Sale order: "Printify" group showing `printify_order_id`, `printify_state`, and the Retry button.
- Saved filter: "Printify push failed".
- The weekly rollup and digest count Printify orders like any shipped order; no Printify-specific section.

## Storefront (`grove-sites`, apps/goldberry, Iris)

1. **Remove the demo catalog:** delete `data/mock-products.ts`, the "placeholders" banner, and the Flour / Freeze-dried / Jams / Mushrooms tabs (`SHOP_CATEGORIES`, `filterByCategory`). On Odoo failure or an empty list, show "The shop is restocking. Check back soon." instead of fake products.
2. **Shop grid** from `odoo.products.list` only.
3. **PDP:** Color and Size pickers mapped to Odoo variants, size-chart note, "Printed to order, ships in {leadTime}" line from the payload. No format, wave, deposit or zone UI (already nursery-only).
4. **Checkout page:** live shipping from `/checkout/quote` once the shopper has entered state and ZIP ("Enter your address" until then); render the 503 and 409 messages in the existing refusal channel; submit is blocked while the quote is unavailable. The cart page keeps "At checkout" because it has no address.
5. **Confirmation:** "Your shirt is printed to order and usually ships in {leadTime}. We'll email tracking when it's on the way."
6. Copy follows the listing style rules (no em dashes, plain tone).

Reference mockup: brainstorm widget `goldberry_printify_checkout_mockup` (2026-10-13).

## Data and setup

1. Josh finishes the remaining shirt(s) in Printify (shop on the API / custom integration channel).
2. Set `PRINTIFY_*` env vars on QA and prod (Josh's terminal; secrets from 1Password).
3. Farm Stripe Tax: WV registration, head office and defaults on the Goldberry Stripe account via API (nursery recipe, 09-29). Needs a restricted live key for the farm account in the vault.
4. Run "Sync from Printify", set prices, publish the three shirts.
5. Register the Printify webhooks (script).
6. Turn on `GROVE_STRIPE_TAX_GOLDBERRY`.

## Testing

- **Odoo unit (Printify mocked, fixtures from the Postman collections):** sync creates templates and variants; second sync writes nothing; disabled variant is archived; Odoo price survives re-sync; push sends `external_id` and reuses an existing order on retry; backoff then `push_failed` + alert; dry run skips `send_to_production`; webhook bad signature → 401; shipment webhook calls `_apply_delivery_status` with tracking; quote timeout → 503; mixed cart → 409; Printify order to a non-green state (e.g. CA) is allowed while a tree to CA stays blocked; farm catalog excludes `company_id = False` in-house products; **nursery checkout and catalog unchanged** (existing suites green).
- **Storefront unit/DOM:** no mock fallback; variant pickers select the right variant; quote loading / 503 / 409 states render.
- **QA e2e (Train #3 gate):** with `PRINTIFY_DRY_RUN=1`, buy a shirt on the QA farm site with a Stripe test card → Printify order exists with `external_id` = SO name and is NOT in production → fire a test shipment webhook → shipment email sent, order shows shipped.
- **Prod check after promote:** one real order, one shirt shipped to Josh.

## Rollout and risks

- **Tight freeze:** spec approved 10-13, freeze Fri 10-16. Ada (Odoo, sections 1 to 9) and Iris (storefront) build in parallel. If the Odoo half misses freeze, the storefront cleanup (item 1) can still ship alone, and **shirts stay unpublished** until the push is live (never sell what cannot be fulfilled). Next chance: Train #4 (up 11-02).
- **Prod fragility:** `grove_headless` update on prod must pass QA first (severed external IDs history). New fields only; no data migration of existing records.
- **Printify error-rate cap (5%):** bounded retries keep the account under it.
- **Live quote dependency:** a Printify outage blocks shirt checkout (by decision). Tree checkout is unaffected.
- **Printify notifications:** `send_shipping_notification = false` so customers do not get two shipping emails.
