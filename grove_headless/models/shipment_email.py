"""Branded shipment-notification email copy + a carrier -> tracking-URL helper
(GOL-1979, Phase 3 of GOL-1975). Pure Python / stdlib so the ratified voice and
the per-carrier deep links unit-test without an Odoo DB (mirrors preorder_email
/ shipping_calendar / stripe_gateway).

This is a DISTINCT, branded shipment notice, not the order-confirmation receipt:
it renders the carrier and a clickable tracking link per packed box. Copy follows
house voice: no em dashes in customer-facing text.

The controller (controllers/main.py _notify_shipping_status) owns idempotency:
it only emails on a delivery-status *transition*, so exactly one "shipped" notice
goes out even when both the operator signal (Phase 2) and the Shippo `transit`
scan land. This module is a pure renderer and holds no state of its own.
"""

from html import escape
from urllib.parse import quote

BRAND = "Goldberry Grove Nursery"

# Subject + lead line per notify-worthy delivery status. Keys double as the set
# of statuses that produce an email (see NOTIFY_STATUSES); every other Shippo
# status is a silent status update.
_SUBJECT = {
    "transit": "Your order {order} has shipped",
    "delivered": "Your order {order} has been delivered",
}
_LEAD = {
    "transit": "Good news, your order is on its way.",
    "delivered": "Your order has been delivered. We hope you love it.",
}

NOTIFY_STATUSES = frozenset(_SUBJECT)

# Per-carrier public tracking deep link. The tracking number is URL-encoded so a
# stray space or slash can never break the link. Keys are matched case-folded
# against the carrier name Shippo persists on the label (grove_shipping_carriers,
# e.g. "UPS" / "USPS"); an unknown or blank carrier yields no link (the number is
# still shown as plain text). Aliases cover the common long-form provider names.
_TRACKING_URL = {
    "UPS": "https://www.ups.com/track?loc=en_US&tracknum={t}",
    "USPS": "https://tools.usps.com/go/TrackConfirmAction?tLabels={t}",
}
_CARRIER_ALIASES = {
    "UNITED PARCEL SERVICE": "UPS",
    "UNITED STATES POSTAL SERVICE": "USPS",
    "U.S. POSTAL SERVICE": "USPS",
}


def _normalize_carrier(carrier):
    """Fold a stored carrier name to a canonical key ("UPS"/"USPS") or "" when we
    don't recognise it."""
    key = (carrier or "").strip().upper()
    key = _CARRIER_ALIASES.get(key, key)
    return key


def carrier_label(carrier):
    """Human-facing carrier name for the email. Falls back to a generic word so a
    missing/unknown carrier never renders an empty cell."""
    return _normalize_carrier(carrier) or "Carrier"


def tracking_url(carrier, tracking_number):
    """Public tracking deep link for `tracking_number` on `carrier`, or None when
    the carrier is unknown or the number is blank. URL-encodes the number."""
    if not tracking_number or not str(tracking_number).strip():
        return None
    template = _TRACKING_URL.get(_normalize_carrier(carrier))
    if not template:
        return None
    return template.format(t=quote(str(tracking_number).strip(), safe=""))


def _shipment_line(carrier, tracking_number):
    """One '<Carrier>: <tracked link or plain number>' list item, fully escaped."""
    label = escape(carrier_label(carrier))
    number = str(tracking_number).strip()
    url = tracking_url(carrier, tracking_number)
    if url:
        # url is composed only of a fixed template + URL-encoded number, so it is
        # already attribute-safe; escape defensively anyway.
        rendered = f'<a href="{escape(url, quote=True)}">{escape(number)}</a>'
    else:
        rendered = escape(number)
    return f"<li>{label}: {rendered}</li>"


def shipment_notice_copy(status, order_name, customer_name, shipments, balance_line=None, brand=BRAND):
    """Render the branded shipment notice as (subject, body_html).

    `shipments` is an iterable of (carrier, tracking_number) pairs, one per packed
    box. `balance_line` is the optional preorder pre-ship balance sentence (kept
    for deposit orders). Raises KeyError for a status that isn't notify-worthy;
    callers gate on NOTIFY_STATUSES first.
    """
    subject = _SUBJECT[status].format(order=order_name)
    lead = _LEAD[status]
    greeting = escape(customer_name or "there")

    items = [_shipment_line(c, t) for c, t in shipments if str(t or "").strip()]
    tracking_block = (
        '<p style="margin:0 0 8px">Tracking:</p>'
        f'<ul style="margin:0 0 16px;padding-left:20px">{"".join(items)}</ul>'
        if items
        else ""
    )
    balance_block = f'<p style="margin:0 0 16px">{escape(balance_line)}</p>' if balance_line else ""

    body = (
        '<div style="font-family:Georgia,\'Times New Roman\',serif;color:#1F3F2B;'
        'max-width:560px;margin:0 auto;padding:24px">'
        f'<div style="font-size:20px;font-weight:bold;letter-spacing:0.5px;'
        f'border-bottom:2px solid #1F3F2B;padding-bottom:12px;margin-bottom:20px">{escape(brand)}</div>'
        f'<p style="margin:0 0 16px">Hi {greeting},</p>'
        f'<p style="margin:0 0 16px">{escape(lead)}</p>'
        f'<p style="margin:0 0 16px">Order: <strong>{escape(str(order_name))}</strong></p>'
        f'{tracking_block}'
        f'{balance_block}'
        '<p style="margin:24px 0 0;font-size:13px;color:#4a5a4a">'
        f'Thank you for growing with us. {escape(brand)}.</p>'
        '</div>'
    )
    return subject, body
