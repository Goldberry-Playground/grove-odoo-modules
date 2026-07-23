# grove_headless Catalog API v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the batched `grove_headless` API changes from the 2026-07-13 nursery product-pages spec — growing-facts fields, variant-level shipping tier, tags, richer list/detail serialization, and a ZIP→zone endpoint.

**Architecture:** One Odoo 19 module PR. New template fields carry filterable growing facts; a computed variant field fixes the bareroot-ships-at-potted-rates bug; the public JSON controller grows `tag_id`/`zone` filters plus `tags`, `price_min`, `variant_count`, `facts`, `images[]`, and structured `variants[]`; a new `/zone` endpoint wraps the existing `usda_zone_for_zip()`. Filter-domain logic is extracted to a pure-Python module so it tests under plain pytest (repo convention: pure modules test standalone; DB-touching tests are `TransactionCase` under Odoo's runner).

**Tech Stack:** Odoo 19 (`product.template`, `product.product`, `product.tag`, website_sale `product.image`), XML-RPC-free public HTTP controller, pytest + `odoo.tests.TransactionCase`.

**Spec:** `grove-sites/docs/superpowers/specs/2026-07-13-nursery-product-pages-design.md` (PR #123).

## Global Constraints

- Module version bumps ONCE, in the final task: `19.0.1.5.0` → `19.0.1.6.0`.
- Lint: `ruff check . --select E,F,I --line-length 120` and `ruff format --check . --line-length 120` must pass (repo CI).
- API rules from repo CLAUDE.md: explicit field lists (never `*`), plain JSON via `_json_response()`, company isolation via `request.website`.
- All new controller params are optional and backward-compatible; existing consumers (grove-sites odoo-client) must keep working unchanged.
- Attribute-name string contracts (live QA data): Cultivar axis is named `Cultivar`, format axis is named `Format` with values `Potted` / `Bareroot`.
- Branch: `feat/catalog-api-v1` off `main` in grove-odoo-modules. Odoo-runner test command (from the odoocker checkout's local stack):
  `docker compose -f docker-compose.yml -f docker-compose.override.local.yml exec odoo odoo -d odoo -u grove_headless --test-tags /grove_headless --stop-after-init` → expect `0 failed`.

---

### Task 1: Growing-facts fields on product.template

**Files:**
- Modify: `grove_headless/models/product_template.py` (append after `grove_shipping_tier`, ~line 60)
- Test: `grove_headless/tests/test_growing_facts.py` (create)

**Interfaces:**
- Produces: template fields `grove_botanical_name` (Char), `grove_zone_min`/`grove_zone_max` (Integer), `grove_layer` (Selection: canopy/understory/shrub/ground/vine), `grove_sun` (Selection: full/partial/shade), `grove_mature_size` (Char), `grove_spacing` (Char), `grove_soil` (Char). Constraint: `zone_min ≤ zone_max` when both set. Consumed by Tasks 3, 4.

- [ ] **Step 1: Write the failing test**

```python
# grove_headless/tests/test_growing_facts.py
"""Growing-facts fields (2026-07-13 catalog spec). DB tests — Odoo runner only."""

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestGrowingFacts(TransactionCase):
    def _tmpl(self, **vals):
        base = {"name": "Test Pear", "type": "consu"}
        base.update(vals)
        return self.env["product.template"].create(base)

    def test_facts_fields_exist_and_store(self):
        t = self._tmpl(
            grove_botanical_name="Pyrus communis",
            grove_zone_min=5,
            grove_zone_max=8,
            grove_layer="canopy",
            grove_sun="full",
            grove_mature_size="15-20 ft x 12 ft",
            grove_spacing="15 ft",
            grove_soil="well-drained",
        )
        self.assertEqual(t.grove_botanical_name, "Pyrus communis")
        self.assertEqual((t.grove_zone_min, t.grove_zone_max), (5, 8))
        self.assertEqual(t.grove_layer, "canopy")
        self.assertEqual(t.grove_sun, "full")

    def test_zone_range_constraint(self):
        with self.assertRaises(ValidationError):
            self._tmpl(grove_zone_min=8, grove_zone_max=5)

    def test_zones_optional(self):
        t = self._tmpl()
        self.assertFalse(t.grove_zone_min)
        self.assertFalse(t.grove_botanical_name)
```

- [ ] **Step 2: Run test to verify it fails**

Run the Odoo-runner command from Global Constraints.
Expected: FAIL — `Invalid field 'grove_botanical_name'` on create.

- [ ] **Step 3: Write minimal implementation**

Append to `grove_headless/models/product_template.py` (inside `ProductTemplate`; add `from odoo.exceptions import ValidationError` and `api` to the existing imports if missing — `api` is already imported):

```python
# ── Growing facts (2026-07-13 catalog spec) ─────────────────────────
# Filterable facts live here (typed); display-only facts stay Char.
# Narrative content deliberately does NOT live in Odoo (Ghost, keyed
# by grove_slug — see the nursery product-pages spec).
grove_botanical_name = fields.Char(string="Botanical Name")
grove_zone_min = fields.Integer(string="USDA Zone Min")
grove_zone_max = fields.Integer(string="USDA Zone Max")
grove_layer = fields.Selection(
    [
        ("canopy", "Canopy"),
        ("understory", "Understory"),
        ("shrub", "Shrub"),
        ("ground", "Ground cover"),
        ("vine", "Vine"),
    ],
    string="Food Forest Layer",
)
grove_sun = fields.Selection(
    [("full", "Full sun"), ("partial", "Partial sun"), ("shade", "Shade")],
    string="Sun Requirement",
)
grove_mature_size = fields.Char(string="Mature Size")
grove_spacing = fields.Char(string="Plant Spacing")
grove_soil = fields.Char(string="Soil")


@api.constrains("grove_zone_min", "grove_zone_max")
def _check_zone_range(self):
    for record in self:
        if record.grove_zone_min and record.grove_zone_max and record.grove_zone_min > record.grove_zone_max:
            raise ValidationError("USDA zone min cannot exceed zone max.")
```

- [ ] **Step 4: Run test to verify it passes**

Same Odoo-runner command. Expected: PASS (`0 failed`).

- [ ] **Step 5: Commit**

```bash
git add grove_headless/models/product_template.py grove_headless/tests/test_growing_facts.py
git commit -m "feat(grove_headless): growing-facts fields on product.template (catalog spec)"
```

---

### Task 2: Variant-level effective shipping tier (bareroot rate-quote fix)

**Files:**
- Create: `grove_headless/models/product_product.py`
- Modify: `grove_headless/models/__init__.py` (add `from . import product_product`)
- Modify: `grove_headless/models/sale_order.py:59`
- Test: `grove_headless/tests/test_effective_shipping_tier.py` (create)

**Interfaces:**
- Consumes: template `grove_shipping_tier` (existing); Format attribute contract from Global Constraints.
- Produces: `product.product.grove_effective_shipping_tier` (computed Selection, values `bareroot`/`potted`) — a Bareroot Format variant is `bareroot` regardless of the template field; everything else falls back to the template field (default `potted`). Consumed by Task 4 serializer and `sale_order` label purchasing.

- [ ] **Step 1: Write the failing test**

```python
# grove_headless/tests/test_effective_shipping_tier.py
"""Variant-level shipping tier (fixes bareroot variants quoting potted rates)."""

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestEffectiveShippingTier(TransactionCase):
    def setUp(self):
        super().setUp()
        self.fmt = self.env["product.attribute"].create({"name": "Format", "create_variant": "always"})
        self.v_potted = self.env["product.attribute.value"].create({"name": "Potted", "attribute_id": self.fmt.id})
        self.v_bareroot = self.env["product.attribute.value"].create({"name": "Bareroot", "attribute_id": self.fmt.id})

    def test_bareroot_variant_overrides_template_tier(self):
        tmpl = self.env["product.template"].create(
            {
                "name": "Tier Pear",
                "type": "consu",
                "grove_shipping_tier": "potted",
                "attribute_line_ids": [
                    (0, 0, {"attribute_id": self.fmt.id, "value_ids": [(6, 0, [self.v_potted.id, self.v_bareroot.id])]})
                ],
            }
        )
        tiers = {
            v.product_template_variant_value_ids.name: v.grove_effective_shipping_tier for v in tmpl.product_variant_ids
        }
        self.assertEqual(tiers["Bareroot"], "bareroot")
        self.assertEqual(tiers["Potted"], "potted")

    def test_no_format_axis_falls_back_to_template(self):
        tmpl = self.env["product.template"].create(
            {"name": "Plain Aronia", "type": "consu", "grove_shipping_tier": "bareroot"}
        )
        self.assertEqual(tmpl.product_variant_id.grove_effective_shipping_tier, "bareroot")

    def test_default_is_potted(self):
        tmpl = self.env["product.template"].create({"name": "Untagged", "type": "consu"})
        self.assertEqual(tmpl.product_variant_id.grove_effective_shipping_tier, "potted")
```

- [ ] **Step 2: Run test to verify it fails**

Odoo-runner command. Expected: FAIL — `grove_effective_shipping_tier` not a field.

- [ ] **Step 3: Write minimal implementation**

```python
# grove_headless/models/product_product.py
from odoo import api, fields, models

FORMAT_ATTRIBUTE = "Format"
BAREROOT_VALUE = "Bareroot"


class ProductProduct(models.Model):
    _inherit = "product.product"

    # The zone-rate engine bills bareroot as a 4 lb slim box and potted as a
    # ~25 lb box. Format is a VARIANT axis on live data, so the tier must be
    # resolved per-variant — the template field alone quotes bareroot pears
    # at potted rates (bug found in the 2026-07-13 design review).
    grove_effective_shipping_tier = fields.Selection(
        [("bareroot", "Bareroot"), ("potted", "Potted")],
        compute="_compute_grove_effective_shipping_tier",
        string="Effective Shipping Tier",
    )

    @api.depends("product_template_variant_value_ids", "product_tmpl_id.grove_shipping_tier")
    def _compute_grove_effective_shipping_tier(self):
        for product in self:
            fmt_values = product.product_template_variant_value_ids.filtered(
                lambda v: v.attribute_id.name == FORMAT_ATTRIBUTE
            )
            if fmt_values and fmt_values[0].name == BAREROOT_VALUE:
                product.grove_effective_shipping_tier = "bareroot"
            else:
                product.grove_effective_shipping_tier = product.product_tmpl_id.grove_shipping_tier or "potted"
```

In `grove_headless/models/__init__.py` add `from . import product_product` alongside the existing imports. In `grove_headless/models/sale_order.py` replace line 59:

```python
                tier = tmpl.grove_shipping_tier or "potted"
```

with:

```python
                tier = line.product_id.grove_effective_shipping_tier or "potted"
```

- [ ] **Step 4: Run tests to verify pass (including existing shippo/sale tests)**

Odoo-runner command. Expected: PASS, and pre-existing `test_shippo_client.py` / `test_shipping_zones.py` stay green.

- [ ] **Step 5: Commit**

```bash
git add grove_headless/models/product_product.py grove_headless/models/__init__.py grove_headless/models/sale_order.py grove_headless/tests/test_effective_shipping_tier.py
git commit -m "fix(grove_headless): resolve shipping tier per-variant — bareroot Format variants no longer quote potted rates"
```

---

### Task 3: Pure filter-domain builder + list endpoint filters/fields

**Files:**
- Create: `grove_headless/controllers/product_domain.py` (pure Python — no odoo imports)
- Modify: `grove_headless/controllers/main.py` (`product_list`, lines 132–186; `PRODUCT_LIST_FIELDS` line 52)
- Test: `grove_headless/tests/test_product_domain.py` (create; plain pytest, repo pure-module pattern)

**Interfaces:**
- Consumes: Task 1 zone fields (for the `zone` filter).
- Produces: `build_product_domain(kwargs: dict, company_id: int) -> list` returning an Odoo search domain; list responses gain `tags: [{id,name}]`, `variant_count: int`, `price_min: float`; new optional query params `tag_id` (int) and `zone` (int). Consumed by grove-sites odoo-client (Plan 3).

- [ ] **Step 1: Write the failing test**

```python
# grove_headless/tests/test_product_domain.py
"""Pure tests for the product_list filter-domain builder (no Odoo runtime)."""

import importlib.util
import pathlib

_path = pathlib.Path(__file__).parent.parent / "controllers" / "product_domain.py"
_spec = importlib.util.spec_from_file_location("product_domain", _path)
product_domain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(product_domain)

BASE = [
    ("website_published", "=", True),
    ("sale_ok", "=", True),
    ("company_id", "in", [7, False]),
]


def test_base_domain_no_filters():
    assert product_domain.build_product_domain({}, 7) == BASE


def test_tag_filter():
    dom = product_domain.build_product_domain({"tag_id": "3"}, 7)
    assert ("product_tag_ids", "in", [3]) in dom


def test_zone_filter_brackets_range():
    dom = product_domain.build_product_domain({"zone": "6"}, 7)
    assert ("grove_zone_min", "<=", 6) in dom
    assert ("grove_zone_max", ">=", 6) in dom


def test_bad_values_ignored():
    assert product_domain.build_product_domain({"tag_id": "x", "zone": "?"}, 7) == BASE


def test_existing_filters_still_work():
    dom = product_domain.build_product_domain({"featured": "1", "category_id": "12", "slug": " Pear "}, 7)
    assert ("grove_featured", "=", True) in dom
    assert ("public_categ_ids", "in", [12]) in dom
    assert ("grove_slug", "=", "pear") in dom
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest grove_headless/tests/test_product_domain.py -v`
Expected: FAIL — `FileNotFoundError` (module doesn't exist).

- [ ] **Step 3: Write minimal implementation**

```python
# grove_headless/controllers/product_domain.py
"""Pure filter-domain builder for the public product_list endpoint.

No Odoo imports on purpose: this module tests standalone under pytest
(repo convention) and is consumed by controllers/main.py at runtime.
"""


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_product_domain(kwargs: dict, company_id: int) -> list:
    domain = [
        ("website_published", "=", True),
        ("sale_ok", "=", True),
        ("company_id", "in", [company_id, False]),
    ]
    if kwargs.get("featured"):
        domain.append(("grove_featured", "=", True))
    category_id = _to_int(kwargs.get("category_id"))
    if category_id is not None:
        domain.append(("public_categ_ids", "in", [category_id]))
    slug = str(kwargs.get("slug") or "").strip().lower()
    if slug:
        domain.append(("grove_slug", "=", slug))
    tag_id = _to_int(kwargs.get("tag_id"))
    if tag_id is not None:
        domain.append(("product_tag_ids", "in", [tag_id]))
    zone = _to_int(kwargs.get("zone"))
    if zone is not None:
        domain.append(("grove_zone_min", "<=", zone))
        domain.append(("grove_zone_max", ">=", zone))
    return domain
```

- [ ] **Step 4: Run pure tests to verify pass**

Run: `python3 -m pytest grove_headless/tests/test_product_domain.py -v`
Expected: 5 passed.

- [ ] **Step 5: Wire the controller**

In `grove_headless/controllers/main.py`: add import `from .product_domain import build_product_domain`. In `product_list` (line 132), replace the inline `domain = [...]` block and the `featured`/`category_id`/`slug` if-blocks (lines 136–155) with:

```python
        domain = build_product_domain(kwargs, current_company.id)
```

Then, in the items loop (after `data["slug"] = ...`), add the new list fields:

```python
                data["tags"] = [{"id": t.id, "name": t.name} for t in product.product_tag_ids]
                data["variant_count"] = len(product.product_variant_ids)
                data["price_min"] = min(product.product_variant_ids.mapped("lst_price"), default=product.list_price)
```

(`PRODUCT_LIST_FIELDS` is unchanged — tags/variants are serialized from the recordset, not `read()`.)

- [ ] **Step 6: Run lint + full Odoo test suite**

Run: `ruff check . --select E,F,I --line-length 120 && ruff format --check . --line-length 120` → clean.
Odoo-runner command → `0 failed`.

- [ ] **Step 7: Commit**

```bash
git add grove_headless/controllers/product_domain.py grove_headless/controllers/main.py grove_headless/tests/test_product_domain.py
git commit -m "feat(grove_headless): tag_id + zone list filters (pure domain builder), tags/variant_count/price_min on list cards"
```

---

### Task 4: Detail endpoint — facts block + structured variants

**Files:**
- Modify: `grove_headless/controllers/main.py` (`product_detail`, lines 187–232)
- Test: `grove_headless/tests/test_detail_serialization.py` (create; TransactionCase)

**Interfaces:**
- Consumes: Task 1 fields, Task 2 `grove_effective_shipping_tier`, attribute-name contracts (`Cultivar`, `Format`).
- Produces: detail JSON gains `facts` object `{botanical_name, zone_min, zone_max, layer, sun, mature_size, spacing, soil}` (empty-string/None when unset), `tags` (as in list), and each entry of `variants[]` gains `sku` (default_code), `cultivar` (Cultivar axis value name or ""), `format` ("Potted"/"Bareroot"/""), `price` (lst_price), `qty_available` (float), `shipping_tier`. Existing variant keys (`id`, `display_name`, `image_url`) are preserved. Consumed by Plan 3 normalizers.

- [ ] **Step 1: Write the failing test**

```python
# grove_headless/tests/test_detail_serialization.py
"""Detail serializer: facts block + structured variants (catalog spec)."""

from odoo.tests import TransactionCase, tagged

from odoo.addons.grove_headless.controllers.main import _serialize_facts, _structure_variant


@tagged("post_install", "-at_install")
class TestDetailSerialization(TransactionCase):
    def setUp(self):
        super().setUp()
        self.cultivar = self.env["product.attribute"].create({"name": "Cultivar", "create_variant": "always"})
        self.fmt = self.env["product.attribute"].create({"name": "Format", "create_variant": "always"})
        self.c_mag = self.env["product.attribute.value"].create({"name": "Magness", "attribute_id": self.cultivar.id})
        self.f_pt = self.env["product.attribute.value"].create({"name": "Potted", "attribute_id": self.fmt.id})
        self.f_br = self.env["product.attribute.value"].create({"name": "Bareroot", "attribute_id": self.fmt.id})
        self.tmpl = self.env["product.template"].create(
            {
                "name": "Pear",
                "type": "consu",
                "grove_botanical_name": "Pyrus communis",
                "grove_zone_min": 5,
                "grove_zone_max": 8,
                "attribute_line_ids": [
                    (0, 0, {"attribute_id": self.cultivar.id, "value_ids": [(6, 0, [self.c_mag.id])]}),
                    (0, 0, {"attribute_id": self.fmt.id, "value_ids": [(6, 0, [self.f_pt.id, self.f_br.id])]}),
                ],
            }
        )

    def test_facts_block(self):
        facts = _serialize_facts(self.tmpl)
        self.assertEqual(facts["botanical_name"], "Pyrus communis")
        self.assertEqual(facts["zone_min"], 5)
        self.assertEqual(facts["layer"], "")

    def test_structured_variant(self):
        bareroot = self.tmpl.product_variant_ids.filtered(
            lambda v: "Bareroot" in v.product_template_variant_value_ids.mapped("name")
        )
        data = _structure_variant(bareroot)
        self.assertEqual(data["cultivar"], "Magness")
        self.assertEqual(data["format"], "Bareroot")
        self.assertEqual(data["shipping_tier"], "bareroot")
        self.assertIn("price", data)
        self.assertIn("qty_available", data)
```

- [ ] **Step 2: Run test to verify it fails**

Odoo-runner command. Expected: FAIL — `ImportError: cannot import name '_serialize_facts'`.

- [ ] **Step 3: Write minimal implementation**

Add to `grove_headless/controllers/main.py`, next to `_serialize_product` (~line 91):

```python
def _serialize_facts(product):
    """Growing-facts block for the detail endpoint (catalog spec 2026-07-13)."""
    return {
        "botanical_name": product.grove_botanical_name or "",
        "zone_min": product.grove_zone_min or None,
        "zone_max": product.grove_zone_max or None,
        "layer": product.grove_layer or "",
        "sun": product.grove_sun or "",
        "mature_size": product.grove_mature_size or "",
        "spacing": product.grove_spacing or "",
        "soil": product.grove_soil or "",
    }


def _structure_variant(variant):
    """Structured variant entry: axes parsed into fields, not display-name strings."""
    axis = {v.attribute_id.name: v.name for v in variant.product_template_variant_value_ids}
    return {
        "id": variant.id,
        "display_name": variant.display_name,
        "sku": variant.default_code or "",
        "cultivar": axis.get("Cultivar", ""),
        "format": axis.get("Format", ""),
        "price": variant.lst_price,
        "qty_available": variant.qty_available,
        "shipping_tier": variant.grove_effective_shipping_tier,
        "image_url": f"/web/image/product.product/{variant.id}/image_128",
    }
```

In `product_detail` (lines ~219–227), replace the existing variants loop with:

```python
        data["variants"] = [_structure_variant(v) for v in product.product_variant_ids]
        data["facts"] = _serialize_facts(product)
        data["tags"] = [{"id": t.id, "name": t.name} for t in product.product_tag_ids]
```

- [ ] **Step 4: Run tests + lint**

Odoo-runner command → `0 failed`. Ruff commands → clean.

- [ ] **Step 5: Commit**

```bash
git add grove_headless/controllers/main.py grove_headless/tests/test_detail_serialization.py
git commit -m "feat(grove_headless): facts block + structured variants (sku/cultivar/format/price/qty/tier) on product detail"
```

---

### Task 5: images[] on product detail

**Files:**
- Modify: `grove_headless/controllers/main.py` (`product_detail`)
- Test: extend `grove_headless/tests/test_detail_serialization.py`

**Interfaces:**
- Consumes: website_sale's `product.image` model (`product_template_image_ids`).
- Produces: detail JSON `images: [{id, url, thumb_url}]` — hero first (`image_1024`/`image_256` of the template), then eCommerce media. Empty list when no image. Consumed by Plan 3 gallery.

- [ ] **Step 1: Write the failing test (append to TestDetailSerialization)**

```python
def test_images_hero_first_and_empty_ok(self):
    from odoo.addons.grove_headless.controllers.main import _serialize_images

    self.assertEqual(_serialize_images(self.tmpl), [])  # no image set
    self.tmpl.image_1920 = (
        b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    images = _serialize_images(self.tmpl)
    self.assertEqual(images[0]["url"], f"/web/image/product.template/{self.tmpl.id}/image_1024")
    self.assertEqual(images[0]["thumb_url"], f"/web/image/product.template/{self.tmpl.id}/image_256")
```

- [ ] **Step 2: Run to verify it fails**

Odoo-runner command. Expected: FAIL — `_serialize_images` not defined.

- [ ] **Step 3: Implement**

Add next to `_serialize_facts` in `controllers/main.py`:

```python
def _serialize_images(product):
    """Gallery list: template hero first, then eCommerce media images."""
    images = []
    if product.image_1920:
        images.append(
            {
                "id": 0,
                "url": f"/web/image/product.template/{product.id}/image_1024",
                "thumb_url": f"/web/image/product.template/{product.id}/image_256",
            }
        )
    for media in product.product_template_image_ids:
        images.append(
            {
                "id": media.id,
                "url": f"/web/image/product.image/{media.id}/image_1024",
                "thumb_url": f"/web/image/product.image/{media.id}/image_256",
            }
        )
    return images
```

And in `product_detail`, after the `facts`/`tags` lines from Task 4:

```python
        data["images"] = _serialize_images(product)
```

- [ ] **Step 4: Run tests + lint** — Odoo runner `0 failed`, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add grove_headless/controllers/main.py grove_headless/tests/test_detail_serialization.py
git commit -m "feat(grove_headless): images[] gallery (hero + eCommerce media) on product detail"
```

---

### Task 6: ZIP→zone endpoint

**Files:**
- Modify: `grove_headless/controllers/main.py` (new route after `product_detail`)
- Test: `grove_headless/tests/test_zone_endpoint.py` (create; pure pytest on the response builder)

**Interfaces:**
- Consumes: `usda_zone_for_zip(zip_code) -> int | None` from `grove_headless/models/shipping_calendar.py` (existing, backed by `data/zip_usda_zone.csv`).
- Produces: `GET /grove/api/v1/zone?zip=25301` → `200 {"zip": "25301", "zone": 6}` or `404 {"error": "unknown zip"}`. Pure helper `zone_response(zip_raw, zone) -> tuple[dict, int]` in `product_domain.py`. Consumed by Plan 3's "Will this grow for me?" widget.

- [ ] **Step 1: Write the failing test**

```python
# grove_headless/tests/test_zone_endpoint.py
"""Pure tests for the /zone response builder."""

import importlib.util
import pathlib

_path = pathlib.Path(__file__).parent.parent / "controllers" / "product_domain.py"
_spec = importlib.util.spec_from_file_location("product_domain", _path)
product_domain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(product_domain)


def test_known_zone():
    body, status = product_domain.zone_response("25301-1234", 6)
    assert status == 200
    assert body == {"zip": "25301", "zone": 6}


def test_unknown_zone_404():
    body, status = product_domain.zone_response("00000", None)
    assert status == 404
    assert body == {"error": "unknown zip"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest grove_headless/tests/test_zone_endpoint.py -v`
Expected: FAIL — `zone_response` not defined.

- [ ] **Step 3: Implement**

Append to `grove_headless/controllers/product_domain.py`:

```python
def zone_response(zip_raw, zone):
    """(body, status) for the ZIP->USDA-zone lookup endpoint."""
    if zone is None:
        return {"error": "unknown zip"}, 404
    return {"zip": str(zip_raw or "")[:5], "zone": zone}, 200
```

Add the route in `controllers/main.py` (imports: `from .product_domain import build_product_domain, zone_response` and `from ..models.shipping_calendar import usda_zone_for_zip`), after `product_detail`:

```python
    @http.route(
        "/grove/api/v1/zone",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def zone_lookup(self, **kwargs):
        """USDA zone for a ZIP — powers the 'Will this grow for me?' widget."""
        zip_raw = str(kwargs.get("zip", ""))
        body, status = zone_response(zip_raw, usda_zone_for_zip(zip_raw))
        return _json_response(body, status=status)
```

- [ ] **Step 4: Run pure tests + full Odoo suite + lint** — all green.

- [ ] **Step 5: Commit**

```bash
git add grove_headless/controllers/product_domain.py grove_headless/controllers/main.py grove_headless/tests/test_zone_endpoint.py
git commit -m "feat(grove_headless): GET /grove/api/v1/zone ZIP->USDA-zone endpoint"
```

---

### Task 7: Version bump, changelog docstring, PR

**Files:**
- Modify: `grove_headless/__manifest__.py` (version `19.0.1.5.0` → `19.0.1.6.0`)

**Interfaces:**
- Produces: the deployable module version QA's git-sync will pick up; the PR that Plan 3 (odoo-client) targets as its API contract.

- [ ] **Step 1: Bump version**

In `grove_headless/__manifest__.py` change `"version": "19.0.1.5.0"` to `"version": "19.0.1.6.0"`.

- [ ] **Step 2: Full verification**

Run: `ruff check . --select E,F,I --line-length 120 && ruff format --check . --line-length 120`
Run: `python3 -m pytest grove_headless/tests/test_product_domain.py grove_headless/tests/test_zone_endpoint.py -v` → all pass.
Odoo-runner command (Global Constraints) → `0 failed` including all pre-existing suites.

- [ ] **Step 3: Manual smoke against local stack**

```bash
curl -s "http://localhost:8069/grove/api/v1/zone?zip=25301" | python3 -m json.tool          # {"zip":"25301","zone":6}
curl -s -H "X-Grove-Tenant: nursery" "http://localhost:8069/grove/api/v1/products?zone=6&limit=5" | python3 -m json.tool
```

Expected: zone lookup returns 6; product list responds 200 with `tags`, `variant_count`, `price_min` keys on each item.

- [ ] **Step 4: Commit + push + PR**

```bash
git add grove_headless/__manifest__.py
git commit -m "chore(grove_headless): bump to 19.0.1.6.0 (catalog API v1)"
git push -u origin feat/catalog-api-v1
gh pr create --draft --title "feat(grove_headless): catalog API v1 — facts, variant shipping tier, tags, structured variants, images, zone lookup" \
  --body "Implements Plan 1 of the nursery product-pages spec (grove-sites#123). QA deploys via git-sync on merge; run module upgrade (-u grove_headless) on the QA droplet after merge."
```

Note for the operator: after merge, git-sync delivers the code within ~60 s, but **new fields require a module upgrade**: `docker exec grove-odoo-1 odoo -d odoo -u grove_headless --stop-after-init` on the QA droplet (then restart the odoo container).

---

## Self-Review (done at write time)

- **Spec coverage:** facts fields ✓(T1) · shipping-tier fix ✓(T2) · tags list+detail ✓(T3/T4) · `tag_id`/`zone` filters ✓(T3) · `variant_count`/`price_min` ✓(T3) · structured variants ✓(T4) · `images[]` ✓(T5) · ZIP→zone ✓(T6). Out of scope here (later plans): `/facets` endpoint (v3), seed rework (Plan 2), frontend (Plan 3), guides (Plan 4).
- **Placeholders:** none — every step carries code/commands.
- **Type consistency:** `build_product_domain(kwargs, company_id)` used in T3 step 5 as defined; `_structure_variant`/`_serialize_facts`/`_serialize_images` imported in tests exactly as defined; `grove_effective_shipping_tier` name identical across T2/T4.
