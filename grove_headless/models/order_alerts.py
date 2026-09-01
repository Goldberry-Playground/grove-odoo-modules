"""Pure formatters for the post-purchase ops chain (GOL-1933).

Deliberately free of any ``odoo`` import so the message-building logic can be
unit-tested under plain pytest. The webhook (controllers/main.py) pulls the
primitive values off the confirmed order once and calls these to build:

  * the Discord ops ping fired on *every* new paid order (shipping AND pickup),
  * the merchant notification email sent to ``order.company_id.email``.

Side-effect free by design: the caller owns the actual Discord POST and the
``mail.mail`` send, and their best-effort error handling — a formatter never
does I/O and never raises on ordinary input.
"""

_FULFILLMENT_LABELS = {"ship": "Shipping", "pickup": "Farm pickup"}


def fulfillment_label(fulfillment):
    """Human label for a stored ``grove_fulfillment`` value.

    Unknown/absent (legacy orders written before the field existed) fall back to
    "Shipping" — the safer default, since a mislabelled pickup only over-informs
    staff whereas a mislabelled ship could hide that a label is owed."""
    return _FULFILLMENT_LABELS.get(fulfillment, "Shipping")


def format_money(amount, currency="USD"):
    """`$1,234.50 USD` — thousands-separated, two decimals."""
    try:
        return f"${float(amount):,.2f} {currency}"
    except (TypeError, ValueError):
        return f"{amount} {currency}"


def _line_bullets(lines):
    """`• 2× American Plum (Bareroot)` per (name, qty). Integer quantities lose
    the trailing `.0`; fractional ones (rare — trees pack per unit) are kept."""
    out = []
    for name, qty in lines:
        try:
            q = int(qty) if float(qty) == int(qty) else qty
        except (TypeError, ValueError):
            q = qty
        out.append(f"• {q}× {name}")
    return out


def format_new_order_discord(
    *,
    order_ref,
    customer,
    customer_email=None,
    fulfillment,
    is_deposit=False,
    lines,
    total,
    currency="USD",
    carrier=None,
    tracking=None,
):
    """Build the Discord ops-channel content string for a new paid order.

    Fires for both fulfilment types. Pickup orders get no label but still
    notify (Josh called this out). Carrier/tracking are included only when
    present — at new-paid-order time a label is usually not bought yet, so the
    lines are simply omitted rather than shown empty.
    """
    kind = "New preorder (deposit)" if is_deposit else "New order"
    header = f":package: {kind} {order_ref} — {fulfillment_label(fulfillment)}"
    who = customer or "Unknown customer"
    if customer_email:
        who = f"{who} <{customer_email}>"
    parts = [header, f"Customer: {who}"]
    parts.extend(_line_bullets(lines))
    parts.append(f"Total: {format_money(total, currency)}")
    if carrier or tracking:
        ship_bits = []
        if carrier:
            ship_bits.append(f"Carrier: {carrier}")
        if tracking:
            ship_bits.append(f"Tracking: {tracking}")
        parts.append(" · ".join(ship_bits))
    return "\n".join(parts)


def format_merchant_email(
    *,
    order_ref,
    customer,
    customer_email=None,
    fulfillment,
    is_deposit=False,
    lines,
    total,
    currency="USD",
    shipping_address=None,
    carrier=None,
    tracking=None,
):
    """Build ``(subject, body_html)`` for the internal merchant notification.

    This is the *staff* alert (outbound to ``order.company_id.email``), separate
    from the customer's receipt (``_send_order_confirmation_email``). Reply-To on
    the customer templates already routes replies to the merchant; this closes
    the other direction — telling staff a sale happened.
    """
    kind = "preorder" if is_deposit else "order"
    subject = f"New {kind} {order_ref} — {fulfillment_label(fulfillment)}"

    rows = "".join(
        f"<li>{q}× {name}</li>"
        for name, q in (
            (name, int(qty) if _is_int(qty) else qty) for name, qty in lines
        )
    )
    who = customer or "Unknown customer"
    if customer_email:
        who = f"{who} ({customer_email})"

    ship_html = ""
    if shipping_address:
        ship_html = f"<p><strong>Ship to:</strong><br/>{shipping_address}</p>"
    track_html = ""
    if carrier or tracking:
        bits = []
        if carrier:
            bits.append(f"Carrier: {carrier}")
        if tracking:
            bits.append(f"Tracking: {tracking}")
        track_html = f"<p>{' &middot; '.join(bits)}</p>"

    body_html = (
        f"<p>A new {kind} was paid on the website.</p>"
        f"<p><strong>Order:</strong> {order_ref}<br/>"
        f"<strong>Fulfilment:</strong> {fulfillment_label(fulfillment)}<br/>"
        f"<strong>Customer:</strong> {who}</p>"
        f"<ul>{rows}</ul>"
        f"<p><strong>Total:</strong> {format_money(total, currency)}</p>"
        f"{ship_html}"
        f"{track_html}"
    )
    return subject, body_html


def _is_int(qty):
    try:
        return float(qty) == int(qty)
    except (TypeError, ValueError):
        return False
