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


class StripeCardError(StripeError):
    """An off-session charge that Stripe declined at the card (HTTP 402).

    Carries the machine-readable decline detail so the caller can tell a
    recoverable card decline (dun the customer, retry the settlement) apart from
    a transport/config error (StripeError). ``payment_intent`` is the id Stripe
    returns on the failed intent so a later retry can reference the same object.
    """

    def __init__(self, message, *, code=None, decline_code=None, payment_intent=None):
        super().__init__(message)
        self.code = code
        self.decline_code = decline_code
        self.payment_intent = payment_intent


def to_cents(amount) -> int:
    """USD dollars (float/Decimal/str) -> integer cents, half-up rounded.

    Stripe amounts are integer minor units; float dollar math (e.g. 19.99 * 100
    == 1998.9999) must be rounded, never truncated, or every price is a cent low.
    """
    return int(round(float(amount) * 100))


# ── Checkout Session ────────────────────────────────────────────────────────


def line_charge(unit_price, quantity, free_available, deposit=PREORDER_DEPOSIT, ships_now=True):
    """Resolve one product line into Stripe sub-charges under the charging matrix.

    Returns a list of ``(amount_cents, quantity, is_preorder)`` tuples so a
    partially-stocked line SPLITS instead of collapsing to a single flat
    deposit (GOL-1036 defect 3): the units we can fill from free stock are
    billed at full price now, and each unit of the shortfall is a preorder
    charged the flat deposit PER UNIT (balance captured off-session at ship).

      * fully in stock, in window  -> [(full price, quantity, False)]
      * fully short/unknown stock -> [(deposit, quantity, True)]  (per unit)
      * partially in stock (0 < free < quantity) ->
            [(full price, free, False), (deposit, quantity - free, True)]
      * cannot ship now (``ships_now`` False) -> [(deposit, quantity, True)]

    Stock is measured as *free* quantity (on-hand minus already-reserved),
    passed in by the caller (GOL-1036 defect 4) — a unit another order has
    reserved is not sellable now and must fall to the preorder side, or the
    same tree is billed to two customers. ``free_available`` None/negative is
    treated as zero free stock (unknown -> preorder), never as "in stock".

    ``ships_now`` closes the calendar-window gap (GOL-1666 §1): a line that
    cannot ship now — a bareroot tree inside a dormant preorder window — is a
    preorder for its WHOLE quantity even when stock is on hand, because the
    tree is in the ground and can't be lifted until its wave. On-hand units
    still reserve with the flat deposit and settle off-session at ship, which
    matches the product page's deposit-now promise (previously such a line
    charged 100% at checkout and contradicted the page). The caller decides
    which lines honor the window — only bareroot passes ``ships_now`` through;
    potted is pickup-only and keeps its stock-driven behaviour.
    """
    qty = int(quantity)
    free = 0 if free_available is None else int(free_available)
    in_stock = max(0, min(free, qty)) if ships_now else 0
    charges = []
    if in_stock > 0:
        charges.append((to_cents(unit_price), in_stock, False))
    reserve = qty - in_stock
    if reserve > 0:
        charges.append((to_cents(deposit), reserve, True))
    return charges


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
    discount_coupon_id=None,
):
    """Build the flat form params for POST /v1/checkout/sessions.

    `line_items` is a list of {"name": str, "amount_cents": int, "quantity": int}
    already resolved through the charging matrix — all POSITIVE. Stripe Tax is
    OFF; tax rides in as its own explicit line item built by the caller from
    Odoo's amount_tax. A promo discount cannot be a negative line item (Stripe
    rejects a negative `unit_amount`); it is applied via `discount_coupon_id` —
    an existing one-time coupon id — which Stripe subtracts from the total
    (GOL-2088).
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
    if discount_coupon_id:
        nested["discounts"] = [{"coupon": discount_coupon_id}]
    if customer_email:
        nested["customer_email"] = customer_email
    if metadata:
        nested["metadata"] = metadata
    if setup_future_usage:
        # Save the payment method so the preorder balance can be charged
        # off-session when the plant actually ships.
        nested["payment_intent_data"] = {"setup_future_usage": "off_session"}
    return _flatten("", nested, {})


def create_coupon(secret_key, *, amount_off_cents, name=None, post=requests.post, timeout=DEFAULT_TIMEOUT):
    """Create a one-time Stripe coupon worth `amount_off_cents` (minor units) in
    CURRENCY. Used to represent a storefront promo discount on a Checkout
    Session, which cannot itself carry a negative line item (GOL-2088). Returns
    the parsed coupon dict (has `id`). Raises StripeError on a non-positive
    amount, a missing key, or any non-2xx response."""
    if not secret_key:
        raise StripeError("Stripe secret key is not configured")
    amount_off_cents = int(amount_off_cents)
    if amount_off_cents <= 0:
        raise StripeError("coupon amount_off must be positive")
    nested = {
        "amount_off": amount_off_cents,
        "currency": CURRENCY,
        "duration": "once",
        "max_redemptions": 1,
    }
    if name:
        nested["name"] = name
    resp = post(
        f"{STRIPE_API_BASE}/v1/coupons",
        data=_flatten("", nested, {}),
        auth=(secret_key, ""),
        timeout=timeout,
    )
    return _parse(resp, "coupon")


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
    (has `id`, `url`, `payment_intent`). Raises StripeError on any non-2xx.

    A promo discount is passed in as one or more NEGATIVE-amount entries in
    `line_items` (kind "discount"). Stripe Checkout can't take a negative
    `unit_amount`, so those are summed into a one-time coupon (`create_coupon`)
    and applied via the session's `discounts` — the positive lines alone become
    Stripe line items. `charged_cents` at the call site already nets the
    negative, so the total Stripe collects (positives − coupon) matches
    (GOL-2088)."""
    if not secret_key:
        raise StripeError("Stripe secret key is not configured")
    if not line_items:
        raise StripeError("cannot create a checkout session with no line items")
    positives = [li for li in line_items if int(li["amount_cents"]) > 0]
    if not positives:
        raise StripeError("cannot create a checkout session with no chargeable line items")
    discount_cents = -sum(
        int(li["amount_cents"]) * int(li.get("quantity", 1)) for li in line_items if int(li["amount_cents"]) < 0
    )
    coupon_id = None
    if discount_cents > 0:
        coupon = create_coupon(
            secret_key, amount_off_cents=discount_cents, name="Promo discount", post=post, timeout=timeout
        )
        coupon_id = coupon.get("id")
    params = build_session_params(
        line_items=positives,
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=metadata,
        customer_email=customer_email,
        setup_future_usage=setup_future_usage,
        discount_coupon_id=coupon_id,
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


def create_payment_intent(
    secret_key,
    *,
    amount_cents,
    customer,
    payment_method,
    metadata=None,
    idempotency_key=None,
    description=None,
    post=requests.post,
    timeout=DEFAULT_TIMEOUT,
):
    """Charge a saved card off-session (GOL-2052 ship-time settlement).

    Creates and confirms a PaymentIntent for ``amount_cents`` against the
    ``customer``/``payment_method`` saved at checkout via
    ``setup_future_usage=off_session``. Returns the parsed intent dict on a
    successful capture.

    Raises:
      * ``StripeCardError`` when Stripe declines the card (HTTP 402) — the
        recoverable case: the caller flags the order shipped-but-unsettled and
        duns the customer. The decline ``code``/``decline_code`` and the failed
        ``payment_intent`` id are attached for the retry path.
      * ``StripeError`` on any other non-2xx (bad key, network, config).

    ``idempotency_key`` is sent as Stripe's ``Idempotency-Key`` header so a
    retried settlement never double-charges: replaying the same key returns the
    original intent instead of creating a second charge.
    """
    if not secret_key:
        raise StripeError("Stripe secret key is not configured")
    if not customer or not payment_method:
        raise StripeError("off-session charge requires a saved customer and payment_method")
    amount_cents = int(amount_cents)
    if amount_cents <= 0:
        raise StripeError("off-session charge amount must be positive")
    nested = {
        "amount": amount_cents,
        "currency": CURRENCY,
        "customer": customer,
        "payment_method": payment_method,
        "off_session": True,
        "confirm": True,
    }
    if description:
        nested["description"] = description
    if metadata:
        nested["metadata"] = metadata
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    resp = post(
        f"{STRIPE_API_BASE}/v1/payment_intents",
        data=_flatten("", nested, {}),
        auth=(secret_key, ""),
        headers=headers,
        timeout=timeout,
    )
    return _parse(resp, "payment intent")


def _parse(resp, what):
    """Turn a Stripe HTTP response into a dict or a StripeError.

    A card decline surfaces as HTTP 402 with ``error.code`` set (Stripe's
    standard shape); it is raised as ``StripeCardError`` so the off-session
    settlement path can treat it as recoverable, distinct from a plain
    ``StripeError`` for every other failure."""
    status = getattr(resp, "status_code", 0)
    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 — any decode failure is a gateway error
        raise StripeError(f"Stripe {what}: unparseable response (HTTP {status})") from exc
    if status < 200 or status >= 300:
        error = (body or {}).get("error", {}) or {}
        message = error.get("message", f"HTTP {status}")
        # A declined card (typically HTTP 402, error.type card_error) is
        # recoverable: raise the card-specific error so an off-session
        # settlement can dun-and-retry rather than treat it as a hard failure.
        if status == 402 or error.get("type") == "card_error":
            pi = error.get("payment_intent") or {}
            raise StripeCardError(
                f"Stripe {what} declined: {message}",
                code=error.get("code"),
                decline_code=error.get("decline_code"),
                payment_intent=pi.get("id") if isinstance(pi, dict) else pi,
            )
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
