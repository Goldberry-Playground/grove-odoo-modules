# Seed pre-orders on the nursery storefront (design)

**Status:** approved in brainstorm with Josh, 2026-10-09. Target: Release Train #3 (freeze Fri 2026-10-16, QA Mon 10-19, promote Wed 10-21; manifest GOL-2735). Builds on the bareroot pre-order deposit and ship-time settlement (GOL-2233, GOL-2053, GOL-2895) and the shop departments (GOL-2745, grove-sites#892).

## Decision (Josh, 2026-10-09)

Sell tree seed nuts on atthegrovenursery.com as real products with a pack-size choice. Seeds are a **pre-order**: the shopper pays a **$1 deposit** at checkout and the rest is charged **when the order ships**, using the settlement path the tree pre-orders already use. Each seed product has its own **ship window**, **order-by date** and **season cap** in Odoo, because Allegheny Chinquapin must be in the ground before winter and its season ends earlier than other seeds. After a product's order-by date, or once its cap is reached, the page **rolls over to next fall's harvest** and tells the shopper so.

Ruled during brainstorm, do not re-open:

- **Pre-order, $1 deposit per order** (flat, not per line or unit), not charge-in-full.
- **Balance at ship time = rest of the pack price + actual label cost + the existing handling fee** (`grove_headless.shipping_handling_fee`, $5.00 default, GOL-2895 / GOL-2923) + tax. The 10-09 payment links' $5 flat shipping does NOT apply to the site.
- **Per-product season settings**, editable in Odoo: open switch, ship window, order-by date, season cap.
- **After order-by or at the cap: roll over to next fall automatically** (option B), with the shopper clearly told which harvest they are buying from.
- **Season cap defaults to 10 lb per product**, overridable per product in Odoo. Pre-orders count against it by pack weight.
- **Seeds are exempt from the state plant-shipping restrictions** ("it's seed anyways"). The exemption keys on the seed product type, not the per-template `grove_compliance_exempt` flag, so trees of the same genus stay blocked.
- **Department: keep "Seed & scion"** (slug `seed-and-scion`) and list seeds under its **Seed** category. No rename.
- **Button copy:** in season **"Reserve for $1"**; past the season **"Pre-order for fall YYYY, $1"**.
- **Seeds check out on their own**: a cart holding seeds cannot also hold trees or seeds from another harvest year.

## Launch catalog

Both species run about 400 nuts per lb. Prices are Josh's (2026-10-09).

| Product | Botanical | Pack of 10 | Pack of 50 | 1/2 lb (~200) | 1 lb (~400) |
|---|---|---|---|---|---|
| Allegheny Chinquapin seed nuts | *Castanea pumila* | $20 | $30 | $100 | $180 |
| American Hazelnut seed nuts | *Corylus americana* | $7 | $20 | $36 | $65 |

Pack weights for the cap: 0.025 / 0.125 / 0.5 / 1.0 lb.
Chinquapin season: ship window Oct 15 to Nov 15, order by Nov 1, cap 10 lb. Hazelnut season values are entered by Josh at data entry; the code needs no hazelnut-specific value.

## Non-goals

- No seed-specific facet controls in the department sidebar (form / species / ships). Later train.
- No flat seed shipping rate and no change to Box Engine v2 or the ZIP calculator (gom#327/#328, Train #4). Seeds are not quoted at checkout; shipping is the actual label cost at ship time.
- No email capture / "notify me" for closed seasons.
- No change to the tree $10 deposit, tree waves or tree compliance.
- No Odoo `payment_stripe` provider and no Stripe Payment Link sync. The 10-09 payment links (metadata `source=messenger-seeds-2026-10`) are a stopgap and are deactivated by hand after launch.

## Odoo (`grove_headless`, Ada)

### 1. Seed product type

Add `("seed", "Seed")` to `grove_shipping_tier` on `product.template` (`models/product_template.py:463`) and `product.product` (`models/product_product.py:15`). The default stays `potted`.

- `shipping_zones.TIERS` gains `seed`; `SHIPPABLE_TIERS` does **not**. Seeds never enter box packing or a rate quote; `pack_for_state` and the rate feed skip seed lines.
- Seeds bypass the leafed-season gate (`fulfillment` May 1 to Oct 15 window), USDA zone and wave rules.
- **Compliance:** `plant_compliance` returns "allowed" for any line whose tier is `seed`, before genus parsing. A seed with an empty botanical name is NOT fail-safe blocked. Trees are unchanged.

### 2. Seed season fields (template)

New "Seed pre-orders" page on the product form, visible when the tier is `seed`:

| Field | Type | Notes |
|---|---|---|
| `grove_seed_open` | Boolean | Master switch. Off = not purchasable, page says "Not taking reservations right now". |
| `grove_seed_ship_start`, `grove_seed_ship_end` | Date | This season's ship window. |
| `grove_seed_order_by` | Date | Last day an order reserves this season. |
| `grove_seed_cap_lb` | Float, default 10.0 | Season cap in lb. |
| `grove_seed_reserved_lb` | Float, computed, read-only | Sum of pack weight × qty over confirmed, not-cancelled seed lines whose harvest year = current season year. |

`product.product` gets `grove_seed_pack_lb` (Float), set per pack variant.

### 3. Season resolution (pure function, unit tested)

`seed_season(template, today, adding_lb=0) -> {year, ship_start, ship_end, order_by, rolled_over: bool, reason}`

- Current season year = year of `grove_seed_ship_start`.
- If `grove_seed_open` is false → not purchasable.
- If `today <= order_by` and `reserved_lb + adding_lb <= cap_lb` → current season, `rolled_over=False`.
- Otherwise → next season: all three dates + 1 year, `rolled_over=True`, `reason` = `"order_by_passed"` or `"cap_reached"`.
- When Josh edits the dates forward (next year), the "current season" moves with them; no cron is needed.

A cancelled or refunded line drops out of `reserved_lb`, so the weight returns to the cap.

### 4. Pack size axis

Serialize a "Pack size" product attribute as a variant axis next to Cultivar / Format / Rootstock (`controllers/main.py:739-751`), with `pack_lb` per variant. Seed templates use Pack size only.

### 5. Catalog API

Each seed product payload carries `shippingTier: "seed"` and a `seedSeason` object from `seed_season(template, today)`: `{year, shipStart, shipEnd, orderBy, rolledOver, reason, open}`.

### 6. Cart and checkout

- **Deposit:** a seed order charges `SEED_DEPOSIT = 1.00` (USD, flat per order) in `stripe_gateway`, beside `PREORDER_DEPOSIT`. `line_charge` takes the deposit per tier.
- **Harvest year:** each seed `sale.order.line` stores `grove_seed_harvest_year` from `seed_season(..., adding_lb=line weight)` at add time and is re-checked at checkout (a line that would push past the cap moves to next season and the shopper is shown the change before paying).
- **Mixing rule:** refuse an add that would put seeds with trees, or seeds of two harvest years, in one cart. Two seed products in the same harvest year may share a cart (one $1 deposit). Refusal copy: "Seed reservations check out on their own." Reuse the existing refusal channel (grove-sites#1034 red message).
- **Stripe Tax:** the $1 deposit follows the same tax treatment as the $10 tree deposit today.

### 7. Ship-time settlement

Seed orders settle through the existing off-session balance charge on mark-shipped / tracking import (GOL-2053, GOL-2895): balance = seed goods total − the $1 deposit + `grove_actual_shipping_cost` + `_shipping_handling_fee` + tax on the balance. No new charge code; extend the deposit-order predicate to include seed orders. Settlement failures use GOL-3011 handling.

### 8. Back office

- Saved filters: "Seed reservations by harvest year", "Seed balance not charged".
- Confirmation and pre-ship emails (`preorder_email.py`): seed wording, `$1` deposit label, harvest year and ship window, "the rest of the pack price, shipping and handling are charged when it ships".

## Storefront (`grove-sites`, apps/nursery, Iris)

1. **Tier type:** add `"seed"` to `ShippingTier` (`packages/odoo-client/src/types.ts:167`) and every exhaustive switch (shipping-estimate, shipping-hints, fulfillment-method, fulfillment-mode, cart-deposit, buy-state, product-view). Seed products skip the shipping estimator, zone select, method toggle, Format and wave UI.
2. **PDP seed block** (new `seed-preorder-card.tsx`, same radio-card treatment as `option-card.tsx` from GOL-3246):
   - Harvest badge: "Fall YYYY harvest", green in season, amber when `rolledOver`.
   - Ship line: "Ships approx {shipStart} to {shipEnd} · order by {orderBy}".
   - Rolled-over banner: "This season's pre-orders have closed. You're reserving from the fall YYYY harvest, shipping approx {window}." (`reason=cap_reached` says "This season's harvest is fully reserved.")
   - Pack size radio cards with price.
   - Note: "Pay a $1 deposit today. The rest of the pack price, plus shipping and tax, is charged when your order ships. Seed pre-orders check out on their own."
   - CTA: "Reserve for $1" when `!rolledOver`, "Pre-order for fall YYYY, $1" when `rolledOver`.
3. **Cart / checkout:** seed line subline "Reserved, fall YYYY harvest" / "Pre-ordered, fall YYYY harvest", ship window, "$X + shipping charged when it ships"; due today $1.
4. **Department:** Seed & scion renders live with the Seed category once a seed product is published (backend status flip is data).
5. Copy follows the listing style rules (no em dashes, plain educational tone).

## Data (after promote)

Two templates (tier `seed`, botanical names, photos, short descriptions), Pack size variants with prices and `pack_lb`, chinquapin season values above, hazelnut values from Josh, `product.public.category` Seed under Seed & scion, department status `live`. Then deactivate the 10-09 Stripe Payment Links.

## Testing

- **Odoo unit:** `seed_season` (before order-by, after it, at cap, cap freed by cancel, open=false, date edit moves the season); deposit = $1 per order; mixing refusals; settlement total; compliance allows a Castanea seed into FL/WA/OR while a Castanea tree stays blocked; seed lines skipped by `pack_for_state`.
- **Storefront unit/DOM:** CTA and badge switch on `rolledOver`; no zone/Format/wave sections on a seed PDP; pack picker selects the right variant; cart refusal shows.
- **QA e2e (Train #3 gate):** reserve a chinquapin 50-pack for $1 → mark shipped with a test label → balance charged = $29 + label + handling + tax. This also covers the deposit path that no gate exercises today (GOL-2910).

## Rollout and risks

- Ada (Odoo) and Iris (storefront) build in parallel. Both must merge by freeze **Fri 10-16**; Ada's rebase queue (#314 → #319 → #308 → #306) goes first.
- If either half misses freeze: hotfix during the Train #3 QA week, else Train #4 (up 11-02). Chinquapin's order-by is Nov 1, so a Train #4 slip leaves this fall's chinquapin on the payment links.
- Seeds are recalcitrant (must not dry out); packing in damp peat is an operations step, not code.
