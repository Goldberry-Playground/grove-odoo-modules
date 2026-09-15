"""Pure-Python carrier-tracking clients + status mapping (GOL-2272, Pirate Ship C).

Two thin OAuth 2.0 client-credentials clients — UPS Track API and USPS Tracking
v3 — plus the pure mappers that fold a raw carrier payload into the delivery-status
vocabulary ``shipment_email`` already consumes (``transit`` / ``out_for_delivery``
/ ``delivered`` / ``failure``, or ``None`` for "no change"). Stdlib + ``requests``
only, no Odoo import, so token fetch and mapping unit-test without a DB (mirrors
``shippo_client`` / ``stripe_gateway``). The cron in ``sale_order.py`` composes
these clients with the existing ``_apply_delivery_status`` email path, so the
shipped / out-for-delivery / delivered emails and their once-only guard are
unchanged — this module only sources the events Shippo's webhook used to push.

Design: docs/superpowers/specs/2026-09-09-pirateship-fulfillment-design.md § C.
Credentials (UPS_CLIENT_ID/SECRET, USPS_CLIENT_ID/SECRET) reach Odoo through the
compose ``${VAR:-}`` passthrough (separate odoocker PR); Josh vaults them per
stage. No live carrier calls in CI — every test injects a fake ``session``.
"""

import base64
import logging
import os
import time
from urllib.parse import quote

import requests

_logger = logging.getLogger(__name__)

# Delivery-status vocabulary shipment_email consumes. The three PROGRESS statuses
# are strictly ordered so a multi-box order takes the LEAST-advanced box status
# (an order is not "delivered" until every box is). ``failure`` is off this
# ladder: it drives a silent Discord ops note, never a customer email.
STATUS_TRANSIT = "transit"
STATUS_OUT_FOR_DELIVERY = "out_for_delivery"
STATUS_DELIVERED = "delivered"
STATUS_FAILURE = "failure"

_PROGRESS_RANK = {STATUS_TRANSIT: 1, STATUS_OUT_FOR_DELIVERY: 2, STATUS_DELIVERED: 3}

# Carrier developer endpoints. Env-overridable so a QA/sandbox app (UPS CIE,
# USPS test) can point elsewhere without a code change; prod defaults are live.
UPS_BASE = os.environ.get("UPS_API_BASE", "https://onlinetools.ups.com")
USPS_BASE = os.environ.get("USPS_API_BASE", "https://apis.usps.com")


class CarrierError(RuntimeError):
    """Any carrier call that failed — logged and skipped by the cron."""


class CarrierAuthError(CarrierError):
    """OAuth token fetch or a 401 on a tracked call. Counted toward the
    three-consecutive-failures trip that pauses a carrier for the run."""


def least_advanced_status(statuses):
    """The least-advanced PROGRESS status in ``statuses`` (``transit`` <
    ``out_for_delivery`` < ``delivered``), or None when none are present.

    Called with every box's mapped status on one order: an order only reaches
    ``delivered`` when its slowest box has, so the delivered email never fires
    early on a multi-box shipment. Non-progress values (``failure``, ``None``)
    are ignored here — the caller handles the exception note separately.
    """
    ranked = [s for s in statuses if s in _PROGRESS_RANK]
    if not ranked:
        return None
    return min(ranked, key=lambda s: _PROGRESS_RANK[s])


# ── Status mapping ───────────────────────────────────────────────────────────

# Keyword fallback shared by both carriers, checked most-terminal first. Ordered
# tuples, not a dict, because the FIRST match wins (an "out for delivery" phrase
# also contains "delivery"). Applied to a lowercased status phrase.
_KEYWORD_RULES = (
    (STATUS_DELIVERED, ("delivered",)),
    (STATUS_OUT_FOR_DELIVERY, ("out for delivery",)),
    (
        STATUS_FAILURE,
        ("alert", "return to sender", "returned to sender", "undeliverable", "exception", "delivery failed"),
    ),
    (
        STATUS_TRANSIT,
        ("in transit", "in-transit", "accepted", "arrived", "departed", "picked up", "en route", "moving", "processed"),
    ),
)


def _classify_phrase(phrase):
    """Map a free-text carrier status phrase to our vocabulary, or None.

    Pre-shipment / "label created" phrases match nothing and stay None (no
    change) — the order already sits at ``label_purchased`` and must not email
    "shipped" before it actually moves.
    """
    text = (phrase or "").lower()
    if not text.strip():
        return None
    for status, keywords in _KEYWORD_RULES:
        if any(k in text for k in keywords):
            return status
    return None


def map_ups_status(payload):
    """Fold a UPS Track ``details`` response into our vocabulary, or None.

    Reads the newest package's ``currentStatus`` (falling back to the latest
    ``activity`` scan on the older schema). UPS classifies by a coarse status
    ``type`` code — ``D`` delivered, ``X`` exception, ``I``/``O``/``P`` moving,
    ``M``/``MV`` manifest-only (label made, not moving) — and carries the
    human phrase in ``description``; "out for delivery" only appears in the
    description while the type is still ``I``, so the phrase is checked first.
    """
    pkg = _ups_package(payload)
    if not pkg:
        return None
    status = pkg.get("currentStatus") or {}
    type_ = (status.get("type") or "").upper()
    desc = status.get("description") or ""
    if not type_ and not desc:
        activities = pkg.get("activity") or []
        act_status = (activities[0].get("status") if activities else None) or {}
        type_ = (act_status.get("type") or "").upper()
        desc = act_status.get("description") or ""
    if type_ == "D":
        return STATUS_DELIVERED
    if type_ == "X":
        return STATUS_FAILURE
    # Description carries the finer state (out-for-delivery, exception wording)
    # that the coarse type code lacks; consult it before the type buckets.
    by_phrase = _classify_phrase(desc)
    if by_phrase is not None:
        return by_phrase
    if type_ in ("I", "O", "P"):
        return STATUS_TRANSIT
    # 'M' / 'MV' (manifest / billing-info received) and anything unknown: no change.
    return None


def _ups_package(payload):
    """The first package block from a UPS Track response, or None. UPS nests it
    as ``trackResponse.shipment[].package[]``; guarded end-to-end so a partial
    or error body maps to "no change" rather than raising."""
    shipments = ((payload or {}).get("trackResponse") or {}).get("shipment") or []
    if not shipments:
        return None
    packages = (shipments[0] or {}).get("package") or []
    return packages[0] if packages else None


def map_usps_status(payload):
    """Fold a USPS Tracking v3 response into our vocabulary, or None.

    USPS answers with a human ``status`` phrase plus a coarse ``statusCategory``
    ("In Transit", "Out for Delivery", "Delivered", "Alert", "Pre-Shipment");
    both are fed to the shared keyword classifier so "Delivered to Agent" and
    "Delivered, In/At Mailbox" both read as ``delivered`` and an "Alert" reads
    as ``failure``.
    """
    p = payload or {}
    phrase = " ".join(str(p.get(k) or "") for k in ("status", "statusCategory", "statusSummary"))
    return _classify_phrase(phrase)


# ── OAuth clients ────────────────────────────────────────────────────────────


class _OAuthTrackClient:
    """Shared client-credentials token cache + tracked-call plumbing. Subclasses
    supply the carrier's token request, track URL, and status mapper. ``session``
    is injected (defaults to ``requests``) so tests never touch the network."""

    carrier = ""

    def __init__(self, client_id, client_secret, *, session=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self._session = session or requests
        self._token = None
        self._token_expiry = 0.0  # monotonic seconds; 0 forces a fetch

    # -- token ---------------------------------------------------------------

    def _token_or_fetch(self):
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        resp = self._request_token()
        if resp.status_code in (400, 401, 403):
            raise CarrierAuthError(f"{self.carrier} token rejected: HTTP {resp.status_code}")
        try:
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — normalise to our error type
            raise CarrierError(f"{self.carrier} token fetch failed: {exc}") from exc
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise CarrierAuthError(f"{self.carrier} token response missing access_token")
        # Cache until 60 s before the carrier's stated expiry so an in-flight
        # request can never present a just-expired token.
        expires_in = int(data.get("expires_in") or 3600)
        self._token = token
        self._token_expiry = time.monotonic() + max(0, expires_in - 60)
        return token

    def _request_token(self):
        raise NotImplementedError

    # -- tracking ------------------------------------------------------------

    def track(self, tracking_number):
        """Return the mapped delivery status for ``tracking_number`` (or None for
        "no change"). Raises CarrierAuthError on a 401/403, CarrierError on any
        other transport failure — the cron logs-and-skips the latter and counts
        the former toward the pause trip."""
        token = self._token_or_fetch()
        resp = self._session.get(
            self._track_url(tracking_number),
            headers=self._track_headers(token, tracking_number),
            timeout=30,
        )
        if resp.status_code in (401, 403):
            # A rejected token mid-run: drop the cache so the next call re-auths,
            # and surface as an auth failure.
            self._token = None
            self._token_expiry = 0.0
            raise CarrierAuthError(f"{self.carrier} track rejected: HTTP {resp.status_code}")
        try:
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise CarrierError(f"{self.carrier} track failed for {tracking_number}: {exc}") from exc
        return self._map_status(resp.json())

    def _track_url(self, tracking_number):
        raise NotImplementedError

    def _track_headers(self, token, tracking_number):
        return {"Authorization": f"Bearer {token}"}

    def _map_status(self, payload):
        raise NotImplementedError


class UpsTrackClient(_OAuthTrackClient):
    carrier = "UPS"

    def _request_token(self):
        creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        return self._session.post(
            f"{UPS_BASE}/security/v1/oauth/token",
            data={"grant_type": "client_credentials"},
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
        )

    def _track_url(self, tracking_number):
        return f"{UPS_BASE}/api/track/v1/details/{quote(str(tracking_number), safe='')}"

    def _track_headers(self, token, tracking_number):
        # UPS requires a per-request transaction id (any opaque string, unique
        # enough to correlate) + a source label. The tracking number serves.
        return {
            "Authorization": f"Bearer {token}",
            "transId": str(tracking_number)[:32],
            "transactionSrc": "grove_headless",
        }

    def _map_status(self, payload):
        return map_ups_status(payload)


class UspsTrackClient(_OAuthTrackClient):
    carrier = "USPS"

    def _request_token(self):
        return self._session.post(
            f"{USPS_BASE}/oauth2/v3/token",
            json={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )

    def _track_url(self, tracking_number):
        return f"{USPS_BASE}/tracking/v3/tracking/{quote(str(tracking_number), safe='')}?expand=DETAIL"

    def _map_status(self, payload):
        return map_usps_status(payload)


# ── Client factory ───────────────────────────────────────────────────────────


def build_clients(*, session=None):
    """Build the per-carrier client map from the environment. A carrier with no
    credentials configured maps to None (skipped, not an error) so a stage that
    has only provisioned one carrier still polls it. ``session`` is threaded
    through for tests."""
    clients = {}
    ups_id, ups_secret = os.environ.get("UPS_CLIENT_ID"), os.environ.get("UPS_CLIENT_SECRET")
    clients["UPS"] = UpsTrackClient(ups_id, ups_secret, session=session) if ups_id and ups_secret else None
    usps_id, usps_secret = os.environ.get("USPS_CLIENT_ID"), os.environ.get("USPS_CLIENT_SECRET")
    clients["USPS"] = UspsTrackClient(usps_id, usps_secret, session=session) if usps_id and usps_secret else None
    return clients
