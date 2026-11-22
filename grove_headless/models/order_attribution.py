"""Pure validation for storefront order attribution (GOL-4077).

No Odoo imports on purpose: the untrusted-input sanitiser is the security-
sensitive surface here (anyone with a storefront bearer key can POST junk), so
it lives in a pure module that can be unit-tested without an Odoo runtime —
loaded by file path in ``tests/`` the same way ``shipping_zones.py`` is.
``controllers.main`` imports the spec + ``sanitize_attribution`` from here and
layers the ORM write / built-in-UTM mirror / read endpoint on top.

Design of record: the GOL-4074 analytics-and-tracking strategy note, sections
"Event plan" and "Why revenue attribution lives in Odoo".
"""

import re

# UTM values are lowercased on write so "Email" and "email" don't split a report
# (GOL-4077). Bounded, printable set — letters, digits, and the punctuation a
# real source/medium/campaign/content/term carries. Matched AFTER lowercasing.
_UTM_RE = re.compile(r"^[a-z0-9 _.+%:,/&'\-]+$")
# The URL path + query the shopper landed on, and the referrer. Not a full URL
# validator — just a bound that rejects whitespace and control characters while
# admitting the RFC-3986 characters a real path/referrer uses.
_URL_RE = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+$")
# Analytics / ad-platform client ids: GA client id & session id, Meta _fbp/_fbc.
_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+$")

# The spec drives both validation and the sale.order write:
# (payload key, sale.order field, lowercase?, max length, allowed-charset regex).
ATTRIBUTION_SPEC = (
    ("utm_source", "grove_utm_source", True, 100, _UTM_RE),
    ("utm_medium", "grove_utm_medium", True, 100, _UTM_RE),
    ("utm_campaign", "grove_utm_campaign", True, 150, _UTM_RE),
    ("utm_content", "grove_utm_content", True, 150, _UTM_RE),
    ("utm_term", "grove_utm_term", True, 150, _UTM_RE),
    ("landing_page", "grove_landing_page", False, 500, _URL_RE),
    ("referrer", "grove_referrer", False, 500, _URL_RE),
    ("ga_client_id", "grove_ga_client_id", False, 100, _ID_RE),
    ("ga_session_id", "grove_ga_session_id", False, 100, _ID_RE),
    ("fbp", "grove_fbp", False, 255, _ID_RE),
    ("fbc", "grove_fbc", False, 255, _ID_RE),
)

# source / medium / campaign are mirrored onto Odoo's built-in utm.mixin m2o
# fields when this build exposes them on sale.order (controllers.main guards on
# field presence): (sale.order field, utm model, m2o field).
UTM_MIRROR = (
    ("grove_utm_source", "utm.source", "source_id"),
    ("grove_utm_medium", "utm.medium", "medium_id"),
    ("grove_utm_campaign", "utm.campaign", "campaign_id"),
)

# Orders whose money has been collected — the revenue the Sunday attribution read
# recognises. A deposit-paid preorder counts (a deposit landed); a draft, expired
# or refunded order does not. Mirrors sale_order's paid/settling checkout states.
REVENUE_STATUSES = ("paid", "deposit_paid", "settled", "settlement_failed", "settlement_error")

# ``group_by`` query value → sale.order field, for the attribution read endpoint.
GROUP_FIELDS = {
    "source": "grove_utm_source",
    "medium": "grove_utm_medium",
    "campaign": "grove_utm_campaign",
    "content": "grove_utm_content",
    "term": "grove_utm_term",
}


def sanitize_attribution(payload):
    """Pull a clean sale.order attribution write-dict out of an untrusted payload.

    Reads the optional top-level ``attribution`` object. Every field is validated
    against its length cap and charset; a value that fails is DROPPED, never
    raised — attribution is marketing telemetry and must never cost a sale
    ("Unknown or missing keys must never fail an order", GOL-4077). UTM values are
    trimmed then lowercased so "Email" and "email" don't split a report; URL-ish
    and id-ish values are trimmed only (paths and ids are case-sensitive).

    Returns a (possibly empty) dict of sale.order field → value, plus the boolean
    ``grove_marketing_tags_eligible`` when the payload carries a real bool.
    """
    attribution = payload.get("attribution")
    if not isinstance(attribution, dict):
        return {}
    vals = {}
    for key, field, lowercase, max_len, charset_re in ATTRIBUTION_SPEC:
        raw = attribution.get(key)
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if not value or len(value) > max_len:
            continue
        if lowercase:
            value = value.lower()
        # Match the charset AFTER lowercasing so the regex sees the stored value.
        if not charset_re.match(value):
            continue
        vals[field] = value
    eligible = attribution.get("marketing_tags_eligible")
    if isinstance(eligible, bool):
        vals["grove_marketing_tags_eligible"] = eligible
    return vals
