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
        body = _call(
            get,
            f"{BASE_URL}/shops/{shop_id}/products.json",
            token,
            DEFAULT_TIMEOUT,
            params={"page": page, "limit": 50},
        )
        products.extend(body.get("data") or [])
        if page >= int(body.get("last_page") or 1):
            return products
        page += 1


def calculate_shipping(token, shop_id, line_items, address, post=requests.post):
    body = _call(
        post,
        f"{BASE_URL}/shops/{shop_id}/orders/shipping.json",
        token,
        QUOTE_TIMEOUT,
        json={"line_items": line_items, "address_to": address},
    )
    cents = body.get("standard")
    if cents is None:
        raise PrintifyError("Printify shipping quote had no standard rate")
    return round(int(cents) / 100.0, 2)


def create_order(token, shop_id, payload, post=requests.post):
    return str(_call(post, f"{BASE_URL}/shops/{shop_id}/orders.json", token, DEFAULT_TIMEOUT, json=payload)["id"])


def get_order(token, shop_id, order_id, get=requests.get):
    return _call(get, f"{BASE_URL}/shops/{shop_id}/orders/{order_id}.json", token, DEFAULT_TIMEOUT)


def find_order_by_external_id(token, shop_id, external_id, get=requests.get, max_pages=50):
    """Find a shop order by its external_id (the sale-order name, used for push
    idempotency). Printify's order list has no external_id filter, so scan pages
    newest-first until the order is found or the list is exhausted — never only a
    fixed head of recent orders, or a push retry could miss an already-created
    order and duplicate it (GOL-3314 review 2026-10-14). limit=50 keeps the page
    count low; max_pages caps a runaway scan on a very large shop."""
    page = 1
    while page <= max_pages:
        body = _call(
            get,
            f"{BASE_URL}/shops/{shop_id}/orders.json",
            token,
            DEFAULT_TIMEOUT,
            params={"page": page, "limit": 50},
        )
        for order in body.get("data") or []:
            if order.get("external_id") == external_id:
                return order
        if page >= int(body.get("last_page") or 1):
            return None
        page += 1
    return None


def send_to_production(token, shop_id, order_id, post=requests.post):
    _call(post, f"{BASE_URL}/shops/{shop_id}/orders/{order_id}/send_to_production.json", token, DEFAULT_TIMEOUT)


def create_webhook(token, shop_id, topic, url, secret, post=requests.post):
    return _call(
        post,
        f"{BASE_URL}/shops/{shop_id}/webhooks.json",
        token,
        DEFAULT_TIMEOUT,
        json={"topic": topic, "url": url, "secret": secret},
    )


def verify_signature(secret, body, header):
    """HMAC-SHA256 of the raw body. Accepts ``sha256=<hex>`` or bare hex; fails closed.
    Confirm the header name (``X-Pfy-Signature``) against developers.printify.com in Task 7."""
    if not secret or not header:
        return False
    provided = header.split("=", 1)[1] if header.startswith("sha256=") else header
    expected = hmac.new(secret.encode(), body or b"", hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided.strip(), expected)
