"""Stripe gateway helpers — pure Python, no Odoo imports, so they unit-test
without a DB (mirrors shippo_client). Every network call takes an injectable
`post` callable (default `requests.post`) so tests mock Stripe without hitting
the live API.

We talk to Stripe's REST API directly with `requests` rather than pulling in
the `stripe` SDK: it keeps the Odoo Docker image dependency-free (Terra's
domain), mirrors core `payment_stripe` (which also uses raw HTTP), and makes
signature verification something we control byte-for-byte.

Keys are read by the caller from the server environment
(`stripe_test_secret_key` / `stripe_test_webhook_secret`) and passed in — this
module never touches os.environ, which keeps it trivially testable and lets the
endpoints tolerate absent keys at build/test time.
"""

import hashlib
import hmac
import time

import requests

STRIPE_API_BASE = "https://api.stripe.com"
CURRENCY = "usd"
DEFAULT_TIMEOUT = 30

# Charging matrix (issue GOL-642): a line that cannot be filled from on-hand
# stock is a preorder and is charged a flat deposit now; the balance is taken
# off-session at ship time (setup_future_usage=off_session on the session).
PREORDER_DEPOSIT = 10.00  # USD, flat, per preorder line

# Reject webhook events whose signed timestamp is more than this many seconds
# from now — Stripe's recommended default, blunts replay of a captured payload.
SIG_TOLERANCE = 300


class StripeError(Exception):
    """Raised on any non-2xx Stripe API response or malformed webhook."""


def to_cents(amount) -> int:
    """USD dollars (float/Decimal/str) -> integer cents, half-up rounded.

    Stripe amounts are integer minor units; float dollar math (e.g. 19.99 * 100
    == 1998.9999) must be rounded, never truncated, or every price is a cent low.
    """
    return int(round(float(amount) * 100))


# ── Checkout Session ────────────────────────────────────────────────────────


def line_charge(unit_price, quantity, qty_available, deposit=PREORDER_DEPOSIT):
    """Resolve one product line to a Stripe amount under the charging matrix.

    Returns (amount_cents, quantity, is_preorder):
      * in stock  (qty_available covers the requested quantity) -> full price
        for the full quantity;
      * preorder  (stock cannot cover it, incl. qty_available None/unknown) ->
        a single flat deposit line (quantity collapses to 1), balance later.
    """
    if qty_available is not None and qty_available >= quantity:
        return (to_cents(unit_price), int(quantity), False)
    return (to_cents(deposit), 1, True)


def _flatten(prefix, value, out):
    """Flatten a nested dict/list into Stripe's bracketed form-encoding pairs.

    line_items -> line_items[0][price_data][unit_amount]=1999 etc. `requests`
    only form-encodes flat dicts, so we do the nesting ourselves.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}[{k}]" if prefix else str(k), v, out)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _flatten(f"{prefix}[{i}]", v, out)
    elif isinstance(value, bool):
        out[prefix] = "true" if value else "false"
    elif value is not None:
        out[prefix] = value
    return out


def build_session_params(
    *,
    line_items,
    success_url,
    cancel_url,
    metadata=None,
    customer_email=None,
    setup_future_usage=False,
):
    """Build the flat form params for POST /v1/checkout/sessions.

    `line_items` is a list of {"name": str, "amount_cents": int, "quantity": int}
    already resolved through the charging matrix. Stripe Tax is OFF — tax rides
    in as its own explicit line item built by the caller from Odoo's amount_tax.
    """
    nested = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "line_items": [
            {
                "price_data": {
                    "currency": CURRENCY,
                    "unit_amount": int(li["amount_cents"]),
                    "product_data": {"name": li["name"]},
                },
                "quantity": int(li.get("quantity", 1)),
            }
            for li in line_items
        ],
    }
    if customer_email:
        nested["customer_email"] = customer_email
    if metadata:
        nested["metadata"] = metadata
    if setup_future_usage:
        # Save the payment method so the preorder balance can be charged
        # off-session when the plant actually ships.
        nested["payment_intent_data"] = {"setup_future_usage": "off_session"}
    return _flatten("", nested, {})


def create_checkout_session(
    secret_key,
    *,
    line_items,
    success_url,
    cancel_url,
    metadata=None,
    customer_email=None,
    setup_future_usage=False,
    post=requests.post,
    timeout=DEFAULT_TIMEOUT,
):
    """Create a Stripe Checkout Session. Returns the parsed session dict
    (has `id`, `url`, `payment_intent`). Raises StripeError on any non-2xx."""
    if not secret_key:
        raise StripeError("Stripe secret key is not configured")
    if not line_items:
        raise StripeError("cannot create a checkout session with no line items")
    params = build_session_params(
        line_items=line_items,
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=metadata,
        customer_email=customer_email,
        setup_future_usage=setup_future_usage,
    )
    resp = post(
        f"{STRIPE_API_BASE}/v1/checkout/sessions",
        data=params,
        auth=(secret_key, ""),
        timeout=timeout,
    )
    return _parse(resp, "checkout session")


def create_refund(
    secret_key, payment_intent, *, reason=None, metadata=None, post=requests.post, timeout=DEFAULT_TIMEOUT
):
    """Refund a payment intent in full. Returns the parsed refund dict.

    `reason` must be one of Stripe's enum values (duplicate | fraudulent |
    requested_by_customer) or None. Raises StripeError on any non-2xx."""
    if not secret_key:
        raise StripeError("Stripe secret key is not configured")
    if not payment_intent:
        raise StripeError("cannot refund without a payment_intent")
    nested = {"payment_intent": payment_intent}
    if reason:
        nested["reason"] = reason
    if metadata:
        nested["metadata"] = metadata
    resp = post(
        f"{STRIPE_API_BASE}/v1/refunds",
        data=_flatten("", nested, {}),
        auth=(secret_key, ""),
        timeout=timeout,
    )
    return _parse(resp, "refund")


def _parse(resp, what):
    """Turn a Stripe HTTP response into a dict or a StripeError."""
    status = getattr(resp, "status_code", 0)
    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 — any decode failure is a gateway error
        raise StripeError(f"Stripe {what}: unparseable response (HTTP {status})") from exc
    if status < 200 or status >= 300:
        message = (body or {}).get("error", {}).get("message", f"HTTP {status}")
        raise StripeError(f"Stripe {what} failed: {message}")
    return body


# ── Webhook signature ───────────────────────────────────────────────────────


def verify_webhook_signature(payload, sig_header, secret, tolerance=SIG_TOLERANCE, now=None):
    """Verify a `Stripe-Signature` header against the raw request body.

    Returns True on success; raises StripeError on any failure. `payload` is the
    raw body (bytes or str) — it MUST be the exact bytes Stripe signed, so the
    caller reads it before any JSON round-trip. Implements Stripe's scheme:
    signed_payload = "{t}.{body}", HMAC-SHA256 with the endpoint secret, compared
    constant-time against any provided v1 signature, with a timestamp tolerance.
    """
    if not secret:
        raise StripeError("webhook secret is not configured")
    if not sig_header:
        raise StripeError("missing Stripe-Signature header")
    if isinstance(payload, str):
        payload = payload.encode("utf-8")

    parts = {}
    for item in sig_header.split(","):
        key, _, val = item.partition("=")
        if val:
            parts.setdefault(key.strip(), []).append(val.strip())
    timestamps = parts.get("t", [])
    signatures = parts.get("v1", [])
    if not timestamps or not signatures:
        raise StripeError("signature header missing t or v1")
    try:
        ts = int(timestamps[0])
    except ValueError as exc:
        raise StripeError("signature header has a non-integer timestamp") from exc

    if now is None:
        now = time.time()
    if tolerance and abs(now - ts) > tolerance:
        raise StripeError("webhook timestamp is outside the tolerance window")

    signed = f"{timestamps[0]}.".encode("utf-8") + payload
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise StripeError("webhook signature mismatch")
    return True
