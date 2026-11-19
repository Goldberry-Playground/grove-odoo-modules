"""Keyless Pirate Ship ``RatesQuery`` client for the live checkout quote (GOL-3927).

Rollout step 1 of the live-ZIP-shipping design
(``docs/superpowers/specs/2026-10-14-live-zip-shipping-design.md`` §1): a pure
Python client that quotes a single destination ZIP + box at its real packed
weight against Pirate Ship's public GraphQL rate endpoint, and returns the
cheapest allowlisted ground service within the transit ceiling.

**Persisted query, no key.** Pirate Ship switched its public GraphQL endpoint to
Apollo's *persisted-queries-only* mode (GOL-2605, 2026-09-29): an ad-hoc query
document is refused. The endpoint still answers **keyless** when the request
carries the ``extensions.persistedQuery.sha256Hash`` of Pirate Ship's own rates
query — the mechanism the web app itself uses. That is what this client sends, so
no carrier credential is needed (spec "The quote source"). The hash lives in one
constant below with a comment pointing at the upstream source it is taken from.

**Pure Python, no Odoo imports** so it unit-tests without a database (see
``tests/test_pirateship_rates.py``). ``quote()`` takes an injectable ``post``
callable (defaults to ``requests.post``) so the tests drive it off recorded real
responses. Everything about checkout is fail-safe around this client: every
typed error maps to a checkout fallback level, and nothing about an order
succeeding depends on this endpoint being up (spec Error handling).

Design note (spec §1): ``scripts/rate_check/rate_check.py`` quotes the same
endpoint for the nightly/zone table. The two were written against the same
request shape (origin 26651, residential, ounces, mail classes 03/93/
GroundAdvantage, Parcel) and share the same selection rule (cheapest allowlisted
ground within ``MAX_TRANSIT_DAYS``, else fastest). Unifying rate_check onto this
client is a follow-up; the constants here are the authoritative request shape for
the *live* path.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import date as _date

import requests

# ── Request shape ────────────────────────────────────────────────────────────
PIRATESHIP_URL = "https://ship.pirateship.com/api/graphql?opname=RatesQuery"

# SHA-256 of Pirate Ship's own persisted ``RatesQuery`` operation. Taken from
# taciturnaxolotl/pirateship-api (MIT), ``src/rates-query.ts`` — the upstream
# that ships a ``sync-query`` drift check — as of 2026-10-05. A retired hash
# makes the endpoint answer ``PersistedQueryNotFound``; the client raises
# ``QueryRetired`` and the weekly drift workflow (spec §7, Terra) opens the
# one-line PR that bumps this constant.
PERSISTED_QUERY_HASH = "2692f7149bbb741a0fd343d1d59d8c02b0d4cf50abe36d3ebce54bdef2b73f85"

# Fixed quote origin: Goldberry Grove, Summersville WV (spec §1). Pirate Ship
# rates off the origin ZIP/city/region; no street is sent to the quote endpoint.
ORIGIN_ZIP = "26651"
ORIGIN_CITY = "Summersville"
ORIGIN_REGION = "WV"

MAIL_CLASS_KEYS = ["03", "93", "GroundAdvantage"]
PACKAGE_TYPE_KEYS = ["Parcel"]

# Ground services we quote AND buy, in a carrier-neutral least-cost race, matched
# exactly on (carrier title, mailClassKey) — an ALLOWLIST, never a global min()
# over every returned rate (GOL-1906). Pirate Ship codes: UPS "03" Ground, "93"
# Ground Saver; USPS "GroundAdvantage". Mirrors
# ``rate_check.GROUND_SERVICE_ALLOWLIST`` so the live path and the nightly table
# race the same services.
GROUND_SERVICE_ALLOWLIST = frozenset(
    {
        ("UPS", "03"),
        ("UPS", "93"),
        ("USPS", "GroundAdvantage"),
    }
)
SERVICE_TITLES = {
    ("UPS", "03"): "UPS Ground",
    ("UPS", "93"): "UPS Ground Saver",
    ("USPS", "GroundAdvantage"): "USPS Ground Advantage",
}

# Transit ceiling (days), matching the label purchase's dormant ceiling
# (``rate_check.MAX_TRANSIT_DAYS``, GOL-1906): the cheapest allowlisted ground
# that delivers within this wins; a rate with no parsable delivery date is NOT
# excluded (unknown transit != slow); if NONE fit the ceiling the fastest known
# wins (ties break cheapest), so a slow week never strands an order unquotable.
MAX_TRANSIT_DAYS = 7

# Total request timeout (seconds), spec §1. Short on purpose: a live quote sits
# in the checkout request path, so a slow endpoint must fall back fast, not hang.
TIMEOUT_S = 3.0

# Error strings that mean the endpoint does not know our persisted query (the
# hash was retired or rotated upstream), as opposed to "no ground rate for this
# parcel". Apollo answers HTTP 200 with a GraphQL error carrying one of these.
_QUERY_RETIRED_MARKERS = (
    "persistedquerynotfound",
    "persistedquerynotsupported",
    "only executes persisted queries",
)


# ── Typed errors (spec §1) ───────────────────────────────────────────────────
class PirateShipError(Exception):
    """Base for every live-quote failure. Each subclass maps to a checkout
    fallback (spec Error handling); catch this to fall back on any of them."""


class QueryRetired(PirateShipError):
    """The persisted query hash is unknown to the endpoint (rotated upstream).

    Fatal for the whole live path, not just this box: live quotes are disabled
    until the hash is updated and an immediate Discord alert fires (deduped
    daily). Handled by the caller, not retried here."""


class Blocked(PirateShipError):
    """HTTP 403/429 or a non-JSON challenge page — Pirate Ship is rate-limiting
    or blocking us. Falls back for this box; counts toward the hourly alert."""


class Timeout(PirateShipError):
    """The request exceeded ``TIMEOUT_S``. Falls back for this box."""


class NoService(PirateShipError):
    """No allowlisted ground rate came back for this box (carrier lapse for the
    route/parcel). Falls back for this box."""


class BadResponse(PirateShipError):
    """The response was not a RatesQuery result we can parse (unexpected GraphQL
    error, malformed JSON, missing fields). Falls back for this box."""


# ── Parsed results ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Surcharge:
    """One itemised line on a rate (e.g. UPS "Delivery Area" $3.45 at 26651)."""

    name: str
    amount: float


@dataclass(frozen=True)
class RateOption:
    """One allowlisted ground rate returned for the quote."""

    service: str  # mailClassKey: "03" / "93" / "GroundAdvantage"
    carrier: str  # carrier title: "UPS" / "USPS"
    service_title: str  # human label, ®/™ stripped
    total: float
    surcharges: tuple = ()
    transit_days: int | None = None


@dataclass(frozen=True)
class Quote:
    """The result of a live quote: the winning service plus every option seen."""

    dest_zip: str
    box_id: str
    weight_lb: float
    winner: RateOption
    options: tuple = field(default_factory=tuple)

    @property
    def total(self) -> float:
        return self.winner.total

    @property
    def service(self) -> str:
        return self.winner.service

    @property
    def service_title(self) -> str:
        return self.winner.service_title


# ── Box geometry (shared with the box engine) ────────────────────────────────
try:
    from . import shipping_boxes
except ImportError:  # loaded standalone (tests import by file path)
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location(
        "grove_shipping_boxes", os.path.join(os.path.dirname(__file__), "shipping_boxes.py")
    )
    shipping_boxes = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(shipping_boxes)


def _box_dims(box_id: str) -> dict:
    """``{length, width, height}`` for ``box_id`` from either packing catalog."""
    box = shipping_boxes.BOXES.get(box_id) or shipping_boxes.POTTED_BOXES.get(box_id)
    if not box:
        raise NoService(f"unknown box id {box_id!r}")
    return {"length": box["length"], "width": box["width"], "height": box["height"]}


# ── Transit parsing (mirrors rate_check.parse_transit_days) ──────────────────
_DELIVERY_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})")


def parse_transit_days(delivery_description, today):
    """Days from ``today`` to Pirate Ship's estimated delivery date, or None.

    Parses the ``M/D`` in ``deliveryDescription``; the year is the next
    occurrence on or after ``today`` (a late-December quote of an early-January
    date rolls to next year). Absent/unparsable -> None, treated by the selector
    as acceptable (unknown transit != slow), mirroring ``rate_check``."""
    if not delivery_description:
        return None
    m = _DELIVERY_DATE_RE.search(delivery_description)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    try:
        target = _date(today.year, month, day)
    except ValueError:
        return None
    if target < today:
        try:
            target = _date(today.year + 1, month, day)
        except ValueError:
            return None
    return (target - today).days


def _normalize_title(title) -> str:
    """Strip ®/™ marks and collapse whitespace from a Pirate Ship rate title."""
    return re.sub(r"\s+", " ", (title or "").replace("®", "").replace("™", "")).strip()


def _parse_surcharges(rate) -> tuple:
    """Itemised surcharges on a rate -> ``(Surcharge, ...)``.

    Pirate Ship returns each as ``{name/title, amount/price}``; we accept either
    key and skip any entry whose amount is missing/unparsable."""
    out = []
    for s in rate.get("surcharges") or []:
        name = s.get("name") or s.get("title") or ""
        raw = s.get("amount")
        if raw is None:
            raw = s.get("price")
        try:
            out.append(Surcharge(name=str(name), amount=round(float(raw), 2)))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def _normalize_rate(rate, today) -> RateOption | None:
    """A Pirate Ship rate object -> ``RateOption``, or None when unpriceable or
    not an allowlisted ground service."""
    try:
        total = float(rate.get("totalPrice"))
    except (TypeError, ValueError):
        return None
    carrier = (rate.get("carrier") or {}).get("title") or ""
    service = rate.get("mailClassKey")
    if (carrier, service) not in GROUND_SERVICE_ALLOWLIST:
        return None
    return RateOption(
        service=service,
        carrier=carrier,
        service_title=SERVICE_TITLES.get((carrier, service)) or _normalize_title(rate.get("title")),
        total=round(total, 2),
        surcharges=_parse_surcharges(rate),
        transit_days=parse_transit_days(rate.get("deliveryDescription"), today),
    )


def select_winner(options):
    """Cheapest allowlisted ground within the transit ceiling, else fastest.

    ``options`` is a list of ``RateOption``. Within the ceiling the cheapest
    wins; if none fit, the fastest known wins (ties break cheapest). Returns the
    winning ``RateOption`` or None when ``options`` is empty. Same rule as
    ``rate_check.select_cheapest_ground`` so the live quote matches the table."""
    if not options:
        return None
    within = [o for o in options if o.transit_days is None or o.transit_days <= MAX_TRANSIT_DAYS]
    if within:
        return min(within, key=lambda o: o.total)
    return min(options, key=lambda o: (o.transit_days, o.total))


def _parse_response(payload, dest_zip, box_id, weight_lb, today) -> Quote:
    """Turn a decoded RatesQuery JSON body into a ``Quote`` or raise a typed
    error. Shared by ``quote`` and the recorded-fixture tests."""
    if not isinstance(payload, dict):
        raise BadResponse(f"expected a JSON object, got {type(payload).__name__}")
    errors = payload.get("errors")
    if errors:
        text = str(errors).lower()
        if any(marker in text for marker in _QUERY_RETIRED_MARKERS):
            raise QueryRetired(f"persisted query not recognized: {errors}")
        raise BadResponse(f"pirateship graphql errors: {errors}")
    rates = ((payload.get("data") or {}).get("rates")) or []
    options = [o for o in (_normalize_rate(r, today) for r in rates) if o is not None]
    winner = select_winner(options)
    if winner is None:
        raise NoService(f"no allowlisted ground rate for {box_id} to {dest_zip}")
    return Quote(
        dest_zip=dest_zip,
        box_id=box_id,
        weight_lb=weight_lb,
        winner=winner,
        options=tuple(options),
    )


def _variables(dest_zip, box_id, weight_lb) -> dict:
    """RatesQuery variables for one destination + box at ``weight_lb``.

    Weight is sent in OUNCES (Pirate Ship's unit); dimensions come from the box
    catalog so the live quote is priced on the carton the packer actually used."""
    dims = _box_dims(box_id)
    return {
        "originZip": ORIGIN_ZIP,
        "originCity": ORIGIN_CITY,
        "originRegionCode": ORIGIN_REGION,
        "destinationZip": str(dest_zip),
        "isResidential": True,
        "destinationCountryCode": "US",
        "weight": float(weight_lb) * 16.0,  # ounces
        "dimensionX": dims["length"],
        "dimensionY": dims["width"],
        "dimensionZ": dims["height"],
        "mailClassKeys": MAIL_CLASS_KEYS,
        "packageTypeKeys": PACKAGE_TYPE_KEYS,
        "showUpsRatesWhen2x7Selected": True,
    }


def _request_body(dest_zip, box_id, weight_lb) -> dict:
    """Apollo persisted-query request body — the hash, no query document."""
    return {
        "operationName": "RatesQuery",
        "variables": _variables(dest_zip, box_id, weight_lb),
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": PERSISTED_QUERY_HASH}},
    }


def quote(dest_zip, box_id, weight_lb, *, post=None, today=None) -> Quote:
    """Live Pirate Ship quote for one box to ``dest_zip`` at ``weight_lb``.

    Returns a :class:`Quote` (winning allowlisted ground service + every option),
    or raises a typed :class:`PirateShipError` subclass — :class:`QueryRetired`,
    :class:`Blocked`, :class:`Timeout`, :class:`NoService`, :class:`BadResponse` —
    each of which the checkout maps to a fallback level. Never caches; errors are
    never cached (spec §2).

    ``post`` is injectable for tests (defaults to ``requests.post``). ``today``
    anchors the transit-day math (defaults to ``date.today()``)."""
    post = post or requests.post
    if today is None:
        today = _date.today()
    body = _request_body(dest_zip, box_id, weight_lb)
    try:
        resp = post(
            PIRATESHIP_URL,
            json=body,
            timeout=TIMEOUT_S,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
    except requests.Timeout as exc:
        raise Timeout(f"pirateship quote timed out after {TIMEOUT_S}s") from exc
    except requests.RequestException as exc:
        raise Blocked(f"pirateship request failed: {exc}") from exc

    status = getattr(resp, "status_code", 200)
    if status in (403, 429):
        raise Blocked(f"pirateship returned HTTP {status} (rate-limited or blocked)")
    try:
        payload = resp.json()
    except ValueError as exc:
        # A challenge/interstitial page is HTML, not JSON — treat as blocked.
        raise Blocked("pirateship returned a non-JSON body (challenge page?)") from exc
    return _parse_response(payload, str(dest_zip), box_id, float(weight_lb), today)
