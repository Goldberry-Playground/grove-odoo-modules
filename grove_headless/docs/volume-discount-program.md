# Volume-discount loyalty program (GOL-2431)

The automatic 5+/10+ volume tiers are **data, not code**: they live in an Odoo
`loyalty.program` so marketing can change thresholds/percentages without a
deploy. The headless backend only *reads* the program (best-single-discount
resolution, the `/promotions/auto` feed) — it never creates it in production.

- **QA:** the engineer creates this program (safe to write).
- **Production:** Josh creates it by hand (or gives an explicit go), using the
  exact shape below.

## Program shape

Create under **Sales → Products → Discount & Loyalty** (or Settings → Promotions)
in the **At The Grove Nursery** company.

| Field | Value |
| --- | --- |
| Program Name | `Volume discount` |
| Program Type | `Promotions` (`program_type = promotion`) |
| Company | `At The Grove Nursery` |
| Available on | Sales **and** eCommerce (Website) |
| Trigger | **Automatic** (`trigger = auto`) — no code |
| Applies on | Current order (`applies_on = current`) |
| Active window | leave open, or set Start/End for a seasonal promo |

### Rule (points)

One rule that awards **1 point per qualifying-plant unit**:

| Field | Value |
| --- | --- |
| Mode | Automatic (`mode = auto`) |
| Reward point mode | Per unit (`reward_point_mode = unit`) |
| Points | `1.0` (`reward_point_amount`) |
| Products | restrict to the **Plants** category (`product_category_id`) — or the plant products / a plant product tag. **Do not** leave it catalog-wide, or supplies and gift cards would earn tier points. |

### Rewards (two tiers)

Two order-discount rewards, priced in points. "5+ trees" = 5 points, "10+" = 10.

| Reward | `reward_type` | `discount_mode` | `discount` | `discount_applicability` | `required_points` | Description |
| --- | --- | --- | --- | --- | --- | --- |
| 10% off (5+ trees) | discount | percent | `10` | order | `5` | `10% off (5+ trees)` |
| 20% off (10+ trees) | discount | percent | `20` | order | `10` | `20% off (10+ trees)` |

The backend applies **only the single highest-value claimable reward** — at 10+
units both rewards are affordable, and it keeps the 20% one (never both).

## How the backend uses it

- **Best single discount wins.** Every ships-now checkout resolves the automatic
  tier *and* any promo code, then applies whichever saves more — never both. The
  loser's reward line is never written. See `models/promotions.py::resolve_discounts`.
- **Deposit/preorder carts get neither** (same gate as promo codes; CEO directive
  2026-09-06). Revisit at ship-time settlement.
- **Storefront nudge.** `GET /grove/api/v1/promotions/auto` returns a bare JSON
  array `[{min_qty, percent, label}, ...]` derived from this program (min_qty
  = `required_points / points-per-unit`), so the storefront can say
  "Add 2 more trees to unlock 10% off". It is deliberately a top-level array, not
  a `{"tiers": [...]}` wrapper — the storefront normalizer reads the array
  directly (GOL-2439 contract; a wrapper parses as empty and hides the nudge).
- **Stripe.** The Odoo-computed discount rides to Stripe as a one-time coupon on
  the Checkout Session (`stripe_gateway.create_coupon`), exactly as promo codes
  already do. No Stripe promotion codes are ever used.

## What counts as a "tree"

Any nursery **plant** product unit — a product under the `grove_headless.categ_plants`
root. A phantom-BOM **bundle** counts as its component tree count (Remembrance
Grove = 5). Supplies, gift cards and services never count and are never
discounted. The two places this matters:

1. The **loyalty rule's** product restriction (above) governs which lines earn
   tier points — set it to the Plants category.
2. The backend's `qualifying_tree_count` (feed nudge maths + component counting)
   uses the same Plants-root test and explodes bundles via their phantom BOM.

Keep the two in agreement: if marketing broadens what earns a tier, widen the
rule's product set to match.
