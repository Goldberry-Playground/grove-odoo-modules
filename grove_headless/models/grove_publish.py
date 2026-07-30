"""Storefront publish webhook — signing + delivery (pure Python, mockable).

Odoo → grove-sites publish/revalidate signal (GOL-985, "PR B" of the guides
chain — see GATH-130 / PR A #51 for the serializer half). When a species guide
is approved and published from the Odoo product form, we POST a small JSON event
to the tenant's grove-sites webhook so Next.js can revalidate the affected
pages. The body is signed with a **per-tenant** HMAC-SHA256 secret so the
receiver can prove the request really came from Odoo.

Contract (kept in lock-step with the grove-sites receiver — see
`docs/publish-webhook-contract.md`):

    POST <tenant webhook url>
    Headers:
        Content-Type: application/json
        X-Grove-Event:        <event type, e.g. "guide.publish">
        X-Grove-Delivery:     <opaque delivery id, unique per logical publish>
        X-Grove-Tenant:       <goldberry|ggg|nursery>
        X-Grove-Signature-256: sha256=<hex HMAC-SHA256(secret, RAW body bytes)>
    Body: the exact bytes that were signed (see `serialize`).

The receiver MUST verify the signature over the **raw** request body it read off
the wire — never over a re-serialized object — because JSON key order / spacing
would otherwise change the bytes and break the MAC. We therefore send the exact
signed bytes via `data=` (not `json=`, which would re-encode).

Injection point: pass `post=` (default `requests.post`) so Odoo methods and
tests share one code path — same pattern as `shippo_client` / `stripe_gateway`.
"""

import hashlib
import hmac
import json

import requests

DEFAULT_TIMEOUT = 10  # seconds — manual admin action, short and fail-loud

SIGNATURE_HEADER = "X-Grove-Signature-256"
EVENT_HEADER = "X-Grove-Event"
DELIVERY_HEADER = "X-Grove-Delivery"
TENANT_HEADER = "X-Grove-Tenant"

SIGNATURE_PREFIX = "sha256="


class PublishDeliveryError(RuntimeError):
    """Network-level failure delivering a publish webhook (never a non-2xx)."""


def serialize(payload: dict) -> bytes:
    """Canonical bytes for a payload — deterministic so a replay signs identically.

    Compact separators + sorted keys means the same logical payload always yields
    the same bytes (stable delivery_id → stable signature on retry), and the
    receiver never has to guess our encoding: it verifies over exactly these
    bytes as received.
    """
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sign_body(secret: str, body: bytes) -> str:
    """`sha256=<hexdigest>` HMAC of the raw body bytes (GitHub-style header value)."""
    if not secret:
        raise ValueError("publish webhook secret is empty")
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
    return SIGNATURE_PREFIX + mac.hexdigest()


def verify_signature(secret: str, body: bytes, provided: str) -> bool:
    """Constant-time check of a `sha256=...` signature against raw body bytes.

    Mirror of `sign_body` for the receiver side / round-trip tests. Uses
    `hmac.compare_digest` so a mismatch can't be timed. A missing/empty secret
    or signature fails closed (returns False), never raises.
    """
    if not secret or not provided:
        return False
    try:
        expected = sign_body(secret, body)
    except ValueError:
        return False
    return hmac.compare_digest(expected, provided)


def build_headers(*, event_type: str, delivery_id: str, tenant: str, signature: str) -> dict:
    return {
        "Content-Type": "application/json",
        EVENT_HEADER: event_type,
        DELIVERY_HEADER: delivery_id,
        TENANT_HEADER: tenant,
        SIGNATURE_HEADER: signature,
    }


def deliver(
    url: str,
    secret: str,
    payload: dict,
    *,
    event_type: str,
    delivery_id: str,
    tenant: str,
    post=None,
    timeout: int = DEFAULT_TIMEOUT,
):
    """Sign `payload` and POST the exact signed bytes.

    Returns `(body_bytes, signature, response)`. Raises `PublishDeliveryError`
    on a transport failure (timeout / connection error) so the caller can record
    the event as failed and offer a retry; a non-2xx HTTP response is NOT an
    exception — the caller inspects `response.status_code`.

    `post` defaults to `requests.post`, resolved at call time so tests can patch
    `grove_publish.requests.post` without threading an injection through the ORM.
    """
    sender = post or requests.post
    body = serialize(payload)
    signature = sign_body(secret, body)
    headers = build_headers(event_type=event_type, delivery_id=delivery_id, tenant=tenant, signature=signature)
    try:
        response = sender(url, data=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise PublishDeliveryError(str(exc)) from exc
    return body, signature, response
