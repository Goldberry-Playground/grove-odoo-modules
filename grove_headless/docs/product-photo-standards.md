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
     "width": 600, "height": 450,
     "low_res": true,
     "min_long_edge": 1600
   }
   ```

   so content owners / audit tooling can enumerate below-minimum products
   programmatically. `image_url` is retained at the top level for back-compat.

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
