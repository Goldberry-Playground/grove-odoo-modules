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

# Stripe product tax codes (GOL-2568, Josh ruling 2026-09-29). The nursery
# Stripe account's default is txcd_99999999 (General — Tangible Goods); we stamp
# it explicitly on every goods line so the intent is legible in Stripe's
# reports, tag the shipping line with the Shipping code so Stripe applies each
# state's shipping-taxability rule, and tag gift cards with the gift-card code
# (gift cards are non-taxable at sale; tax lands when the card is redeemed).
TAX_CODE_GOODS = "txcd_99999999"  # General — Tangible Goods
TAX_CODE_SHIPPING = "txcd_92010001"  # Shipping
TAX_CODE_GIFT_CARD = "txcd_10401000"  # Gift card (non-taxable at sale)

# Deposit rule (GOL-2233, ratified by Josh in the 2026-09-07 release-train
# session): an order that triggers a deposit — sold-out bareroot OR any order
# placed after the season cutover (default Oct 15) — is charged ONE flat $10
# deposit for the WHOLE order, regardless of cart contents or quantity (100
# trees = $10 for the same order). The balance (goods + real shipping +
# recomputed tax) settles off-session at ship time
# (setup_future_usage=off_session on the session). Supersedes the earlier
# per-line / per-unit deposit split (GOL-642 / GOL-1036 / GOL-1666).
PREORDER_DEPOSIT = 10.00  # USD, flat, per ORDER (not per line / per unit)

# Reject webhook events whose signed timestamp is more than this many seconds
# from now — Stripe's recommended default, blunts replay of a captured payload.
SIG_TOLERANCE = 300


class StripeError(Exception):
    """Raised on any non-2xx Stripe API response, transport failure, or malformed
    webhook.

    Carries the HTTP status and Stripe ``error.type`` (when there was a response)
    so a caller can classify the failure (GOL-3011): a 4xx ``invalid_request`` /
    ``authentication`` error means Stripe rejected the request BEFORE charging (no
    money moved → safe to retry with a new key), while a timeout / connection
    reset / 5xx / rate-limit leaves the outcome UNKNOWN (the charge may have
    succeeded → must reconcile before any retry). ``http_status`` is ``None`` for a
    transport error (no response was received)."""

    def __init__(self, message, *, http_status=None, error_type=None):
        super().__init__(message)
        self.http_status = http_status
        self.error_type = error_type


class StripeCardError(StripeError):
    """An off-session charge that Stripe declined at the card (HTTP 402).

    Carries the machine-readable decline detail so the caller can tell a
    recoverable card decline (dun the customer, retry the settlement) apart from
    a transport/config error (StripeError). ``payment_intent`` is the id Stripe
    returns on the failed intent so a later retry can reference the same object.
    """

    def __init__(self, message, *, code=None, decline_code=None, payment_intent=None):
        super().__init__(message, http_status=402, error_type="card_error")
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


def _price_data(li, *, tax_enabled):
    """price_data for one Stripe line item.

    When Stripe Tax is on (GOL-2568) prices stay tax-EXCLUSIVE and every line
    carries a Stripe tax code (`product_data[tax_code]`) so Stripe applies the
    right destination rule per line — goods vs shipping vs gift card. When Tax is
    off (the deposit path, where tax is deferred to ship-time settlement) the
    tax_code/tax_behavior are omitted so the flat deposit is charged verbatim.
    """
    product_data = {"name": li["name"]}
    if tax_enabled and li.get("tax_code"):
        product_data["tax_code"] = li["tax_code"]
    price_data = {
        "currency": CURRENCY,
        "unit_amount": int(li["amount_cents"]),
        "product_data": product_data,
    }
    if tax_enabled:
        # Grove prices are entered tax-exclusive; Stripe adds destination tax on
        # top rather than backing it out of the shown price (Josh 2026-09-29).
        price_data["tax_behavior"] = "exclusive"
    return price_data


def build_session_params(
    *,
    line_items,
    success_url,
    cancel_url,
    metadata=None,
    customer_email=None,
    customer=None,
    automatic_tax=False,
    setup_future_usage=False,
    discount_coupon_id=None,
):
    """Build the flat form params for POST /v1/checkout/sessions.

    `line_items` is a list of {"name": str, "amount_cents": int, "quantity": int}
    (optionally "tax_code") already resolved through the charging matrix — all
    POSITIVE. A promo discount cannot be a negative line item (Stripe rejects a
    negative `unit_amount`); it is applied via `discount_coupon_id` — an existing
    one-time coupon id — which Stripe subtracts from the total (GOL-2088).

    Sales-tax handling (GOL-2568, Josh ruling 2026-09-29): when
    ``automatic_tax`` is True, Stripe Tax computes destination tax on the session
    (``automatic_tax[enabled]=true``) and we NO LONGER pass a "Sales tax" line —
    each goods/shipping line carries its own tax code and stays tax-exclusive.
    The ship-to lives on a Stripe Customer we build from the order's address, so
    ``customer`` is passed with ``customer_update[shipping]=auto`` (never
    ``shipping_address_collection`` — no double entry). ``customer`` and
    ``customer_email`` are mutually exclusive in Stripe, so ``customer`` wins
    when both are given.
    """
    nested = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "line_items": [
            {
                "price_data": _price_data(li, tax_enabled=automatic_tax),
                "quantity": int(li.get("quantity", 1)),
            }
            for li in line_items
        ],
    }
    if automatic_tax:
        nested["automatic_tax"] = {"enabled": True}
    if discount_coupon_id:
        nested["discounts"] = [{"coupon": discount_coupon_id}]
    if customer:
        # A Customer carrying the ship-to address is how Stripe Tax learns the
        # destination without a second on-page address entry. customer_update
        # [shipping]=auto lets the session reconcile/save that shipping address.
        nested["customer"] = customer
        nested["customer_update"] = {"shipping": "auto"}
    elif customer_email:
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
    customer=None,
    automatic_tax=False,
    setup_future_usage=False,
    post=requests.post,
    timeout=DEFAULT_TIMEOUT,
):
    """Create a Stripe Checkout Session. Returns the parsed session dict
    (has `id`, `url`, `payment_intent`). Raises StripeError on any non-2xx.

    ``customer`` + ``automatic_tax`` enable Stripe Tax on the session (GOL-2568):
    Stripe computes destination tax from the Customer's shipping address instead
    of an explicit "Sales tax" line item. See ``build_session_params``.

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
        customer=customer,
        automatic_tax=automatic_tax,
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
        # Without an explicit allow-list Stripe defaults a PaymentIntent to card
        # only on this account's (2017-01-27) API version, so a saved Stripe Link
        # method is refused ("The PaymentMethod provided (link) is not allowed for
        # this PaymentIntent"). Checkout saves whichever reusable method the
        # shopper picked (card, Link, ...); accept any enabled type that needs no
        # redirect, since an off-session charge has no shopper to redirect.
        "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
    }
    if description:
        nested["description"] = description
    if metadata:
        nested["metadata"] = metadata
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    try:
        resp = post(
            f"{STRIPE_API_BASE}/v1/payment_intents",
            data=_flatten("", nested, {}),
            auth=(secret_key, ""),
            headers=headers,
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        # Timeout / connection reset / DNS failure: the request may or may not
        # have reached Stripe, so the charge OUTCOME IS UNKNOWN (GOL-3011).
        # http_status=None marks the reconcile-before-retry bucket — the caller
        # must never blindly re-charge, as the first attempt may have succeeded.
        raise StripeError(
            f"Stripe payment intent: no response ({type(exc).__name__}: {exc})",
            http_status=None,
            error_type="connection_error",
        ) from exc
    return _parse(resp, "payment intent")


def retrieve_payment_intent(secret_key, payment_intent_id, *, get=requests.get, timeout=DEFAULT_TIMEOUT):
    """Fetch a PaymentIntent by id. Returns the parsed intent dict.

    The ship-time settlement (GOL-2053) reads back the DEPOSIT intent saved at
    checkout to recover the ``customer`` and ``payment_method`` that
    ``setup_future_usage=off_session`` attached — those are what the off-session
    balance charge (``create_payment_intent``) needs, and the checkout webhook
    only persisted the intent id. Raises StripeError on any non-2xx."""
    if not secret_key:
        raise StripeError("Stripe secret key is not configured")
    if not payment_intent_id:
        raise StripeError("cannot retrieve a payment intent without an id")
    resp = get(
        f"{STRIPE_API_BASE}/v1/payment_intents/{payment_intent_id}",
        auth=(secret_key, ""),
        timeout=timeout,
    )
    return _parse(resp, "payment intent")


def search_payment_intents(secret_key, query, *, get=requests.get, timeout=DEFAULT_TIMEOUT):
    """Search PaymentIntents with a Stripe Search query; returns the ``data`` list.

    The reconciliation read for an outcome-UNKNOWN settlement (GOL-3011): after a
    timeout / 5xx we cannot know whether the off-session charge actually landed,
    so before any retry we look up the intents Stripe holds for this order
    (``metadata['order_ref']:'S00357' AND metadata['purpose']:'ship_settlement'``)
    and only re-charge when none already succeeded. Search has no 24h TTL (unlike
    idempotency replay), so it stays authoritative past the cron's daily cadence;
    it is eventually-consistent (a just-created intent can lag ~1 min), which the
    stable idempotency key covers for the fresh-charge case. Raises StripeError on
    any non-2xx; a transport failure propagates so the caller leaves the order
    parked as still-reconciling rather than charging blind."""
    if not secret_key:
        raise StripeError("Stripe secret key is not configured")
    resp = get(
        f"{STRIPE_API_BASE}/v1/payment_intents/search",
        params={"query": query},
        auth=(secret_key, ""),
        timeout=timeout,
    )
    return (_parse(resp, "payment intent search") or {}).get("data", []) or []


# ── Customer (Stripe Tax address carrier) ───────────────────────────────────
#
# Stripe Tax needs the ship-to address to compute destination tax. Rather than
# turn on Checkout's shipping_address_collection (which would make the shopper
# re-type an address they already gave us), Josh's route (2026-09-29) is to put
# the checkout form's ship-to on a Stripe Customer and pass that customer to the
# session. Pickup orders carry the farm address (WV), matching Odoo's WV-nexus
# rule. We reuse a Customer by email so a repeat buyer doesn't accumulate one
# per order, refreshing its shipping to the address on THIS order.


def find_customer_by_email(secret_key, email, *, get=requests.get, timeout=DEFAULT_TIMEOUT):
    """Return the most recent existing Stripe Customer with ``email``, or None.

    Stripe does not dedupe customers by email, so ``ensure_customer`` uses this
    to reuse one we already made rather than pile up a customer per checkout.
    Raises StripeError on a missing key or non-2xx."""
    if not secret_key:
        raise StripeError("Stripe secret key is not configured")
    if not email:
        return None
    resp = get(
        f"{STRIPE_API_BASE}/v1/customers",
        params={"email": email, "limit": 1},
        auth=(secret_key, ""),
        timeout=timeout,
    )
    data = _parse(resp, "customer lookup")
    items = data.get("data") or []
    return items[0] if items else None


def _customer_params(*, email, name, shipping):
    nested = {}
    if email:
        nested["email"] = email
    if name:
        nested["name"] = name
    if shipping:
        # shipping = {"name": str, "address": {"line1","line2","city","state",
        # "postal_code","country"}} — Stripe Tax reads customer.shipping.address.
        nested["shipping"] = shipping
    return nested


def create_customer(secret_key, *, email=None, name=None, shipping=None, post=requests.post, timeout=DEFAULT_TIMEOUT):
    """Create a Stripe Customer carrying the ship-to as shipping[name]+[address].
    Returns the parsed customer dict (has `id`). Raises StripeError on non-2xx."""
    if not secret_key:
        raise StripeError("Stripe secret key is not configured")
    resp = post(
        f"{STRIPE_API_BASE}/v1/customers",
        data=_flatten("", _customer_params(email=email, name=name, shipping=shipping), {}),
        auth=(secret_key, ""),
        timeout=timeout,
    )
    return _parse(resp, "customer")


def update_customer(secret_key, customer_id, *, name=None, shipping=None, post=requests.post, timeout=DEFAULT_TIMEOUT):
    """Refresh an existing Customer's name/shipping. Returns the parsed dict."""
    if not secret_key:
        raise StripeError("Stripe secret key is not configured")
    if not customer_id:
        raise StripeError("cannot update a customer without an id")
    resp = post(
        f"{STRIPE_API_BASE}/v1/customers/{customer_id}",
        data=_flatten("", _customer_params(email=None, name=name, shipping=shipping), {}),
        auth=(secret_key, ""),
        timeout=timeout,
    )
    return _parse(resp, "customer")


def ensure_customer(
    secret_key, *, email, name=None, shipping=None, post=requests.post, get=requests.get, timeout=DEFAULT_TIMEOUT
):
    """Reuse the Customer for ``email`` (refreshing shipping) or create one.
    Returns the parsed customer dict. Raises StripeError on non-2xx."""
    existing = find_customer_by_email(secret_key, email, get=get, timeout=timeout) if email else None
    if existing and existing.get("id"):
        return update_customer(secret_key, existing["id"], name=name, shipping=shipping, post=post, timeout=timeout)
    return create_customer(secret_key, email=email, name=name, shipping=shipping, post=post, timeout=timeout)


# ── Stripe Tax calculation (ship-time settlement) ───────────────────────────
#
# The Checkout Session computes tax itself, but the off-session balance capture
# at ship (GOL-2233/2053) is a raw PaymentIntent with no session, so it has to
# ask Stripe Tax for the number directly: POST /v1/tax/calculations with the
# same line items + address, charge the returned tax, then record a
# tax/transaction from the calculation so Stripe's tax reports include it.


def create_tax_calculation(
    secret_key,
    *,
    line_items,
    address,
    address_source="shipping",
    customer=None,
    post=requests.post,
    timeout=DEFAULT_TIMEOUT,
):
    """POST /v1/tax/calculations for a set of line items shipped to ``address``.

    ``line_items`` is a list of {"amount": cents, "reference": str,
    "tax_code": str, "quantity": int?} (amounts tax-exclusive, matching
    checkout). ``address`` is {"line1","city","state","postal_code","country"}.
    Returns the parsed calculation dict — ``id``, ``tax_amount_exclusive`` (total
    tax in cents), and ``tax_breakdown``/``tax_summary`` for the jurisdictions.
    Raises StripeError on non-2xx."""
    if not secret_key:
        raise StripeError("Stripe secret key is not configured")
    if not line_items:
        raise StripeError("cannot compute tax with no line items")
    nested = {
        "currency": CURRENCY,
        "line_items": [
            {
                "amount": int(li["amount"]),
                "reference": li["reference"],
                "tax_code": li.get("tax_code", TAX_CODE_GOODS),
                "tax_behavior": "exclusive",
                "quantity": int(li.get("quantity", 1)),
            }
            for li in line_items
        ],
        "customer_details": {"address": address, "address_source": address_source},
    }
    if customer:
        nested["customer"] = customer
    resp = post(
        f"{STRIPE_API_BASE}/v1/tax/calculations",
        data=_flatten("", nested, {}),
        auth=(secret_key, ""),
        timeout=timeout,
    )
    return _parse(resp, "tax calculation")


def create_tax_transaction(secret_key, *, calculation, reference, post=requests.post, timeout=DEFAULT_TIMEOUT):
    """Record a tax/transaction from a calculation so Stripe Tax reports include
    the settled tax (POST /v1/tax/transactions/create_from_calculation).
    ``reference`` must be unique per transaction (use the order name). Returns the
    parsed transaction dict. Raises StripeError on non-2xx."""
    if not secret_key:
        raise StripeError("Stripe secret key is not configured")
    if not calculation:
        raise StripeError("cannot create a tax transaction without a calculation id")
    resp = post(
        f"{STRIPE_API_BASE}/v1/tax/transactions/create_from_calculation",
        data=_flatten("", {"calculation": calculation, "reference": reference}, {}),
        auth=(secret_key, ""),
        timeout=timeout,
    )
    return _parse(resp, "tax transaction")


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
        # An unparseable body on a non-2xx leaves the outcome UNKNOWN (we never
        # saw Stripe's verdict); surface the HTTP status so the caller classifies
        # a 5xx as reconcile-before-retry (GOL-3011).
        raise StripeError(f"Stripe {what}: unparseable response (HTTP {status})", http_status=status or None) from exc
    if status < 200 or status >= 300:
        error = (body or {}).get("error", {}) or {}
        message = error.get("message", f"HTTP {status}")
        err_type = error.get("type")
        # A declined card (typically HTTP 402, error.type card_error) is
        # recoverable: raise the card-specific error so an off-session
        # settlement can dun-and-retry rather than treat it as a hard failure.
        if status == 402 or err_type == "card_error":
            pi = error.get("payment_intent") or {}
            raise StripeCardError(
                f"Stripe {what} declined: {message}",
                code=error.get("code"),
                decline_code=error.get("decline_code"),
                payment_intent=pi.get("id") if isinstance(pi, dict) else pi,
            )
        raise StripeError(f"Stripe {what} failed: {message}", http_status=status, error_type=err_type)
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
