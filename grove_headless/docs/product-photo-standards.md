# Product Photo Standards — Live System Reference (GOL-837)

Parent: **GOL-818** (image optimization). The storefront image *pipeline* is
correct — full source is delivered and downscaled cleanly. The remaining half of
GOL-818 is an **ingestion** concern the frontend cannot solve: it cannot add
resolution that is not in the uploaded source. This module enforces/surfaces a
minimum-resolution standard at upload so future uploads can't silently regress
storefront photo quality.

## The standard: long edge ≥ 1600px

| Render surface        | Device px (DPR2) | Notes                                  |
| --------------------- | ---------------- | -------------------------------------- |
| Product detail hero   | ~1056            | 528 CSS px @ DPR2                       |
| Grid card             | ~684             |                                        |
| Odoo stored original  | ≤ 1920           | `image_1920` field caps the long edge  |

`GROVE_MIN_IMAGE_LONG_EDGE = 1600` (`models/image_resolution.py`) sits
comfortably above the hero render size while leaving headroom below Odoo's 1920
store cap. A source whose long edge is below 1600 upscales on the detail hero and
reads blurry — there is no frontend fix.

## How it's enforced / surfaced

1. **Upload-time warning (non-blocking).** `product.template` has an
   `@api.onchange("image_1920")` guardrail (`models/product_template.py`) that
   pops a warning when the uploaded photo's long edge is below 1600px. It warns
   rather than rejects, so a content owner can still stage a placeholder — but
   can't upload a low-res photo without seeing it flagged.

2. **Stored, queryable resolution in the admin.** Three stored computed fields
   (`grove_image_width`, `grove_image_height`, `grove_image_low_res`) are shown
   on the product form's **Grove Headless → Photo Quality** section, with a
   warning banner when `grove_image_low_res` is set. Because they're stored, you
   can filter the product list for `Low-resolution Photo = True` to get the
   re-shoot work list without opening each product.

3. **Grove API exposure.** The product-detail endpoint
   (`/grove/api/v1/products/{id}`) returns an `image` block:

   ```json
   "image": {
     "url": "/web/image/product.template/123/image_1920",
     "unique": "a1b2c3…",
     "width": 600, "height": 450,
     "low_res": true,
     "min_long_edge": 1600
   }
   ```

   so content owners / audit tooling can enumerate below-minimum products
   programmatically. `image_url` is retained at the top level for back-compat.

## `image_unique`: the per-photo cache key (GOL-3950)

Every image URL the headless API returns is a versionless `/web/image/…` path.
Odoo serves those through a **31-day** `Cache-Control: immutable` window, so a
replaced photo at the same path can keep serving stale bytes for weeks. To fix
that, every image field now ships an **`image_unique`** sibling the storefront
appends as a cache key (`…/image_1024?unique=<image_unique>`): a new photo yields
a new token, so the URL changes and the cache can't serve the old bytes. It is
also the name the AVIF/WebP pre-encoder (GOL-3926 part C) gives each encoded
file.

**Value.** The checksum (sha1) of the record's stored `image_1920` ir.attachment
— the same bytes `/web/image` serves and the key Odoo's own route caches on —
falling back to the record `write_date` when a stored image has no checksum. A
`product.product` variant with its own photo keys on `image_variant_1920`; a
variant with no own photo serves the template image and so keys on the template's
`image_1920` (a bare variant `write_date` never moves when the shared photo is
replaced). An **imageless** record carries `image_unique: null`, paired with its
`null` URL.

**Where it rides** (one per URL it versions):

| Endpoint                         | Field(s) carrying `image_unique`                              |
| -------------------------------- | ------------------------------------------------------------- |
| `GET /products` (list)           | top-level `image_unique` (beside `image_url`)                 |
| `GET /products/{id}` (detail)    | top-level `image_unique`; `image.unique`; each `variants[].image_unique`; each `images[].image_unique` (hero + gallery) |
| `GET /planner/catalog`           | each `plants[].image_unique` (beside `image`)                 |
| `GET /cart`                      | each `lines[].image_unique`                                   |

Resolved in one batched `ir.attachment` search per page on the list, planner and
cart feeds (`_image_unique_map` in `controllers/main.py`), so the version key
costs one query, not an N+1.

## Imageless products vs low-res products

- **No photo at all** → `image_url` is `null` (`_image_url` gates on the image
  field's truthiness; Odoo serves its own gray placeholder at HTTP 200, so we
  emit null and the frontend renders its branded "Photo coming soon" state).
  These are **not** flagged low-res — `(0, 0)` is "no photo", not "small photo".
- **A real but small photo** → served, and flagged `low_res: true`.
- **A gray placeholder someone actually uploaded** (bytes present) → served as a
  real image; the code cannot distinguish it from a real photo. Fixing those is
  content work (re-shoot list: **GOL-689**).

## Companion work

Higher-resolution real photography for the current below-minimum products is
tracked in **GOL-689**. This module makes those regressions visible; it does not
manufacture the pixels.
