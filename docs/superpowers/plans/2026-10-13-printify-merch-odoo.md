# Printify T-shirts: Odoo backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Odoo lists Printify T-shirts on the farm tenant, checks them out with a live Printify shipping quote and Stripe Tax, pushes paid orders to Printify production, and folds Printify shipment webhooks into the existing shipment email path.

**Architecture:** Two new pure modules in `grove_headless/models/` (`printify_client.py` for HTTP, `printify_sync.py` for payload mapping) carry all Printify knowledge and are unit-tested without Odoo. ORM fields, a sync action, a push cron and a webhook route wire them into the existing headless checkout. A product-level `grove_fulfillment_route = "printify"` plus an order-level stored `grove_is_printify` gate every branch; every non-Printify path is unchanged.

**Tech Stack:** Odoo 19, Python 3, `requests` (already a dependency), Odoo `TransactionCase` / `HttpCase`, pytest for pure modules.

**Spec:** `docs/superpowers/specs/2026-10-13-printify-merch-design.md` (gom#340). The storefront half is a separate plan in grove-sites: `docs/superpowers/plans/2026-10-13-printify-merch-storefront.md`.

## Global Constraints

- Every existing (non-Printify) product and order behaves byte-for-byte as before. `grove_fulfillment_route` defaults to `in_house`.
- No new Python dependency. Outbound HTTP via `requests` with injectable `get=`/`post=` like `shippo_client.py`.
- Pure modules (`printify_client.py`, `printify_sync.py`) import no `odoo` and use no relative imports (they are loaded by path in tests).
- Config is env vars only: `PRINTIFY_API_TOKEN`, `PRINTIFY_SHOP_ID`, `PRINTIFY_WEBHOOK_SECRET`, `PRINTIFY_DRY_RUN`, plus existing `GROVE_STRIPE_TAX_GOLDBERRY`. Missing token/shop raises a clear error.
- Printify API base `https://api.printify.com/v1`; developers.printify.com is the source of truth. Limits: 600 req/min, 5% error-rate cap. No tight retry loops.
- Shipping quote timeout 5 s; any failure is a 503 `"Shipping is temporarily unavailable. Try again in a few minutes."` Never a fallback price.
- Mixed carts: 409 `"Shirts check out separately from trees."`
- Destinations: 50 states + DC only. Otherwise 400 `"Shirts ship to the 50 US states and DC."`
- Apparel Stripe tax code `txcd_30011000`.
- Push: `external_id = sale_order.name`, `shipping_method = 1`, `send_shipping_notification = False`. Backoff minutes (0, 5, 30, 120), cap 3 attempts, then `push_failed` + Discord alert.
- Shipment webhook uses status `"transit"` (never `"shipped"`) and writes `grove_tracking_numbers` / `grove_shipping_carriers` first.
- Customer copy: no em dashes.
- Manifest version bump `19.0.1.74.0` → `19.0.1.75.0` (or the next free number if main moved).
- Every new Odoo-runtime test module: add to `grove_headless/tests/__init__.py` (alphabetical) AND to `collect_ignore_glob` in `conftest.py`. Pure test modules: `__init__.py` only.
- Commands: pure tests `python3 -m pytest grove_headless/tests/test_printify_client.py -v`; Odoo tests via the CI docker command with `--test-tags='/grove_headless'` (see `.github/workflows/ci.yml:284-298`); lint `ruff check . --select E,F,I --line-length 120 && ruff format --check`.

## Review Focus

1. **Paid shirt order auto-refunded as "oversold".** `_oversold_lines` (`controllers/main.py:3934`) reads `free_qty`, which is 0 for a print-on-demand product. Expect: Printify lines are skipped. Test in Task 6.
2. **Shirt order swept into the Pirate Ship label batch or the UPS/USPS poll.** A paid order sits at `awaiting_label`. Expect: excluded from `label_batch._eligible_orders` and `_poll_carrier_tracking`. Tests in Task 8.
3. **Territory or non-US address** (PR, GU, VI, AS, MP pass `canonical_state_code`). Expect: 400 with the destinations message, no Printify call. Test in Task 4.
4. **Webhook delivered twice, or out of order** (delivered before shipment:created). Expect: no duplicate emails, status never regresses. `_apply_delivery_status` already dedupes and ranks; tests in Task 7 pin it.
5. **Stripe retries `checkout.session.completed`** after a push already created the Printify order. Expect: one Printify order, found by `external_id`. Test in Task 6.

---

### Task 1: Pure Printify client

**Files:**
- Create: `grove_headless/models/printify_client.py`
- Modify: `grove_headless/models/__init__.py` (add `printify_client,  # noqa: F401  # pure HTTP client, no ORM models` alphabetically)
- Test: `grove_headless/tests/test_printify_client.py` (pure; add to `tests/__init__.py` only)

**Interfaces:**
- Produces:
  - `BASE_URL: str`, `QUOTE_TIMEOUT = 5`, `DEFAULT_TIMEOUT = 15`
  - `US_50_AND_DC: frozenset[str]`
  - `class PrintifyError(Exception)` with `.status: int | None`
  - `list_products(token, shop_id, get=requests.get) -> list[dict]`
  - `calculate_shipping(token, shop_id, line_items, address, post=requests.post) -> float` (dollars)
  - `create_order(token, shop_id, payload, post=requests.post) -> str` (Printify order id)
  - `get_order(token, shop_id, order_id, get=requests.get) -> dict`
  - `find_order_by_external_id(token, shop_id, external_id, get=requests.get, pages=3) -> dict | None`
  - `send_to_production(token, shop_id, order_id, post=requests.post) -> None`
  - `create_webhook(token, shop_id, topic, url, secret, post=requests.post) -> dict`
  - `verify_signature(secret, body: bytes, header: str) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
"""Pure tests for the Printify client (no Odoo). Spec: 2026-10-13-printify-merch-design.md."""

import hashlib
import hmac
import importlib.util
import os
import unittest
from unittest import mock

import requests

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "printify_client.py")
_spec = importlib.util.spec_from_file_location("grove_printify_client", _MODULE_PATH)
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)


def _resp(status, body):
    return mock.Mock(status_code=status, json=lambda: body, text=str(body))


class TestPrintifyClient(unittest.TestCase):
    def test_list_products_follows_pages(self):
        get = mock.Mock(
            side_effect=[
                _resp(200, {"current_page": 1, "last_page": 2, "data": [{"id": "a"}]}),
                _resp(200, {"current_page": 2, "last_page": 2, "data": [{"id": "b"}]}),
            ]
        )
        self.assertEqual([p["id"] for p in pc.list_products("tok", "42", get=get)], ["a", "b"])
        url = get.call_args_list[0].args[0]
        self.assertEqual(url, "https://api.printify.com/v1/shops/42/products.json")
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer tok")

    def test_calculate_shipping_returns_standard_in_dollars(self):
        post = mock.Mock(return_value=_resp(200, {"standard": 475, "express": 1200}))
        amount = pc.calculate_shipping("tok", "42", [{"product_id": "p", "variant_id": 1, "quantity": 1}], {"zip": "25526"}, post=post)
        self.assertEqual(amount, 4.75)
        self.assertEqual(post.call_args.kwargs["timeout"], 5)
        self.assertEqual(post.call_args.args[0], "https://api.printify.com/v1/shops/42/orders/shipping.json")

    def test_calculate_shipping_timeout_raises_printify_error(self):
        post = mock.Mock(side_effect=requests.Timeout("slow"))
        with self.assertRaises(pc.PrintifyError):
            pc.calculate_shipping("tok", "42", [], {}, post=post)

    def test_http_error_raises_with_status(self):
        post = mock.Mock(return_value=_resp(503, {"error": "down"}))
        with self.assertRaises(pc.PrintifyError) as ctx:
            pc.calculate_shipping("tok", "42", [], {}, post=post)
        self.assertEqual(ctx.exception.status, 503)

    def test_missing_standard_rate_raises(self):
        post = mock.Mock(return_value=_resp(200, {"express": 900}))
        with self.assertRaises(pc.PrintifyError):
            pc.calculate_shipping("tok", "42", [], {}, post=post)

    def test_create_order_returns_id(self):
        post = mock.Mock(return_value=_resp(200, {"id": "po_1"}))
        self.assertEqual(pc.create_order("tok", "42", {"external_id": "S1"}, post=post), "po_1")

    def test_find_order_by_external_id(self):
        get = mock.Mock(
            side_effect=[
                _resp(200, {"last_page": 2, "data": [{"id": "x", "external_id": "S9"}]}),
                _resp(200, {"last_page": 2, "data": [{"id": "po_1", "external_id": "S1"}]}),
            ]
        )
        self.assertEqual(pc.find_order_by_external_id("tok", "42", "S1", get=get)["id"], "po_1")

    def test_find_order_by_external_id_none(self):
        get = mock.Mock(return_value=_resp(200, {"last_page": 1, "data": []}))
        self.assertIsNone(pc.find_order_by_external_id("tok", "42", "S1", get=get))

    def test_send_to_production_posts(self):
        post = mock.Mock(return_value=_resp(200, {"id": "po_1"}))
        pc.send_to_production("tok", "42", "po_1", post=post)
        self.assertEqual(post.call_args.args[0], "https://api.printify.com/v1/shops/42/orders/po_1/send_to_production.json")

    def test_verify_signature(self):
        body = b'{"type":"order:updated"}'
        digest = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        self.assertTrue(pc.verify_signature("s3cret", body, f"sha256={digest}"))
        self.assertTrue(pc.verify_signature("s3cret", body, digest))
        self.assertFalse(pc.verify_signature("s3cret", body, "sha256=bad"))
        self.assertFalse(pc.verify_signature("", body, f"sha256={digest}"))
        self.assertFalse(pc.verify_signature("s3cret", body, ""))

    def test_destinations_are_50_states_and_dc(self):
        self.assertEqual(len(pc.US_50_AND_DC), 51)
        self.assertIn("DC", pc.US_50_AND_DC)
        for territory in ("PR", "GU", "VI", "AS", "MP"):
            self.assertNotIn(territory, pc.US_50_AND_DC)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest grove_headless/tests/test_printify_client.py -v`
Expected: FAIL (`printify_client.py` does not exist).

- [ ] **Step 3: Implement**

```python
"""Printify REST client for print-on-demand merch (spec 2026-10-13-printify-merch-design.md).

Pure: no Odoo imports, no relative imports (tests load it by path). Every call takes
an injectable ``get``/``post`` like ``shippo_client``. developers.printify.com is the
source of truth for the API; limits are 600 req/min with a 5% error-rate cap, so
callers never retry in a tight loop.
"""

import hashlib
import hmac

import requests

BASE_URL = "https://api.printify.com/v1"
DEFAULT_TIMEOUT = 15
QUOTE_TIMEOUT = 5

# Shirts ship to the 50 states + DC only. ``canonical_state_code`` also accepts
# PR/VI/GU/AS/MP, so checkout checks this explicit set.
US_50_AND_DC = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)


class PrintifyError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "grove-headless"}


def _call(fn, url, token, timeout, **kwargs):
    try:
        resp = fn(url, headers=_headers(token), timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise PrintifyError(f"Printify request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise PrintifyError(f"Printify {resp.status_code}: {str(resp.text)[:300]}", resp.status_code)
    return resp.json()


def list_products(token, shop_id, get=requests.get):
    products, page = [], 1
    while True:
        body = _call(get, f"{BASE_URL}/shops/{shop_id}/products.json", token, DEFAULT_TIMEOUT,
                     params={"page": page, "limit": 50})
        products.extend(body.get("data") or [])
        if page >= int(body.get("last_page") or 1):
            return products
        page += 1


def calculate_shipping(token, shop_id, line_items, address, post=requests.post):
    body = _call(post, f"{BASE_URL}/shops/{shop_id}/orders/shipping.json", token, QUOTE_TIMEOUT,
                 json={"line_items": line_items, "address_to": address})
    cents = body.get("standard")
    if cents is None:
        raise PrintifyError("Printify shipping quote had no standard rate")
    return round(int(cents) / 100.0, 2)


def create_order(token, shop_id, payload, post=requests.post):
    return str(_call(post, f"{BASE_URL}/shops/{shop_id}/orders.json", token, DEFAULT_TIMEOUT, json=payload)["id"])


def get_order(token, shop_id, order_id, get=requests.get):
    return _call(get, f"{BASE_URL}/shops/{shop_id}/orders/{order_id}.json", token, DEFAULT_TIMEOUT)


def find_order_by_external_id(token, shop_id, external_id, get=requests.get, pages=3):
    """Printify's order list has no external_id filter; scan the newest pages.
    A retry happens within hours of the first push, so 3 pages of 10 is plenty."""
    for page in range(1, pages + 1):
        body = _call(get, f"{BASE_URL}/shops/{shop_id}/orders.json", token, DEFAULT_TIMEOUT,
                     params={"page": page, "limit": 10})
        for order in body.get("data") or []:
            if order.get("external_id") == external_id:
                return order
        if page >= int(body.get("last_page") or 1):
            return None
    return None


def send_to_production(token, shop_id, order_id, post=requests.post):
    _call(post, f"{BASE_URL}/shops/{shop_id}/orders/{order_id}/send_to_production.json", token, DEFAULT_TIMEOUT)


def create_webhook(token, shop_id, topic, url, secret, post=requests.post):
    return _call(post, f"{BASE_URL}/shops/{shop_id}/webhooks.json", token, DEFAULT_TIMEOUT,
                 json={"topic": topic, "url": url, "secret": secret})


def verify_signature(secret, body, header):
    """HMAC-SHA256 of the raw body. Accepts ``sha256=<hex>`` or bare hex; fails closed.
    Confirm the header name (``X-Pfy-Signature``) against developers.printify.com in Task 7."""
    if not secret or not header:
        return False
    provided = header.split("=", 1)[1] if header.startswith("sha256=") else header
    expected = hmac.new(secret.encode(), body or b"", hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided.strip(), expected)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m pytest grove_headless/tests/test_printify_client.py -v`
Expected: 11 passed.

- [ ] **Step 5: Lint and commit**

```bash
ruff check grove_headless --select E,F,I --line-length 120 && ruff format grove_headless/models/printify_client.py grove_headless/tests/test_printify_client.py
git add grove_headless/models/printify_client.py grove_headless/models/__init__.py grove_headless/tests/test_printify_client.py grove_headless/tests/__init__.py
git commit -m "feat(grove_headless): pure Printify REST client (printify merch spec)"
```

---

### Task 2: Pure product mapping

**Files:**
- Create: `grove_headless/models/printify_sync.py`
- Modify: `grove_headless/models/__init__.py` (add `printify_sync,  # noqa: F401  # pure mapping, no ORM models`)
- Test: `grove_headless/tests/test_printify_sync_map.py` (pure)

**Interfaces:**
- Produces: `map_product(raw: dict) -> dict` with keys `printify_product_id: str`, `name`, `description`, `image_url: str | None`, `variants: list[{printify_variant_id: int, color: str, size: str, price: float, cost: float, enabled: bool}]`.

- [ ] **Step 1: Write the failing test**

```python
import importlib.util
import os
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "printify_sync.py")
_spec = importlib.util.spec_from_file_location("grove_printify_sync", _MODULE_PATH)
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)

RAW = {
    "id": "6650abc",
    "title": "Grove Tee",
    "description": "<p>Soft tee.</p>",
    "images": [{"src": "https://img/1.png", "is_default": False}, {"src": "https://img/2.png", "is_default": True}],
    "options": [
        {"name": "Colors", "type": "color", "values": [{"id": 521, "title": "Natural"}, {"id": 522, "title": "Sage"}]},
        {"name": "Sizes", "type": "size", "values": [{"id": 14, "title": "M"}, {"id": 18, "title": "2XL"}]},
    ],
    "variants": [
        {"id": 11001, "price": 2800, "cost": 1250, "is_enabled": True, "options": [521, 14]},
        {"id": 11002, "price": 3100, "cost": 1500, "is_enabled": True, "options": [521, 18]},
        {"id": 11003, "price": 2800, "cost": 1250, "is_enabled": False, "options": [522, 14]},
    ],
}


class TestMapProduct(unittest.TestCase):
    def test_maps_variants_to_color_and_size(self):
        m = ps.map_product(RAW)
        self.assertEqual(m["printify_product_id"], "6650abc")
        self.assertEqual(m["name"], "Grove Tee")
        self.assertEqual(m["image_url"], "https://img/2.png")
        first = m["variants"][0]
        self.assertEqual(first, {"printify_variant_id": 11001, "color": "Natural", "size": "M",
                                 "price": 28.0, "cost": 12.5, "enabled": True})
        self.assertFalse(m["variants"][2]["enabled"])

    def test_first_image_when_no_default(self):
        raw = dict(RAW, images=[{"src": "https://img/only.png"}])
        self.assertEqual(ps.map_product(raw)["image_url"], "https://img/only.png")

    def test_no_images(self):
        self.assertIsNone(ps.map_product(dict(RAW, images=[]))["image_url"])
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest grove_headless/tests/test_printify_sync_map.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
"""Map a Printify product payload to the shape the Odoo sync writes (pure, no Odoo).
Printify prices are integer cents; options carry a ``type`` of ``color`` / ``size``."""

_AXIS_BY_TYPE = {"color": "Color", "size": "Size"}


def map_product(raw):
    value_axis = {}
    for option in raw.get("options") or []:
        axis = _AXIS_BY_TYPE.get((option.get("type") or "").lower())
        if not axis:
            continue
        for value in option.get("values") or []:
            value_axis[value["id"]] = (axis, value["title"])

    variants = []
    for variant in raw.get("variants") or []:
        axes = dict(value_axis[i] for i in variant.get("options") or [] if i in value_axis)
        variants.append({
            "printify_variant_id": int(variant["id"]),
            "color": axes.get("Color", ""),
            "size": axes.get("Size", ""),
            "price": round((variant.get("price") or 0) / 100.0, 2),
            "cost": round((variant.get("cost") or 0) / 100.0, 2),
            "enabled": bool(variant.get("is_enabled")),
        })

    images = raw.get("images") or []
    default = next((img["src"] for img in images if img.get("is_default")), None)
    return {
        "printify_product_id": str(raw["id"]),
        "name": raw.get("title") or "",
        "description": raw.get("description") or "",
        "image_url": default or (images[0]["src"] if images else None),
        "variants": variants,
    }
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python3 -m pytest grove_headless/tests/test_printify_sync_map.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add grove_headless/models/printify_sync.py grove_headless/models/__init__.py grove_headless/tests/test_printify_sync_map.py grove_headless/tests/__init__.py
git commit -m "feat(grove_headless): pure Printify product mapping"
```

---

### Task 3: Fields and "Sync from Printify"

**Files:**
- Modify: `grove_headless/models/product_template.py` (fields after `grove_gate_exempt`, ~line 642; sync method)
- Modify: `grove_headless/models/product_product.py` (field)
- Modify: `grove_headless/models/sale_order.py` (fields + compute)
- Create: `grove_headless/data/printify_actions.xml` (server action "Sync from Printify")
- Modify: `grove_headless/__manifest__.py` (append `"data/printify_actions.xml"` after line 81; bump version)
- Test: `grove_headless/tests/test_printify_product_sync.py` (Odoo runtime: `__init__.py` + `conftest.py` ignore)

**Interfaces:**
- Consumes: `printify_client.list_products`, `printify_sync.map_product`.
- Produces:
  - `product.template.grove_fulfillment_route` Selection `[("in_house","In-house"),("printify","Printify")]`, default `in_house`
  - `product.template.printify_product_id` Char (index), `printify_lead_time` Char default `"3 to 7 business days"`
  - `product.product.printify_variant_id` Integer (index)
  - `sale.order.grove_is_printify` Boolean computed stored from `order_line.product_id.grove_fulfillment_route`
  - `sale.order.printify_order_id` Char, `printify_state` Selection `queued/created/in_production/shipped/delivered/canceled/on_hold/push_failed`, `printify_push_attempts` Integer, `printify_push_last_attempt` Datetime (all `readonly=True, copy=False`)
  - `product.template._grove_printify_sync(fetch=None) -> dict(created:int, updated:int, archived:int)` (`@api.model`)

Spec deviation, deliberate: `printify_lead_time` is an editable Char with a default rather than read from the provider, because the Printify product payload carries no production time. Josh edits it per shirt.

- [ ] **Step 1: Write the failing tests**

```python
import base64
import os
from unittest import mock

from odoo.addons.grove_headless.models import printify_client
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin

RAW = {
    "id": "6650abc",
    "title": "Grove Tee",
    "description": "<p>Soft tee.</p>",
    "images": [{"src": "https://img/2.png", "is_default": True}],
    "options": [
        {"type": "color", "values": [{"id": 521, "title": "Natural"}, {"id": 522, "title": "Sage"}]},
        {"type": "size", "values": [{"id": 14, "title": "M"}, {"id": 18, "title": "2XL"}]},
    ],
    "variants": [
        {"id": 11001, "price": 2800, "cost": 1250, "is_enabled": True, "options": [521, 14]},
        {"id": 11002, "price": 3100, "cost": 1500, "is_enabled": True, "options": [521, 18]},
        {"id": 11003, "price": 2800, "cost": 1250, "is_enabled": True, "options": [522, 14]},
        {"id": 11004, "price": 3100, "cost": 1500, "is_enabled": False, "options": [522, 18]},
    ],
}
ENV = {"PRINTIFY_API_TOKEN": "tok", "PRINTIFY_SHOP_ID": "42"}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@tagged("grove_headless", "post_install", "-at_install")
class TestPrintifySync(GroveTaxFixtureMixin, TransactionCase):
    def _sync(self, raws=None):
        fetch = mock.Mock(return_value=mock.Mock(status_code=200, content=PNG))
        with mock.patch.dict(os.environ, ENV, clear=False), mock.patch.object(
            printify_client, "list_products", return_value=raws if raws is not None else [RAW]
        ):
            return self.env["product.template"]._grove_printify_sync(fetch=fetch)

    def _tmpl(self):
        return self.env["product.template"].with_context(active_test=False).search([("printify_product_id", "=", "6650abc")])

    def test_first_sync_creates_unpublished_farm_template(self):
        result = self._sync()
        tmpl = self._tmpl()
        self.assertEqual(result["created"], 1)
        self.assertEqual(len(tmpl), 1)
        self.assertEqual(tmpl.grove_fulfillment_route, "printify")
        self.assertFalse(tmpl.website_published)
        self.assertTrue(tmpl.grove_gate_exempt)
        self.assertFalse(tmpl.is_storable)
        self.assertEqual(tmpl.company_id, self.env.ref("base.main_company"))
        self.assertEqual(tmpl.list_price, 28.0)
        self.assertTrue(tmpl.image_1920)

    def test_variants_mapped_and_disabled_archived(self):
        self._sync()
        variants = self._tmpl().with_context(active_test=False).product_variant_ids
        by_id = {v.printify_variant_id: v for v in variants if v.printify_variant_id}
        self.assertEqual(set(by_id), {11001, 11002, 11003})
        self.assertEqual(by_id[11002].lst_price, 31.0)
        self.assertEqual(by_id[11001].standard_price, 12.5)
        unmapped = variants.filtered(lambda v: not v.printify_variant_id)
        self.assertTrue(unmapped, "Sage/2XL exists as an Odoo combination")
        self.assertFalse(any(unmapped.mapped("active")), "a disabled Printify combination is archived")

    def test_second_sync_is_idempotent_and_keeps_odoo_price(self):
        self._sync()
        tmpl = self._tmpl()
        tmpl.list_price = 30.0
        count = self.env["product.product"].with_context(active_test=False).search_count([])
        result = self._sync()
        self.assertEqual(result["created"], 0)
        self.assertEqual(self.env["product.product"].with_context(active_test=False).search_count([]), count)
        self.assertEqual(self._tmpl().list_price, 30.0)

    def test_variant_disabled_later_is_archived_not_deleted(self):
        self._sync()
        raw = dict(RAW, variants=[dict(v, is_enabled=(v["id"] != 11003)) for v in RAW["variants"]])
        result = self._sync([raw])
        variant = self.env["product.product"].with_context(active_test=False).search([("printify_variant_id", "=", 11003)])
        self.assertEqual(len(variant), 1)
        self.assertFalse(variant.active)
        self.assertEqual(result["archived"], 1)

    def test_missing_config_raises(self):
        from odoo.exceptions import UserError

        with mock.patch.dict(os.environ, {"PRINTIFY_API_TOKEN": "", "PRINTIFY_SHOP_ID": ""}, clear=False):
            with self.assertRaises(UserError):
                self.env["product.template"]._grove_printify_sync()

    def test_order_is_printify_compute(self):
        self._sync()
        variant = self.env["product.product"].search([("printify_variant_id", "=", 11001)])
        order = self.env["sale.order"].create({
            "partner_id": self.env.ref("base.partner_admin").id,
            "order_line": [(0, 0, {"product_id": variant.id, "product_uom_qty": 1})],
        })
        self.assertTrue(order.grove_is_printify)
```

- [ ] **Step 2: Run to verify failure**

Run the CI Odoo test command with `--test-tags='/grove_headless:TestPrintifySync'`.
Expected: FAIL (fields and method missing).

- [ ] **Step 3: Implement the fields**

In `product_template.py` after `grove_gate_exempt` (~line 642):

```python
    # Printify print-on-demand merch (spec 2026-10-13-printify-merch-design.md).
    # ``printify`` products skip every plant rule at checkout and are fulfilled by
    # Printify; ``in_house`` (default) is every existing product, unchanged.
    grove_fulfillment_route = fields.Selection(
        [("in_house", "In-house"), ("printify", "Printify")],
        string="Fulfillment route",
        default="in_house",
        required=True,
        tracking=True,
        help="Printify products are printed and shipped by Printify and skip plant shipping rules.",
    )
    printify_product_id = fields.Char(string="Printify product ID", index=True, copy=False, readonly=True)
    printify_lead_time = fields.Char(
        string="Printify production time",
        default="3 to 7 business days",
        help="Shown on the product page and confirmation: 'Printed to order, ships in ...'.",
    )
```

In `product_product.py` (inside the class):

```python
    printify_variant_id = fields.Integer(string="Printify variant ID", index=True, copy=False, readonly=True)
```

In `sale_order.py` (after the existing grove fields, ~line 180):

```python
    # Printify merch (spec 2026-10-13-printify-merch-design.md §6-8).
    grove_is_printify = fields.Boolean(compute="_compute_grove_is_printify", store=True, index=True)
    printify_order_id = fields.Char(readonly=True, copy=False, index=True)
    printify_state = fields.Selection(
        [
            ("queued", "Queued"),
            ("created", "Created"),
            ("in_production", "In production"),
            ("shipped", "Shipped"),
            ("delivered", "Delivered"),
            ("canceled", "Canceled"),
            ("on_hold", "On hold"),
            ("push_failed", "Push failed"),
        ],
        readonly=True,
        copy=False,
        index=True,
    )
    printify_push_attempts = fields.Integer(readonly=True, copy=False, default=0)
    printify_push_last_attempt = fields.Datetime(readonly=True, copy=False)

    @api.depends("order_line.product_id")
    def _compute_grove_is_printify(self):
        for order in self:
            order.grove_is_printify = any(
                line.product_id.product_tmpl_id.grove_fulfillment_route == "printify"
                for line in order.order_line
                if not line.display_type and line.product_id
            )
```

(Add `api` to the `from odoo import ...` line if it is not imported.)

- [ ] **Step 4: Implement the sync method**

In `product_template.py` (imports at top: `import base64`, `import os`, `import requests`, `from odoo.exceptions import UserError`, `from . import printify_client, printify_sync`):

```python
    @api.model
    def _grove_printify_attribute_value(self, attr_name, value_name):
        Attribute = self.env["product.attribute"].sudo()
        attribute = Attribute.search([("name", "=", attr_name)], limit=1) or Attribute.create(
            {"name": attr_name, "create_variant": "always"}
        )
        Value = self.env["product.attribute.value"].sudo()
        value = Value.search([("attribute_id", "=", attribute.id), ("name", "=", value_name)], limit=1)
        return attribute, value or Value.create({"attribute_id": attribute.id, "name": value_name})

    @api.model
    def _grove_printify_sync(self, fetch=None):
        """Upsert every Printify shop product (spec §3). New templates land unpublished
        on the farm company; Odoo's sale price wins after the first create."""
        token = os.environ.get("PRINTIFY_API_TOKEN", "")
        shop_id = os.environ.get("PRINTIFY_SHOP_ID", "")
        if not token or not shop_id:
            raise UserError("PRINTIFY_API_TOKEN and PRINTIFY_SHOP_ID must be configured on this server.")
        fetch = fetch or requests.get
        company = self.env.ref("base.main_company")
        Template = self.sudo().with_context(active_test=False)
        result = {"created": 0, "updated": 0, "archived": 0}
        try:
            raws = printify_client.list_products(token, shop_id)
        except printify_client.PrintifyError as exc:
            raise UserError(f"Printify sync failed: {exc}") from exc

        for raw in raws:
            mapped = printify_sync.map_product(raw)
            enabled = [v for v in mapped["variants"] if v["enabled"]]
            if not enabled:
                continue
            values = {}
            for v in enabled:
                for axis in ("Color", "Size"):
                    name = v[axis.lower()]
                    if name:
                        attribute, value = self._grove_printify_attribute_value(axis, name)
                        values.setdefault(attribute, self.env["product.attribute.value"])
                        values[attribute] |= value

            tmpl = Template.search([("printify_product_id", "=", mapped["printify_product_id"])], limit=1)
            if not tmpl:
                tmpl = Template.create({
                    "name": mapped["name"],
                    "description_sale": mapped["description"],
                    "type": "consu",
                    "is_storable": False,
                    "company_id": company.id,
                    "website_published": False,
                    "grove_fulfillment_route": "printify",
                    "grove_gate_exempt": True,
                    "printify_product_id": mapped["printify_product_id"],
                    "list_price": min(v["price"] for v in enabled),
                    "attribute_line_ids": [
                        (0, 0, {"attribute_id": attribute.id, "value_ids": [(6, 0, vals.ids)]})
                        for attribute, vals in values.items()
                    ],
                })
                result["created"] += 1
                if mapped["image_url"]:
                    resp = fetch(mapped["image_url"], timeout=15)
                    if resp.status_code == 200:
                        tmpl.image_1920 = base64.b64encode(resp.content)
                first_create = True
            else:
                for attribute, vals in values.items():
                    line = tmpl.attribute_line_ids.filtered(lambda ln, a=attribute: ln.attribute_id == a)
                    missing = vals - line.value_ids
                    if line and missing:
                        line.value_ids = [(4, val.id) for val in missing]
                result["updated"] += 1
                first_create = False

            by_combo = {(v["color"], v["size"]): v for v in mapped["variants"]}
            base_price = tmpl.list_price
            for variant in tmpl.with_context(active_test=False).product_variant_ids:
                axis = {ptav.attribute_id.name: ptav.name for ptav in variant.product_template_attribute_value_ids}
                pv = by_combo.get((axis.get("Color", ""), axis.get("Size", "")))
                if not pv or not pv["enabled"]:
                    if variant.active:
                        variant.active = False
                        if variant.printify_variant_id:
                            result["archived"] += 1
                    continue
                vals = {}
                if variant.printify_variant_id != pv["printify_variant_id"]:
                    vals["printify_variant_id"] = pv["printify_variant_id"]
                if variant.standard_price != pv["cost"]:
                    vals["standard_price"] = pv["cost"]
                if not variant.active:
                    vals["active"] = True
                if vals:
                    variant.write(vals)
                if first_create and pv["price"] > base_price:
                    size_ptav = variant.product_template_attribute_value_ids.filtered(
                        lambda p: p.attribute_id.name == "Size"
                    )
                    if size_ptav and size_ptav.price_extra != pv["price"] - base_price:
                        size_ptav.price_extra = pv["price"] - base_price
        return result

    def action_grove_printify_sync(self):
        result = self._grove_printify_sync()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Printify sync",
                "message": f"Created {result['created']}, updated {result['updated']}, archived {result['archived']}.",
                "sticky": False,
            },
        }
```

Note on price: a Size `price_extra` applies to every color in that size, which matches Printify's pricing (size drives cost). It is set only on first create.

- [ ] **Step 5: Server action**

`grove_headless/data/printify_actions.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="action_grove_printify_sync" model="ir.actions.server">
        <field name="name">Sync from Printify</field>
        <field name="model_id" ref="product.model_product_template"/>
        <field name="binding_model_id" ref="product.model_product_template"/>
        <field name="binding_view_types">list,form</field>
        <field name="state">code</field>
        <field name="code">action = model.action_grove_printify_sync()</field>
    </record>
</odoo>
```

Append `"data/printify_actions.xml",` after line 81 of `__manifest__.py` and bump `"version"`.

- [ ] **Step 6: Run tests to verify pass**

Run the CI Odoo test command with `--test-tags='/grove_headless:TestPrintifySync'`.
Expected: 6 passed. Then the full `/grove_headless` suite stays green.

- [ ] **Step 7: Commit**

```bash
git add grove_headless/models/product_template.py grove_headless/models/product_product.py grove_headless/models/sale_order.py grove_headless/data/printify_actions.xml grove_headless/__manifest__.py grove_headless/tests/test_printify_product_sync.py grove_headless/tests/__init__.py conftest.py
git commit -m "feat(grove_headless): Printify fulfillment route fields and Sync from Printify action"
```

---

### Task 4: Catalog API for the farm tenant

**Files:**
- Modify: `grove_headless/controllers/product_domain.py:38-89`
- Modify: `grove_headless/controllers/main.py` (`_structure_variant` ~726-760, `_fulfillment_flags` 471-492, `product_list` 856-861, `product_detail` 948-958)
- Test: `grove_headless/tests/test_product_domain.py` (pure, extend) and `grove_headless/tests/test_printify_catalog.py` (Odoo runtime)

**Interfaces:**
- Produces:
  - `product_domain.company_domain(company_id: int, tenant: str | None) -> list`
  - `build_product_domain(kwargs, company_id, cat_category_ids=None, dept_category_ids=None, tenant=None) -> list`
  - Product payload keys `fulfillment_route`, `lead_time`; variant keys `color`, `size`.

- [ ] **Step 1: Write the failing pure tests** (append to `test_product_domain.py`)

```python
def test_company_domain_default_includes_shared_products():
    assert product_domain.company_domain(7, None) == [("company_id", "in", [7, False])]
    assert product_domain.company_domain(7, "nursery") == [("company_id", "in", [7, False])]


def test_company_domain_goldberry_only_shares_printify_products():
    assert product_domain.company_domain(1, "goldberry") == [
        "|",
        ("company_id", "=", 1),
        "&",
        ("company_id", "=", False),
        ("grove_fulfillment_route", "=", "printify"),
    ]


def test_build_product_domain_goldberry_uses_tenant_rule():
    domain = product_domain.build_product_domain({}, 1, tenant="goldberry")
    assert domain[:1] == [("website_published", "=", True)]
    assert ("grove_fulfillment_route", "=", "printify") in domain


def test_build_product_domain_unchanged_without_tenant():
    assert product_domain.build_product_domain({}, 7) == BASE
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest grove_headless/tests/test_product_domain.py -v`
Expected: the 3 new tenant tests FAIL (`company_domain` missing).

- [ ] **Step 3: Implement in `product_domain.py`**

```python
def company_domain(company_id, tenant):
    """Company visibility for a storefront. Shared (company_id=False) products show on
    every tenant, except the farm, where only Printify-routed shared products show
    (spec §4: shared trees must not leak onto goldberrygrove.farm)."""
    if tenant == "goldberry":
        return [
            "|",
            ("company_id", "=", company_id),
            "&",
            ("company_id", "=", False),
            ("grove_fulfillment_route", "=", "printify"),
        ]
    return [("company_id", "in", [company_id, False])]
```

Change the signature to `def build_product_domain(kwargs: dict, company_id: int, cat_category_ids=None, dept_category_ids=None, tenant=None) -> list:` and replace the base domain (lines 45-48) with:

```python
    domain = [("website_published", "=", True)] + company_domain(company_id, tenant)
```

- [ ] **Step 4: Run pure tests to verify pass**

Run: `python3 -m pytest grove_headless/tests/test_product_domain.py -v`
Expected: all pass, including the existing `BASE` test.

- [ ] **Step 5: Write the failing Odoo catalog test** (`test_printify_catalog.py`)

```python
import json
from datetime import datetime, timedelta

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.tests import tagged
from odoo.tests.common import HttpCase, get_db_name

from .common import GroveTaxFixtureMixin


@tagged("grove_headless", "post_install", "-at_install")
class TestPrintifyCatalog(GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        color = self.env["product.attribute"].create({"name": "Color", "create_variant": "always"})
        size = self.env["product.attribute"].create({"name": "Size", "create_variant": "always"})
        natural = self.env["product.attribute.value"].create({"attribute_id": color.id, "name": "Natural"})
        medium = self.env["product.attribute.value"].create({"attribute_id": size.id, "name": "M"})
        self.shirt = self.env["product.template"].create({
            "name": "Grove Tee", "type": "consu", "is_storable": False, "list_price": 28.0,
            "company_id": False, "website_published": True, "grove_fulfillment_route": "printify",
            "grove_gate_exempt": True, "printify_lead_time": "3 to 7 business days",
            "attribute_line_ids": [
                (0, 0, {"attribute_id": color.id, "value_ids": [(6, 0, natural.ids)]}),
                (0, 0, {"attribute_id": size.id, "value_ids": [(6, 0, medium.ids)]}),
            ],
        })
        self.shared_tree = self.env["product.template"].create({
            "name": "Shared Tree", "type": "consu", "list_price": 12.0,
            "company_id": False, "website_published": True,
        })
        admin = self.env.ref("base.user_admin")
        try:
            self.api_key = self.env["res.users.apikeys"].with_user(admin)._generate(
                "rpc", "grove-printify-catalog", datetime.now() + timedelta(days=1)
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"API keys unavailable: {exc}")

    def _get(self, path, tenant):
        headers = {"X-Odoo-Database": get_db_name(), "X-Grove-Tenant": tenant, "Authorization": f"Bearer {self.api_key}"}
        return json.loads(self.url_open(path, headers=headers).content)

    def _names(self, body):
        return {p["name"] for p in body.get("products", [])}

    def test_farm_lists_shirt_but_not_shared_tree(self):
        names = self._names(self._get("/grove/api/v1/products", "goldberry"))
        self.assertIn("Grove Tee", names)
        self.assertNotIn("Shared Tree", names)

    def test_nursery_still_lists_shared_tree(self):
        self.assertIn("Shared Tree", self._names(self._get("/grove/api/v1/products", "nursery")))

    def test_farm_detail_refuses_shared_tree(self):
        resp = self.url_open(
            f"/grove/api/v1/products/{self.shared_tree.id}",
            headers={"X-Odoo-Database": get_db_name(), "X-Grove-Tenant": "goldberry", "Authorization": f"Bearer {self.api_key}"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_flags_and_variant_axes(self):
        flags = grove_main._fulfillment_flags(self.shirt)
        self.assertEqual(flags["fulfillment_route"], "printify")
        self.assertEqual(flags["lead_time"], "3 to 7 business days")
        variant = grove_main._structure_variant(self.shirt.product_variant_ids[0])
        self.assertEqual((variant["color"], variant["size"]), ("Natural", "M"))
```

If `product_detail`'s route or 404 shape differs, read `main.py:934-1000` and match its actual path and not-found status; keep the assertion that the farm cannot fetch the shared tree.

- [ ] **Step 6: Run to verify failure**

Run the CI Odoo test command with `--test-tags='/grove_headless:TestPrintifyCatalog'`.
Expected: FAIL (shared tree listed on farm; flags missing `fulfillment_route`; variant missing `color`).

- [ ] **Step 7: Implement in `main.py`**

`product_list` (856-861): pass the tenant.

```python
        domain = build_product_domain(
            kwargs,
            current_company.id,
            cat_category_ids=...,  # unchanged
            dept_category_ids=...,  # unchanged
            tenant=website.grove_tenant_slug(),
        )
```

`product_detail` (952-956): replace `("company_id", "in", [current_company.id, False]),` so the list reads:

```python
                [("id", "=", product_id), ("website_published", "=", True)]
                + company_domain(current_company.id, request.website.grove_tenant_slug()),
```

and import `company_domain` beside `build_product_domain` at the top of `main.py`.

`_fulfillment_flags` (return dict, ~487): add two keys.

```python
        "fulfillment_route": product.grove_fulfillment_route or "in_house",
        "lead_time": product.printify_lead_time if product.grove_fulfillment_route == "printify" else None,
```

`_structure_variant` (after `"rootstock": ...`, ~751): add

```python
        # Printify merch axes (spec §4). Empty for plants.
        "color": axis.get("Color", ""),
        "size": axis.get("Size", ""),
```

- [ ] **Step 8: Run tests to verify pass**

Run `--test-tags='/grove_headless:TestPrintifyCatalog'` then the full `/grove_headless` suite.
Expected: all pass; existing catalog tests unchanged.

- [ ] **Step 9: Commit**

```bash
git add grove_headless/controllers/product_domain.py grove_headless/controllers/main.py grove_headless/tests/test_product_domain.py grove_headless/tests/test_printify_catalog.py grove_headless/tests/__init__.py conftest.py
git commit -m "feat(grove_headless): farm catalog shows Printify products only from shared pool; color/size/route/lead_time in payload"
```

---

### Task 5: Draft order and tax for Printify carts

**Files:**
- Modify: `grove_headless/controllers/main.py` (`_create_draft_order` 2986-3475, `_apply_shipping_line` 2610-2676, `_stripe_tax_code_for` 2771-2781)
- Modify: `grove_headless/models/stripe_gateway.py:33-35`
- Test: `grove_headless/tests/test_printify_checkout.py` (Odoo runtime)

**Interfaces:**
- Consumes: `printify_client.calculate_shipping`, `printify_client.US_50_AND_DC`, `printify_client.PrintifyError`.
- Produces:
  - `stripe_gateway.TAX_CODE_APPAREL = "txcd_30011000"`
  - `main.PRINTIFY_MIXED_CART_MESSAGE`, `PRINTIFY_DESTINATION_MESSAGE`, `PRINTIFY_SHIPPING_UNAVAILABLE`, `PRINTIFY_PICKUP_MESSAGE` (str constants)
  - `main._printify_address(contact: dict, shipping: dict, state: str) -> dict`
  - `main._printify_line_items(order) -> list[dict]`
  - `main._printify_quote(line_items, address) -> float` (raises `PrintifyError`; reads env)
  - `main._add_shipping_line(env, order, charge, state, company) -> float` (extracted from `_apply_shipping_line`)

- [ ] **Step 1: Write the failing tests**

```python
from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import printify_client, stripe_gateway
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin


class _PrintifyFixture:
    def _build(self):
        self.company = self.env.ref("base.main_company")
        size = self.env["product.attribute"].create({"name": "Size", "create_variant": "always"})
        medium = self.env["product.attribute.value"].create({"attribute_id": size.id, "name": "M"})
        self.shirt_tmpl = self.env["product.template"].create({
            "name": "Grove Tee", "type": "consu", "is_storable": False, "list_price": 28.0,
            "company_id": self.company.id, "website_published": True, "grove_fulfillment_route": "printify",
            "printify_product_id": "6650abc",
            "attribute_line_ids": [(0, 0, {"attribute_id": size.id, "value_ids": [(6, 0, medium.ids)]})],
        })
        self.shirt = self.shirt_tmpl.product_variant_ids[0]
        self.shirt.write({"printify_variant_id": 11001})
        self.tree = self.env["product.template"].create({
            "name": "Pawpaw", "type": "consu", "is_storable": True, "list_price": 12.0,
            "company_id": self.company.id, "grove_shipping_tier": "potted",
        }).product_variant_ids[0]

    def _website(self):
        return self.env["website"].search([("company_id", "=", self.company.id)], limit=1) or self.env["website"].search([], limit=1)

    def _payload(self, state="CA", items=None, **extra):
        payload = {
            "contact": {"name": "Shirt Buyer", "email": "shirt@example.com", "phone": "3045551212"},
            "items": items or [{"variant_id": self.shirt.id, "quantity": 1}],
            "shipping": {"street": "1 Rd", "city": "Town", "state": state, "zip": "90001"},
        }
        payload.update(extra)
        return payload


@tagged("grove_headless", "post_install", "-at_install")
class TestPrintifyDraftOrder(_PrintifyFixture, GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self._build()

    def _draft(self, payload, quote=4.75):
        patch = mock.patch.object(grove_main, "_printify_quote", return_value=quote)
        if isinstance(quote, Exception):
            patch = mock.patch.object(grove_main, "_printify_quote", side_effect=quote)
        with patch as quoted:
            order, error = grove_main._create_draft_order(self._website(), self.env, payload)
        return order, error, quoted

    def _orphans(self):
        return self.env["sale.order"].search([("partner_id.email", "=", "shirt@example.com")])

    def test_shirt_ships_to_non_green_state_with_printify_quote(self):
        order, error, quoted = self._draft(self._payload("CA"))
        self.assertIsNone(error)
        ship = order.order_line.filtered(lambda ln: ln.product_id.default_code == grove_main.SHIPPING_PRODUCT_CODE)
        self.assertEqual(ship.price_unit, 4.75)
        self.assertTrue(order.grove_is_printify)
        self.assertEqual(order.grove_fulfillment, "ship")
        line_items, address = quoted.call_args.args
        self.assertEqual(line_items, [{"product_id": "6650abc", "variant_id": 11001, "quantity": 1}])
        self.assertEqual((address["country"], address["region"], address["zip"]), ("US", "CA", "90001"))

    def test_mixed_cart_refused_409(self):
        items = [{"variant_id": self.shirt.id, "quantity": 1}, {"variant_id": self.tree.id, "quantity": 1}]
        order, error, quoted = self._draft(self._payload("WV", items=items))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 409)
        self.assertIn("Shirts check out separately from trees.", error.data.decode())
        quoted.assert_not_called()
        self.assertFalse(self._orphans())

    def test_territory_refused_400_without_quote(self):
        order, error, quoted = self._draft(self._payload("PR"))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("50 US states and DC", error.data.decode())
        quoted.assert_not_called()
        self.assertFalse(self._orphans())

    def test_pickup_refused_400(self):
        order, error, _quoted = self._draft(self._payload("WV", fulfillment="pickup"))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)

    def test_quote_failure_is_503_and_no_orphan(self):
        order, error, _quoted = self._draft(self._payload("CA"), quote=printify_client.PrintifyError("down", 503))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 503)
        self.assertIn("Shipping is temporarily unavailable. Try again in a few minutes.", error.data.decode())
        self.assertFalse(self._orphans())

    def test_tree_order_unchanged_still_blocks_non_green(self):
        order, error, quoted = self._draft(self._payload("CA", items=[{"variant_id": self.tree.id, "quantity": 1}]))
        self.assertIsNone(order)
        self.assertEqual(error.status_code, 400)
        self.assertIn("can't ship", error.data.decode().lower())
        quoted.assert_not_called()

    def test_apparel_tax_code(self):
        self.assertEqual(stripe_gateway.TAX_CODE_APPAREL, "txcd_30011000")
        self.assertEqual(grove_main._stripe_tax_code_for(self.shirt), "txcd_30011000")
        self.assertEqual(grove_main._stripe_tax_code_for(self.tree), stripe_gateway.TAX_CODE_GOODS)
```

How the payload marks pickup: check `_create_draft_order` lines 3176-3196 for the exact key the existing tests use (`fulfillment="pickup"` in `test_stripe_checkout.py:957`) and match it.

- [ ] **Step 2: Run to verify failure**

Run `--test-tags='/grove_headless:TestPrintifyDraftOrder'`.
Expected: FAIL (`_printify_quote` missing; CA refused by the green list).

- [ ] **Step 3: Tax code**

`stripe_gateway.py` after line 35:

```python
TAX_CODE_APPAREL = "txcd_30011000"  # Clothing (Printify merch)
```

`_stripe_tax_code_for` (2771): first line of the body:

```python
    if getattr(product, "grove_fulfillment_route", None) == "printify":
        return stripe_gateway.TAX_CODE_APPAREL
```

- [ ] **Step 4: Extract `_add_shipping_line`**

Move the block in `_apply_shipping_line` that starts at `product = _get_shipping_product(env, company)` and ends at `return charge` (main.py ~2655-2676) verbatim into:

```python
def _add_shipping_line(env, order, charge, state, company):
    """Write the GROVE-SHIP line with the WV state tax forced (GOL-2449). Shared by
    the Box Engine path and the Printify quote path."""
    product = _get_shipping_product(env, company)
    ship_line = (
        env["sale.order.line"]
        .sudo()
        .create(
            {
                "order_id": order.id,
                "product_id": product.id,
                "name": f"Shipping ({state})",
                "product_uom_qty": 1.0,
                "price_unit": charge,
            }
        )
    )
    wv_state = _get_company_wv_state_tax(env, company)
    if set(ship_line.tax_ids.ids) != set(wv_state.ids):
        ship_line.tax_ids = [(6, 0, wv_state.ids)]
    order.invalidate_recordset(["amount_untaxed", "amount_tax", "amount_total"])
    return charge
```

and end `_apply_shipping_line` with `return _add_shipping_line(env, order, charge, state, company)`. Run the full `/grove_headless` suite: all existing shipping tests must still pass before continuing.

- [ ] **Step 5: Printify helpers** (module level in `main.py`, near `_apply_shipping_line`; `import os` already present, add `from ..models import printify_client`)

```python
PRINTIFY_MIXED_CART_MESSAGE = "Shirts check out separately from trees."
PRINTIFY_DESTINATION_MESSAGE = "Shirts ship to the 50 US states and DC."
PRINTIFY_SHIPPING_UNAVAILABLE = "Shipping is temporarily unavailable. Try again in a few minutes."
PRINTIFY_PICKUP_MESSAGE = "Shirts ship to you. Farm pickup isn't available for them."


def _printify_address(contact, shipping, state):
    name = (contact.get("name") or "").strip()
    first, _, last = name.partition(" ")
    return {
        "first_name": first or name,
        "last_name": last or first or name,
        "email": contact.get("email") or "",
        "phone": contact.get("phone") or "",
        "country": "US",
        "region": state,
        "address1": shipping.get("street") or "",
        "address2": shipping.get("street2") or "",
        "city": shipping.get("city") or "",
        "zip": shipping.get("zip") or "",
    }


def _printify_line_items(order):
    return [
        {
            "product_id": line.product_id.product_tmpl_id.printify_product_id,
            "variant_id": line.product_id.printify_variant_id,
            "quantity": int(line.product_uom_qty),
        }
        for line in order.order_line
        if not line.display_type
        and line.product_id
        and line.product_id.product_tmpl_id.grove_fulfillment_route == "printify"
    ]


def _printify_quote(line_items, address):
    token = os.environ.get("PRINTIFY_API_TOKEN", "")
    shop_id = os.environ.get("PRINTIFY_SHOP_ID", "")
    if not token or not shop_id:
        raise printify_client.PrintifyError("Printify is not configured on this server")
    return printify_client.calculate_shipping(token, shop_id, line_items, address)
```

- [ ] **Step 6: Branch `_create_draft_order`**

Right after `is_ship_to` is computed and the `_validate_ship_wave` block (~line 3200), insert:

```python
    # Printify merch (spec §5): one fulfillment route per order; shirts skip every
    # plant rule and take a live Printify shipping quote instead of Box Engine.
    goods_lines = [
        line for line in order.order_line
        if not line.display_type and line.product_id and line.product_id.product_tmpl_id.type != "service"
    ]
    printify_goods = [ln for ln in goods_lines if ln.product_id.product_tmpl_id.grove_fulfillment_route == "printify"]
    is_printify = bool(printify_goods)
    if is_printify and len(printify_goods) != len(goods_lines):
        order.unlink()
        return None, _json_response({"error": PRINTIFY_MIXED_CART_MESSAGE}, status=409)
    printify_charge = None
    if is_printify:
        if is_pickup or not is_ship_to:
            order.unlink()
            return None, _json_response({"error": PRINTIFY_PICKUP_MESSAGE}, status=400)
        dest = canonical_state_code(ship_state)
        if dest not in printify_client.US_50_AND_DC:
            order.unlink()
            return None, _json_response({"error": PRINTIFY_DESTINATION_MESSAGE}, status=400)
        try:
            printify_charge = _printify_quote(
                _printify_line_items(order), _printify_address(payload.get("contact") or {}, shipping, dest)
            )
        except printify_client.PrintifyError:
            _logger.warning("grove_headless: Printify shipping quote failed for draft %s", order.name, exc_info=True)
            order.unlink()
            return None, _json_response({"error": PRINTIFY_SHIPPING_UNAVAILABLE}, status=503)
```

Then guard the plant gates so Printify orders skip them:
- green-list gate (3201): `if is_ship_to:` → `if is_ship_to and not is_printify:`
- `ship_items` / `unshippable_reason` (3216-3230): wrap in `if not is_printify:` and set `ship_items = []` otherwise
- pickup-only gate (3234-3254) and compliance loop (3267+): wrap each in `if not is_printify:`
- shipping line (3373):

```python
    if is_printify:
        shipping_charge = _add_shipping_line(env, order, printify_charge, canonical_state_code(ship_state), current_company)
    else:
        shipping_charge = None if is_pickup else _apply_shipping_line(env, order, shipping, current_company)
```

The no-$0 breaker (3379) is unchanged: `ship_items` is empty for Printify, and `printify_charge` is always set when we get there. The variable name `shipping` above is whatever `_create_draft_order` already binds the payload's shipping dict to (see line ~3176); use the existing name.

- [ ] **Step 7: Run tests to verify pass**

Run `--test-tags='/grove_headless:TestPrintifyDraftOrder'`, then the full `/grove_headless` suite.
Expected: 7 passed; all existing checkout, compliance and ship-wave tests green.

- [ ] **Step 8: Commit**

```bash
git add grove_headless/controllers/main.py grove_headless/models/stripe_gateway.py grove_headless/tests/test_printify_checkout.py grove_headless/tests/__init__.py conftest.py
git commit -m "feat(grove_headless): Printify carts skip plant rules, take a live Printify shipping quote and apparel tax code"
```

---

### Task 6: Checkout quote endpoint, push queue and oversell skip

**Files:**
- Modify: `grove_headless/controllers/main.py` (`checkout_quote` 1787-1894, `_handle_session_completed` 4641-4727, `_oversold_lines` 3934-3970)
- Modify: `grove_headless/models/sale_order.py` (push + cron + retry)
- Create: `grove_headless/data/printify_cron.xml`
- Modify: `grove_headless/data/printify_actions.xml` (retry server action), `__manifest__.py` (append cron xml)
- Test: `grove_headless/tests/test_printify_push.py` (Odoo runtime); extend `test_printify_checkout.py` with the quote endpoint test

**Interfaces:**
- Consumes: Task 5 helpers, `printify_client.create_order`, `find_order_by_external_id`, `send_to_production`.
- Produces:
  - `/checkout/quote` request key `shipping: {state, zip}` (the storefront already sends `shipping.zip`; `state` is new); response keys `fulfillment_route` (`"printify"` or `"in_house"`) and `shipping_amount` (float or null)
  - `sale.order._grove_printify_push() -> bool`, `sale.order._cron_printify_push()`, `sale.order.action_grove_retry_printify_push()`
  - `_PRINTIFY_BACKOFF_MINUTES = (0, 5, 30, 120)`, `_PRINTIFY_MAX_ATTEMPTS = 3`

- [ ] **Step 1: Write the failing push tests**

```python
import os
from datetime import timedelta
from unittest import mock

from odoo import fields
from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import printify_client
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin
from .test_printify_checkout import _PrintifyFixture

ENV = {"PRINTIFY_API_TOKEN": "tok", "PRINTIFY_SHOP_ID": "42", "PRINTIFY_DRY_RUN": ""}


@tagged("grove_headless", "post_install", "-at_install")
class TestPrintifyPush(_PrintifyFixture, GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self._build()
        with mock.patch.object(grove_main, "_printify_quote", return_value=4.75):
            self.order, _err = grove_main._create_draft_order(self._website(), self.env, self._payload("CA"))
        self.order.write({"printify_state": "queued"})

    def _patches(self, create="po_1", existing=None, send=None, env=None):
        return (
            mock.patch.dict(os.environ, env or ENV, clear=False),
            mock.patch.object(printify_client, "find_order_by_external_id", return_value=existing),
            mock.patch.object(printify_client, "create_order", return_value=create)
            if not isinstance(create, Exception) else mock.patch.object(printify_client, "create_order", side_effect=create),
            mock.patch.object(printify_client, "send_to_production", side_effect=send),
            mock.patch.object(grove_main, "_notify_discord"),
        )

    def _run(self, **kw):
        p_env, p_find, p_create, p_send, p_discord = self._patches(**kw)
        with p_env, p_find, p_create as create, p_send as send, p_discord as discord:
            self.order._grove_printify_push()
        return create, send, discord

    def test_push_creates_and_sends_to_production(self):
        create, send, _discord = self._run()
        payload = create.call_args.args[2]
        self.assertEqual(payload["external_id"], self.order.name)
        self.assertEqual(payload["shipping_method"], 1)
        self.assertFalse(payload["send_shipping_notification"])
        self.assertEqual(payload["line_items"], [{"product_id": "6650abc", "variant_id": 11001, "quantity": 1}])
        send.assert_called_once()
        self.assertEqual(self.order.printify_order_id, "po_1")
        self.assertEqual(self.order.printify_state, "in_production")

    def test_retry_reuses_existing_printify_order(self):
        create, send, _discord = self._run(existing={"id": "po_9", "status": "pending"})
        create.assert_not_called()
        send.assert_called_once()
        self.assertEqual(self.order.printify_order_id, "po_9")

    def test_existing_order_already_in_production_not_resent(self):
        _create, send, _discord = self._run(existing={"id": "po_9", "status": "in-production"})
        send.assert_not_called()
        self.assertEqual(self.order.printify_state, "in_production")

    def test_dry_run_skips_send_to_production(self):
        _create, send, _discord = self._run(env=dict(ENV, PRINTIFY_DRY_RUN="1"))
        send.assert_not_called()
        self.assertEqual(self.order.printify_state, "created")

    def test_failure_backs_off_then_alerts_after_three(self):
        for attempt in range(1, 4):
            _c, _s, discord = self._run(create=printify_client.PrintifyError("boom", 500))
            self.assertEqual(self.order.printify_push_attempts, attempt)
        self.assertEqual(self.order.printify_state, "push_failed")
        discord.assert_called_once()

    def test_cron_respects_backoff(self):
        self.order.write({"printify_push_attempts": 1, "printify_push_last_attempt": fields.Datetime.now()})
        p_env, p_find, p_create, p_send, p_discord = self._patches()
        with p_env, p_find, p_create as create, p_send, p_discord:
            self.env["sale.order"]._cron_printify_push()
        create.assert_not_called()
        self.order.write({"printify_push_last_attempt": fields.Datetime.now() - timedelta(minutes=6)})
        with p_env, p_find, p_create as create, p_send, p_discord:
            self.env["sale.order"]._cron_printify_push()
        create.assert_called_once()

    def test_retry_action_requeues(self):
        self.order.write({"printify_state": "push_failed", "printify_push_attempts": 3})
        self.order.action_grove_retry_printify_push()
        self.assertEqual((self.order.printify_state, self.order.printify_push_attempts), ("queued", 0))

    def test_paid_printify_order_not_oversold(self):
        self.assertEqual(grove_main._oversold_lines(self.order), [])

    def test_session_completed_queues_push(self):
        self.order.write({"printify_state": False, "grove_stripe_session_id": "cs_shirt"})
        with mock.patch.object(grove_main, "_notify_discord"), mock.patch.object(grove_main, "_notify_new_order"), \
                mock.patch.object(grove_main, "_notify_merchant_email"), \
                mock.patch.object(grove_main, "_send_order_confirmation_email"):
            grove_main._handle_session_completed(self.env, {"id": "cs_shirt", "payment_intent": "pi_shirt"})
        self.assertEqual(self.order.printify_state, "queued")
```

Match `_handle_session_completed`'s session lookup: read `main.py:4641-4660` for how it finds the order (session id field, metadata) and set the same on `self.order` in the last test.

- [ ] **Step 2: Run to verify failure**

Run `--test-tags='/grove_headless:TestPrintifyPush'`.
Expected: FAIL (`_grove_printify_push` missing; oversold returns the shirt line).

- [ ] **Step 3: Oversell skip** (`_oversold_lines`, after the `SHIPPING_PRODUCT_CODE` / preorder `continue`, ~3963)

```python
        # Printify merch is printed to order: no stock to oversell (spec Review Focus 1).
        if product.product_tmpl_id.grove_fulfillment_route == "printify":
            continue
```

- [ ] **Step 4: Queue on payment** (`_handle_session_completed`, after `_notify_merchant_email(...)` at ~4720)

```python
    if order.grove_is_printify:
        try:
            order.sudo().write({"printify_state": "queued"})
        except Exception:  # noqa: BLE001 — never fail the Stripe webhook
            _logger.exception("grove_headless: could not queue Printify push for %s", order.name)
```

- [ ] **Step 5: Push, cron and retry** (`sale_order.py`; imports `import os`, `from datetime import timedelta`, `from odoo.tools import config`, `from . import printify_client`)

```python
    _PRINTIFY_BACKOFF_MINUTES = (0, 5, 30, 120)
    _PRINTIFY_MAX_ATTEMPTS = 3
    _PRINTIFY_SENDABLE = ("pending", "on-hold")

    def _printify_push_due(self, now=None):
        last = self.printify_push_last_attempt
        if not last:
            return True
        idx = min(self.printify_push_attempts or 0, len(self._PRINTIFY_BACKOFF_MINUTES) - 1)
        return (now or fields.Datetime.now()) - last >= timedelta(minutes=self._PRINTIFY_BACKOFF_MINUTES[idx])

    def _printify_order_payload(self):
        from ..controllers.main import _printify_address, _printify_line_items

        partner = self.partner_shipping_id or self.partner_id
        shipping = {"street": partner.street, "street2": partner.street2, "city": partner.city, "zip": partner.zip}
        contact = {"name": partner.name, "email": partner.email or self.partner_id.email, "phone": partner.phone}
        return {
            "external_id": self.name,
            "label": self.name,
            "line_items": _printify_line_items(self),
            "shipping_method": 1,
            "send_shipping_notification": False,
            "address_to": _printify_address(contact, shipping, partner.state_id.code or ""),
        }

    def _grove_printify_push(self):
        """Create (or find) the Printify order and send it to production (spec §6).
        Bounded retries: the cron calls this per due order; never loops."""
        from ..controllers.main import _notify_discord

        self.ensure_one()
        token = os.environ.get("PRINTIFY_API_TOKEN", "")
        shop_id = os.environ.get("PRINTIFY_SHOP_ID", "")
        dry_run = (os.environ.get("PRINTIFY_DRY_RUN") or "").strip().lower() in ("1", "true", "yes", "on")
        try:
            if not token or not shop_id:
                raise printify_client.PrintifyError("Printify is not configured on this server")
            existing = printify_client.find_order_by_external_id(token, shop_id, self.name)
            if existing:
                po_id, status = str(existing["id"]), existing.get("status") or "pending"
            else:
                po_id, status = printify_client.create_order(token, shop_id, self._printify_order_payload()), "pending"
            self.sudo().write({"printify_order_id": po_id, "printify_state": "created"})
            if status not in self._PRINTIFY_SENDABLE:
                self.sudo().write({"printify_state": "in_production"})
            elif not dry_run:
                printify_client.send_to_production(token, shop_id, po_id)
                self.sudo().write({"printify_state": "in_production"})
            self.message_post(body=f"Printify order {po_id} {'created (dry run)' if dry_run else 'sent to production'}.")
            return True
        except printify_client.PrintifyError as exc:
            attempts = (self.printify_push_attempts or 0) + 1
            vals = {"printify_push_attempts": attempts, "printify_push_last_attempt": fields.Datetime.now()}
            if attempts >= self._PRINTIFY_MAX_ATTEMPTS:
                vals["printify_state"] = "push_failed"
            self.sudo().write(vals)
            self.message_post(body=f"Printify push attempt {attempts} failed: {exc}")
            if attempts >= self._PRINTIFY_MAX_ATTEMPTS:
                _notify_discord(f":rotating_light: Printify push FAILED on {self.name} after {attempts} tries: {exc}")
            return False

    @api.model
    def _cron_printify_push(self):
        now = fields.Datetime.now()
        for order in self.sudo().search([("printify_state", "=", "queued")]):
            if not order._printify_push_due(now=now):
                continue
            try:
                order._grove_printify_push()
            except Exception:  # noqa: BLE001 — the cron must never raise
                _logger.exception("Printify push crashed for %s", order.name)
            if not config["test_enable"]:
                self.env.cr.commit()

    def action_grove_retry_printify_push(self):
        for order in self:
            order.sudo().write({"printify_state": "queued", "printify_push_attempts": 0, "printify_push_last_attempt": False})
            order.message_post(body="Printify push re-queued.")
```

- [ ] **Step 6: Cron and retry action XML**

`grove_headless/data/printify_cron.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo noupdate="1">
    <record id="ir_cron_grove_printify_push" model="ir.cron">
        <field name="name">Grove: push paid orders to Printify</field>
        <field name="model_id" ref="sale.model_sale_order"/>
        <field name="state">code</field>
        <field name="code">model._cron_printify_push()</field>
        <field name="interval_number">5</field>
        <field name="interval_type">minutes</field>
        <field name="active" eval="True"/>
    </record>
</odoo>
```

Append to `printify_actions.xml`:

```xml
    <record id="action_grove_retry_printify_push" model="ir.actions.server">
        <field name="name">Retry Printify push</field>
        <field name="model_id" ref="sale.model_sale_order"/>
        <field name="binding_model_id" ref="sale.model_sale_order"/>
        <field name="binding_view_types">list,form</field>
        <field name="state">code</field>
        <field name="code">records.action_grove_retry_printify_push()</field>
    </record>
```

Add `"data/printify_cron.xml",` to the manifest data list.

- [ ] **Step 7: Quote endpoint test** (append to `test_printify_checkout.py`)

```python
@tagged("grove_headless", "post_install", "-at_install")
class TestPrintifyQuoteEndpoint(_PrintifyFixture, GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self._build()
        admin = self.env.ref("base.user_admin")
        try:
            self.api_key = self.env["res.users.apikeys"].with_user(admin)._generate(
                "rpc", "grove-printify-quote", datetime.now() + timedelta(days=1)
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"API keys unavailable: {exc}")

    def _post(self, body):
        headers = {"X-Odoo-Database": get_db_name(), "X-Grove-Tenant": "goldberry",
                   "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        return self.url_open("/grove/api/v1/checkout/quote", data=json.dumps(body).encode(), headers=headers)

    def _body(self, **extra):
        body = {"items": [{"variant_id": self.shirt.id, "quantity": 1}], "fulfillment": "ship",
                "shipping": {"state": "CA", "zip": "90001"}}
        body.update(extra)
        return body

    def test_quote_returns_printify_shipping(self):
        with mock.patch.object(grove_main, "_printify_quote", return_value=4.75):
            data = json.loads(self._post(self._body()).content)
        self.assertEqual(data["fulfillment_route"], "printify")
        self.assertEqual(data["shipping_amount"], 4.75)
        self.assertFalse(data["deposit_now"])

    def test_quote_without_address_has_null_shipping(self):
        with mock.patch.object(grove_main, "_printify_quote") as quoted:
            data = json.loads(self._post(self._body(shipping=None)).content)
        self.assertIsNone(data["shipping_amount"])
        quoted.assert_not_called()

    def test_quote_503_when_printify_down(self):
        with mock.patch.object(grove_main, "_printify_quote", side_effect=printify_client.PrintifyError("x", 503)):
            resp = self._post(self._body())
        self.assertEqual(resp.status_code, 503)

    def test_quote_409_mixed(self):
        resp = self._post(self._body(items=[{"variant_id": self.shirt.id, "quantity": 1},
                                            {"variant_id": self.tree.id, "quantity": 1}]))
        self.assertEqual(resp.status_code, 409)

    def test_tree_quote_unchanged(self):
        data = json.loads(self._post(self._body(items=[{"variant_id": self.tree.id, "quantity": 1}], shipping=None)).content)
        self.assertEqual(data["fulfillment_route"], "in_house")
        self.assertIsNone(data["shipping_amount"])
```

(Imports to add at the top of `test_printify_checkout.py`: `import json`, `from datetime import datetime, timedelta`, `from odoo.tests.common import HttpCase, get_db_name`.)

- [ ] **Step 8: Implement in `checkout_quote`**

After the variants are looked up (before `_validate_ship_wave`), insert:

```python
        route_lines = [v for v in variants if v.product_tmpl_id.type != "service"]
        printify = [v for v in route_lines if v.product_tmpl_id.grove_fulfillment_route == "printify"]
        if printify and len(printify) != len(route_lines):
            return _json_response({"error": PRINTIFY_MIXED_CART_MESSAGE}, status=409)
        shipping_amount = None
        if printify:
            ship_to = payload.get("shipping") or {}
            dest = canonical_state_code(ship_to.get("state") or "")
            if ship_to.get("state") and dest not in printify_client.US_50_AND_DC:
                return _json_response({"error": PRINTIFY_DESTINATION_MESSAGE}, status=400)
            if dest and ship_to.get("zip"):
                qty = {int(i["variant_id"]): int(i.get("quantity") or 1) for i in items}
                line_items = [
                    {"product_id": v.product_tmpl_id.printify_product_id, "variant_id": v.printify_variant_id,
                     "quantity": qty.get(v.id, 1)}
                    for v in printify
                ]
                try:
                    shipping_amount = _printify_quote(line_items, _printify_address({}, ship_to, dest))
                except printify_client.PrintifyError:
                    return _json_response({"error": PRINTIFY_SHIPPING_UNAVAILABLE}, status=503)
```

Use the names `checkout_quote` already binds (`payload`, `items`, the variants recordset); read lines 1787-1860 and adapt. Then add two keys to the response dict: `"fulfillment_route": "printify" if printify else "in_house"` and `"shipping_amount": shipping_amount`.

- [ ] **Step 9: Run tests to verify pass**

Run `--test-tags='/grove_headless:TestPrintifyPush,/grove_headless:TestPrintifyQuoteEndpoint'`, then the full suite (`test_checkout_quote.py` must stay green).
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add grove_headless/controllers/main.py grove_headless/models/sale_order.py grove_headless/data/printify_cron.xml grove_headless/data/printify_actions.xml grove_headless/__manifest__.py grove_headless/tests/test_printify_push.py grove_headless/tests/test_printify_checkout.py grove_headless/tests/__init__.py conftest.py
git commit -m "feat(grove_headless): Printify quote on /checkout/quote, paid-order push cron with backoff, oversell skip"
```

---

### Task 7: Printify webhook

**Files:**
- Modify: `grove_headless/controllers/main.py` (new route in `GroveHeadlessAPI` next to `stripe_webhook`, plus `_handle_printify_event`)
- Test: `grove_headless/tests/test_printify_webhook.py` (Odoo runtime)

**Interfaces:**
- Consumes: `printify_client.verify_signature`, `printify_client.get_order`, `_apply_delivery_status`.
- Produces: `POST /grove/api/v1/printify/webhook`; `main._handle_printify_event(env, event: dict) -> str` (returns the action taken, for tests).

First, confirm the signature header name and event payload against developers.printify.com (Webhooks). The plan assumes header `X-Pfy-Signature` with `sha256=<hex>` and events shaped `{"type": "...", "resource": {"id": "<printify order id>", "data": {...}}}`. If the docs differ, change the header constant and the `resource` lookup only; the handler fetches the order with `get_order` for shipment details, so it does not depend on the event's shipment fields.

- [ ] **Step 1: Write the failing tests**

```python
import hashlib
import hmac
import json
import os
from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import printify_client
from odoo.tests import tagged
from odoo.tests.common import HttpCase, get_db_name

from .common import GroveTaxFixtureMixin
from .test_printify_checkout import _PrintifyFixture

SECRET = "whsec_test"
ENV = {"PRINTIFY_API_TOKEN": "tok", "PRINTIFY_SHOP_ID": "42", "PRINTIFY_WEBHOOK_SECRET": SECRET}


@tagged("grove_headless", "post_install", "-at_install")
class TestPrintifyWebhook(_PrintifyFixture, GroveTaxFixtureMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self._build()
        with mock.patch.object(grove_main, "_printify_quote", return_value=4.75):
            self.order, _err = grove_main._create_draft_order(self._website(), self.env, self._payload("CA"))
        self.order.write({"printify_order_id": "po_1", "printify_state": "in_production", "state": "sale"})

    def _event(self, kind):
        return {"id": "evt", "type": kind, "resource": {"id": "po_1", "type": "order", "data": {}}}

    def _shipped_order(self, delivered=False):
        return {"id": "po_1", "status": "fulfilled", "shipments": [
            {"carrier": "usps", "number": "9400111", "url": "https://t", "delivered_at": "2026-10-20" if delivered else None}
        ]}

    def test_bad_signature_401(self):
        body = json.dumps(self._event("order:updated")).encode()
        with mock.patch.dict(os.environ, ENV, clear=False):
            resp = self.url_open("/grove/api/v1/printify/webhook", data=body,
                                 headers={"X-Pfy-Signature": "sha256=bad", "Content-Type": "application/json",
                                          "X-Odoo-Database": get_db_name()})
        self.assertEqual(resp.status_code, 401)

    def test_good_signature_200(self):
        body = json.dumps(self._event("order:sent-to-production")).encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        with mock.patch.dict(os.environ, ENV, clear=False):
            resp = self.url_open("/grove/api/v1/printify/webhook", data=body,
                                 headers={"X-Pfy-Signature": sig, "Content-Type": "application/json",
                                          "X-Odoo-Database": get_db_name()})
        self.assertEqual(resp.status_code, 200)

    def test_shipment_created_writes_tracking_then_transit(self):
        with mock.patch.dict(os.environ, ENV, clear=False), \
                mock.patch.object(printify_client, "get_order", return_value=self._shipped_order()), \
                mock.patch.object(grove_main, "_apply_delivery_status", return_value=True) as apply:
            grove_main._handle_printify_event(self.env, self._event("order:shipment:created"))
        self.assertEqual(self.order.grove_tracking_numbers, "9400111")
        self.assertEqual(self.order.grove_shipping_carriers, "USPS")
        self.assertEqual(apply.call_args.args[2], "transit")
        self.assertEqual(apply.call_args.kwargs["source"], "printify")
        self.assertEqual(self.order.printify_state, "shipped")

    def test_delivered_then_late_shipment_never_regresses(self):
        with mock.patch.dict(os.environ, ENV, clear=False), \
                mock.patch.object(printify_client, "get_order", return_value=self._shipped_order(delivered=True)), \
                mock.patch.object(grove_main, "_notify_shipping_status"):
            grove_main._handle_printify_event(self.env, self._event("order:shipment:delivered"))
            grove_main._handle_printify_event(self.env, self._event("order:shipment:created"))
        self.assertEqual(self.order.grove_delivery_status, "delivered")
        self.assertEqual(self.order.printify_state, "delivered")

    def test_duplicate_shipment_event_emails_once(self):
        with mock.patch.dict(os.environ, ENV, clear=False), \
                mock.patch.object(printify_client, "get_order", return_value=self._shipped_order()), \
                mock.patch.object(grove_main, "_notify_shipping_status") as notify:
            grove_main._handle_printify_event(self.env, self._event("order:shipment:created"))
            grove_main._handle_printify_event(self.env, self._event("order:shipment:created"))
        self.assertEqual(notify.call_count, 1)

    def test_canceled_alerts(self):
        event = self._event("order:updated")
        event["resource"]["data"] = {"status": "canceled"}
        with mock.patch.dict(os.environ, ENV, clear=False), mock.patch.object(grove_main, "_notify_discord") as discord:
            grove_main._handle_printify_event(self.env, event)
        self.assertEqual(self.order.printify_state, "canceled")
        discord.assert_called_once()

    def test_unknown_order_ignored(self):
        event = self._event("order:updated")
        event["resource"]["id"] = "po_unknown"
        self.assertEqual(grove_main._handle_printify_event(self.env, event), "ignored")
```

- [ ] **Step 2: Run to verify failure**

Run `--test-tags='/grove_headless:TestPrintifyWebhook'`.
Expected: FAIL (route and handler missing).

- [ ] **Step 3: Implement**

Module level in `main.py`:

```python
PRINTIFY_SIGNATURE_HEADER = "X-Pfy-Signature"
_PRINTIFY_STATE_RANK = {"in_production": 1, "shipped": 2, "delivered": 3}


def _printify_set_state(order, state):
    """Never move a Printify state backwards (delivered stays delivered)."""
    current = _PRINTIFY_STATE_RANK.get(order.printify_state or "", 0)
    if _PRINTIFY_STATE_RANK.get(state, 0) >= current or state in ("canceled", "on_hold"):
        order.sudo().write({"printify_state": state})


def _handle_printify_event(env, event):
    kind = event.get("type") or ""
    resource = event.get("resource") or {}
    order = env["sale.order"].sudo().search([("printify_order_id", "=", str(resource.get("id") or ""))], limit=1)
    if not order:
        return "ignored"
    if kind == "order:sent-to-production":
        _printify_set_state(order, "in_production")
        return "in_production"
    if kind in ("order:shipment:created", "order:shipment:delivered"):
        token = os.environ.get("PRINTIFY_API_TOKEN", "")
        shop_id = os.environ.get("PRINTIFY_SHOP_ID", "")
        shipments = printify_client.get_order(token, shop_id, order.printify_order_id).get("shipments") or []
        numbers = [s.get("number") for s in shipments if s.get("number")]
        carriers = [(s.get("carrier") or "").upper() for s in shipments if s.get("number")]
        if numbers and order.grove_tracking_numbers != "\n".join(numbers):
            order.sudo().write({"grove_tracking_numbers": "\n".join(numbers), "grove_shipping_carriers": "\n".join(carriers)})
        status = "delivered" if kind == "order:shipment:delivered" else "transit"
        _apply_delivery_status(env, order, status, "\n".join(numbers), source="printify")
        _printify_set_state(order, "delivered" if status == "delivered" else "shipped")
        return status
    if kind == "order:updated":
        status = (resource.get("data") or {}).get("status") or ""
        if status in ("canceled", "on-hold"):
            state = "canceled" if status == "canceled" else "on_hold"
            _printify_set_state(order, state)
            _notify_discord(f":warning: Printify marked {order.name} {status}. Check the Printify dashboard.")
            return state
    return "ignored"
```

Route in `GroveHeadlessAPI` (next to `stripe_webhook`):

```python
    @http.route("/grove/api/v1/printify/webhook", type="http", auth="public", methods=["POST"], csrf=False)
    def printify_webhook(self, **_kwargs):
        raw = request.httprequest.get_data() or b""
        secret = os.environ.get("PRINTIFY_WEBHOOK_SECRET", "")
        signature = request.httprequest.headers.get(PRINTIFY_SIGNATURE_HEADER, "")
        if not printify_client.verify_signature(secret, raw, signature):
            return _json_response({"error": "invalid signature"}, status=401)
        try:
            event = json.loads(raw or b"{}")
        except ValueError:
            return _json_response({"error": "invalid JSON"}, status=400)
        try:
            action = _handle_printify_event(request.env, event)
        except printify_client.PrintifyError:
            _logger.warning("grove_headless: Printify webhook could not fetch order", exc_info=True)
            return _json_response({"error": "temporarily unavailable"}, status=503)
        return _json_response({"ok": True, "action": action})
```

A 503 makes Printify redeliver; the handler is idempotent (tracking write is guarded, `_apply_delivery_status` dedupes and ranks).

- [ ] **Step 4: Run tests to verify pass**

Run `--test-tags='/grove_headless:TestPrintifyWebhook'`, then the full suite.
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add grove_headless/controllers/main.py grove_headless/tests/test_printify_webhook.py grove_headless/tests/__init__.py conftest.py
git commit -m "feat(grove_headless): Printify webhook folds shipments into the shipment email path"
```

---

### Task 8: Keep Printify orders out of in-house fulfillment, back office, webhook registration

**Files:**
- Modify: `grove_headless/models/label_batch.py:224-245` (`_eligible_orders`)
- Modify: `grove_headless/models/sale_order.py` (`_poll_carrier_tracking` domain, ~1243)
- Modify: `grove_headless/views/fulfillment_views.xml:75-118` (Printify group on the sale order form)
- Modify: `grove_headless/data/grove_zone_filters.xml` (saved filter)
- Create: `scripts/printify_register_webhooks.py`
- Test: `grove_headless/tests/test_printify_exclusions.py` (Odoo runtime); `scripts/tests/test_printify_register_webhooks.py` (pure)

**Interfaces:**
- Consumes: `grove_is_printify` (Task 3), `printify_client.create_webhook` (Task 1).
- Produces: `scripts/printify_register_webhooks.py` with `register(token, shop_id, url, secret, create=...) -> list[dict]` and `TOPICS`.

- [ ] **Step 1: Write the failing exclusion tests**

```python
import os
from unittest import mock

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import carrier_tracking
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin
from .test_printify_checkout import _PrintifyFixture


@tagged("grove_headless", "post_install", "-at_install")
class TestPrintifyExclusions(_PrintifyFixture, GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self._build()
        with mock.patch.object(grove_main, "_printify_quote", return_value=4.75):
            self.order, _err = grove_main._create_draft_order(self._website(), self.env, self._payload("CA"))
        self.order.action_confirm()
        self.order.write({"grove_checkout_status": "paid"})

    def test_not_in_label_batch(self):
        batch = self.env["grove.label.batch"].create({"company_id": self.company.id})
        self.assertNotIn(self.order, batch._eligible_orders(self.company))

    def test_not_polled_by_carrier_cron(self):
        self.order.write({"grove_tracking_numbers": "9400111", "grove_shipping_carriers": "USPS"})
        clients = {"UPS": mock.Mock(), "USPS": mock.Mock()}
        with mock.patch.object(carrier_tracking, "build_clients", return_value=clients):
            self.env["sale.order"]._poll_carrier_tracking()
        clients["USPS"].track.assert_not_called()
```

Check `label_batch.py` for the model `_name` and required create fields (and `carrier_tracking` for the client method name the poll calls; `test_carrier_tracking_cron.py:83-85` shows the mock shape) and match them.

- [ ] **Step 2: Run to verify failure**

Run `--test-tags='/grove_headless:TestPrintifyExclusions'`.
Expected: FAIL (the shirt order is eligible / polled).

- [ ] **Step 3: Implement the exclusions**

`label_batch._eligible_orders` domain: add `("grove_is_printify", "=", False),` after `("grove_fulfillment", "!=", "pickup"),`.

`sale_order._poll_carrier_tracking` domain: add `("grove_is_printify", "=", False),` after `("grove_carrier_poll_stopped", "=", False),`.

- [ ] **Step 4: Run tests to verify pass**

Run `--test-tags='/grove_headless:TestPrintifyExclusions'` and the existing label-batch and carrier-tracking tests.
Expected: all pass.

- [ ] **Step 5: Back office view and filter**

In `fulfillment_views.xml`, inside `view_order_form_grove_fulfillment`, after the `grove_compliance_check` group xpath:

```xml
            <xpath expr="//field[@name='order_line']" position="after">
                <group string="Printify" name="grove_printify" invisible="not grove_is_printify">
                    <field name="grove_is_printify" invisible="1"/>
                    <field name="printify_order_id"/>
                    <field name="printify_state"/>
                    <field name="printify_push_attempts"/>
                    <button name="action_grove_retry_printify_push" type="object" string="Retry Printify push"
                            class="btn-danger btn-sm" icon="fa-refresh" invisible="printify_state != 'push_failed'"
                            groups="sales_team.group_sale_manager"/>
                </group>
            </xpath>
```

In `grove_zone_filters.xml`:

```xml
    <record id="ir_filter_grove_printify_push_failed" model="ir.filters">
        <field name="name">Printify push failed</field>
        <field name="model_id">sale.order</field>
        <field name="user_ids" eval="[(6, 0, [])]"/>
        <field name="domain">[("printify_state", "=", "push_failed")]</field>
        <field name="context">{}</field>
    </record>
```

Add `grove_fulfillment_route`, `printify_product_id` and `printify_lead_time` to the product form in `views/product_template_views.xml` (a "Printify" group near the `grove_gate_exempt` field), with `printify_lead_time` visible only when `grove_fulfillment_route == 'printify'`.

- [ ] **Step 6: Webhook registration script, failing test first** (`scripts/tests/test_printify_register_webhooks.py`)

```python
import importlib.util
import os
from unittest import mock

_PATH = os.path.join(os.path.dirname(__file__), "..", "printify_register_webhooks.py")
_spec = importlib.util.spec_from_file_location("printify_register_webhooks", _PATH)
reg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reg)


def test_registers_every_topic():
    create = mock.Mock(side_effect=lambda token, shop, topic, url, secret: {"topic": topic})
    out = reg.register("tok", "42", "https://odoo.example/grove/api/v1/printify/webhook", "s", create=create)
    assert [o["topic"] for o in out] == list(reg.TOPICS)
    assert set(reg.TOPICS) == {"order:sent-to-production", "order:shipment:created", "order:shipment:delivered", "order:updated"}
```

`scripts/printify_register_webhooks.py`:

```python
#!/usr/bin/env python3
"""Register the Printify webhooks for one environment (spec §7). No dashboard clicking.

Usage (Josh's terminal, secrets from 1Password):
  PRINTIFY_API_TOKEN=... PRINTIFY_SHOP_ID=... PRINTIFY_WEBHOOK_SECRET=... \
    python3 scripts/printify_register_webhooks.py https://odoo.gatheringatthegrove.com/grove/api/v1/printify/webhook
"""

import importlib.util
import os
import sys

_CLIENT = os.path.join(os.path.dirname(__file__), "..", "grove_headless", "models", "printify_client.py")
_spec = importlib.util.spec_from_file_location("printify_client", _CLIENT)
printify_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(printify_client)

TOPICS = ("order:sent-to-production", "order:shipment:created", "order:shipment:delivered", "order:updated")


def register(token, shop_id, url, secret, create=None):
    create = create or printify_client.create_webhook
    return [create(token, shop_id, topic, url, secret) for topic in TOPICS]


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    token, shop_id, secret = (os.environ.get(k, "") for k in ("PRINTIFY_API_TOKEN", "PRINTIFY_SHOP_ID", "PRINTIFY_WEBHOOK_SECRET"))
    if not (token and shop_id and secret):
        print("Set PRINTIFY_API_TOKEN, PRINTIFY_SHOP_ID and PRINTIFY_WEBHOOK_SECRET.")
        return 2
    for hook in register(token, shop_id, argv[1], secret):
        print("registered", hook.get("topic"), hook.get("id"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

Run: `python3 -m pytest scripts/tests/test_printify_register_webhooks.py -v` → FAIL before the script exists, PASS after.

- [ ] **Step 7: Full verification**

Run, in order:
- `ruff check . --select E,F,I --line-length 120 && ruff format --check`
- `python3 -m pytest grove_headless/tests/ scripts/tests/ -v`
- the CI Odoo command with `--init=grove_headless,grove_support --test-tags='/grove_headless,/grove_support'`

Expected: all green.

- [ ] **Step 8: Commit and open the PR**

```bash
git add grove_headless/models/label_batch.py grove_headless/models/sale_order.py grove_headless/views/fulfillment_views.xml grove_headless/views/product_template_views.xml grove_headless/data/grove_zone_filters.xml scripts/printify_register_webhooks.py scripts/tests/test_printify_register_webhooks.py grove_headless/tests/test_printify_exclusions.py grove_headless/tests/__init__.py conftest.py
git commit -m "feat(grove_headless): keep Printify orders out of label batch and carrier poll; back office; webhook registration script"
```

PR title: `feat(grove_headless): Printify T-shirts on the farm storefront (Train #3)`. Body links the spec and this plan and lists the QA e2e gate below.

---

## QA gate (Train #3, after both PRs merge)

1. QA env vars: `PRINTIFY_API_TOKEN`, `PRINTIFY_SHOP_ID`, `PRINTIFY_WEBHOOK_SECRET`, `PRINTIFY_DRY_RUN=1`, `GROVE_STRIPE_TAX_GOLDBERRY=1`.
2. Run "Sync from Printify"; publish one shirt.
3. `python3 scripts/printify_register_webhooks.py https://<qa-odoo>/grove/api/v1/printify/webhook`.
4. Buy a shirt on the QA farm site with a Stripe test card, shipping to CA.
5. Expect: the Printify dashboard shows an order with external id = the SO name, NOT in production; the SO shows `printify_state = created`; it is absent from a new label batch.
6. POST a signed `order:shipment:created` test event (or use Printify's test delivery) → SO shipped, tracking set, shipment email sent once.

## Prod setup (after promote, Josh)

Env vars without `PRINTIFY_DRY_RUN`; farm Stripe Tax setup with `goldberry_stripe_key` (Grove Prod / `stripe`), first call confirms the key can write tax settings; sync, price, publish; register webhooks with the prod URL; one real order to Josh.
