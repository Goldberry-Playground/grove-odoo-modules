"""Weekly order/preorder rollup digest (GOL-1978).

Pure Python / stdlib so the rollup logic unit-tests without an Odoo DB,
mirroring the preorder_email / shipping_calendar convention.  The Odoo-side
cron (order_rollup.py) queries sale.order, maps records to the plain-dict
shape expected here, and emits the result via mail.mail + Discord.

No em dashes in customer or merchant copy (house voice rule).
"""

import html
from datetime import date, timedelta

# Thresholds for the preorder reminder section.
_WAVE_SOON_DAYS = 30  # highlight a wave as "ships soon" if start <= today + 30d

_MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _fmt_currency(cents_or_float) -> str:
    """Dollar string with cents, e.g. '$1,234.50'.  Accepts a float or int."""
    v = float(cents_or_float or 0)
    return f"${v:,.2f}"


def _fmt_date(d) -> str:
    """Format a date or ISO string as 'Mon D, YYYY'.

    Locale-free and no ``%-d`` (that strftime flag is a glibc-only extension the
    rest of grove_headless avoids, see shipping_calendar._fmt) so the digest
    formats identically on any platform / CI runner.
    """
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    if not isinstance(d, date):
        return str(d)
    return f"{_MONTHS[d.month]} {d.day}, {d.year}"


def build_digest(
    orders: list[dict],
    pickings: list[dict] | None = None,
    today: date | None = None,
    period_days: int = 7,
    next_wave_fn=None,
    usda_zone_fn=None,
) -> dict:
    """Compute the weekly rollup from a flat list of order dicts.

    Each order dict must carry:
      - id: int or str
      - name: str (e.g. "S00042")
      - date_order: date | ISO str
      - amount_total: float
      - grove_checkout_status: str | None
      - grove_delivery_status: str | None
      - grove_preorder_variant_ids: str | None  (comma-sep variant ids when deposit taken)
      - partner_name: str
      - partner_shipping_zip: str | None  (window zip: customer's, or the farm's for pickup)
      - is_pickup: bool  (True when fulfillment == pickup)
      - units: int | None  (physical tree units on the order; used for units-ordered)

    ``pickings`` is a separate list of completed-shipment dicts, each carrying:
      - date_done: date | ISO str  (when the picking was completed)
      - units: int                 (physical tree units on the picking)

    Units shipped is measured from ``pickings`` (stock.picking date_done),
    NOT from orders placed in the window (GOL-1978 review, Josh's call): a
    preorder placed this week ships weeks later, so ordered and shipped
    legitimately diverge.

    Returns a structured digest dict ready for rendering.  Pure -- no I/O.
    """
    if today is None:
        today = date.today()
    period_start = today - timedelta(days=period_days)

    # Partition into period orders (placed in window) vs all outstanding
    period_orders = [o for o in orders if _order_date(o) >= period_start]

    # --- Revenue & volume ---
    # GOL-2052/2053 split a preorder's money in two: a deposit taken at
    # checkout (grove_amount_charged_today) and the balance captured off-
    # session when the box ships. A single "revenue" number over amount_total
    # conflates booked value with cash actually collected and misreports the
    # week, so we report three distinct figures:
    #   booked_total        — gross value of orders written this week
    #                         (amount_total, an estimate for preorders whose
    #                         real shipping/tax settle at ship).
    #   collected_total     — cash actually taken at checkout this week
    #                         (deposits + full payments), via _collected_today.
    #   deposit_balance_due — money still owed on this week's deposit-only
    #                         preorders, captured off-session at ship.
    orders_placed = len(period_orders)
    booked_total = sum(float(o.get("amount_total") or 0) for o in period_orders)
    collected_total = sum(_collected_today(o) for o in period_orders)
    deposit_balance_due = sum(
        _balance_due(o) for o in period_orders if (o.get("grove_checkout_status") or "") == "deposit_paid"
    )

    # --- Units ordered vs shipped in period ---
    # Ordered: physical tree units on orders PLACED in the window.
    units_ordered = sum(int(o["units"]) for o in period_orders if o.get("units") is not None)
    # Shipped: physical tree units on stock.picking records COMPLETED (date_done)
    # in the window. Decoupled from orders placed, so a preorder booked this week
    # but not yet fulfilled does not inflate the shipped figure.
    units_shipped = sum(
        int(p.get("units") or 0) for p in (pickings or []) if period_start <= _coerce_date(p.get("date_done")) <= today
    )

    # Delivery statuses that mean an order has left the farm; gates the
    # outstanding-preorder and pickup-awaiting sections below.
    shipped_statuses = {"label_purchased", "shipped", "transit", "delivered"}

    # --- Outstanding preorders ---
    outstanding_preorders = [
        o
        for o in orders
        if (o.get("grove_checkout_status") or "") == "deposit_paid"
        and (o.get("grove_delivery_status") or "") not in shipped_statuses
    ]
    preorder_entries = _build_preorder_entries(outstanding_preorders, today, next_wave_fn, usda_zone_fn)

    # --- Pickups awaiting collection ---
    # Pickup orders with no terminal status (not shipped/collected/delivered)
    pickup_awaiting = [
        o
        for o in orders
        if o.get("is_pickup")
        and (o.get("grove_delivery_status") or "") not in shipped_statuses | {"collected"}
        and (o.get("grove_checkout_status") or "") in ("paid", "deposit_paid", "settled")
    ]

    # --- Settlement failures (shipped, balance NOT captured) ---
    # GOL-2053: a preorder that shipped but whose off-session balance charge
    # declined lands in "settlement_failed". That is money already-shipped-not-
    # collected, the highest-priority signal in the digest, so it gets its own
    # section rather than silently vanishing from every other bucket.
    settlement_failures = [
        _settlement_failure(o) for o in orders if (o.get("grove_checkout_status") or "") == "settlement_failed"
    ]

    return {
        "period_start": period_start,
        "period_end": today,
        "period_days": period_days,
        "orders_placed": orders_placed,
        # revenue_total kept as an alias for booked_total (backward compat).
        "revenue_total": booked_total,
        "booked_total": booked_total,
        "collected_total": collected_total,
        "deposit_balance_due": deposit_balance_due,
        "units_ordered": units_ordered,
        "units_shipped": units_shipped,
        "outstanding_preorders": preorder_entries,
        "preorder_count": len(preorder_entries),
        "pickups_awaiting": [_pickup_summary(o) for o in pickup_awaiting],
        "pickup_count": len(pickup_awaiting),
        "settlement_failures": settlement_failures,
        "settlement_failed_count": len(settlement_failures),
    }


def _collected_today(o: dict) -> float:
    """Cash actually taken at checkout for this order.

    grove_amount_charged_today is persisted for every checkout session (full
    payment == amount_total; a deposit-only preorder == just the deposit slice,
    GOL-2052/2053). Legacy orders predating that field record 0.0; treat those
    as fully paid (fall back to amount_total) so historical revenue is not
    undercounted. A brand-new order's deposit is always a positive slice, so a
    0.0 charge unambiguously means "no deposit split recorded".
    """
    charged = float(o.get("amount_charged") or 0)
    if charged > 0:
        return charged
    return float(o.get("amount_total") or 0)


def _balance_due(o: dict) -> float:
    """Money still owed on a deposit-only order: total minus what was taken at
    checkout. Floored at 0 so a fully-collected order never reports negative."""
    return max(float(o.get("amount_total") or 0) - _collected_today(o), 0.0)


def _settlement_failure(o: dict) -> dict:
    return {
        "order_name": o.get("name") or "",
        "partner_name": o.get("partner_name") or "",
        "amount_total": float(o.get("amount_total") or 0),
        "balance_due": _balance_due(o),
        "attempts": int(o.get("settlement_attempts") or 0),
    }


def _coerce_date(d) -> date:
    """Coerce a date | ISO str | None to a ``date``; unparseable -> date.min."""
    if isinstance(d, date):
        return d
    try:
        return date.fromisoformat(str(d)[:10])
    except (ValueError, TypeError):
        return date.min


def _order_date(o: dict) -> date:
    return _coerce_date(o.get("date_order"))


def _build_preorder_entries(orders, today, next_wave_fn, usda_zone_fn) -> list[dict]:
    """Return one entry per outstanding preorder, annotated with computed ship window."""
    entries = []
    for o in orders:
        zip_code = o.get("partner_shipping_zip") or ""
        wave = None
        if zip_code and next_wave_fn and usda_zone_fn:
            zone = usda_zone_fn(zip_code)
            if zone is not None:
                wave = next_wave_fn(zone, today)

        entry = {
            "order_name": o.get("name") or "",
            "partner_name": o.get("partner_name") or "",
            "amount_total": float(o.get("amount_total") or 0),
            # Deposit taken at checkout vs balance captured at ship (GOL-2053),
            # so the merchant sees the money still to collect on each preorder.
            "amount_charged": _collected_today(o),
            "balance_due": _balance_due(o),
            "date_order": _order_date(o),
        }
        if wave:
            entry["ship_season"] = wave.get("season") or ""
            entry["ship_start"] = wave.get("ship_start")
            entry["ship_end"] = wave.get("ship_end")
            entry["ships_soon"] = isinstance(wave.get("ship_start"), date) and wave["ship_start"] <= today + timedelta(
                days=_WAVE_SOON_DAYS
            )
        else:
            entry["ship_season"] = ""
            entry["ship_start"] = None
            entry["ship_end"] = None
            entry["ships_soon"] = False
        entries.append(entry)

    # Sort: soon-shipping first, then by order date
    entries.sort(key=lambda e: (not e["ships_soon"], e["date_order"]))
    return entries


def _pickup_summary(o: dict) -> dict:
    return {
        "order_name": o.get("name") or "",
        "partner_name": o.get("partner_name") or "",
        "date_order": _order_date(o),
    }


# ---------------------------------------------------------------------------
# Rendering helpers  (plain text + HTML — no Odoo dependency)
# ---------------------------------------------------------------------------


def render_digest_text(digest: dict) -> str:
    """Plain-text version for Discord ops ping."""
    ps = _fmt_date(digest["period_start"])
    pe = _fmt_date(digest["period_end"])
    lines = [
        f"Weekly order rollup ({ps} to {pe})",
        "",
        f"Orders placed:      {digest['orders_placed']}",
        f"Order value booked: {_fmt_currency(digest['booked_total'])}",
        f"Collected at checkout: {_fmt_currency(digest['collected_total'])}",
        f"Deposit balance due at ship: {_fmt_currency(digest['deposit_balance_due'])}",
        f"Units ordered:      {digest['units_ordered']}",
        f"Units shipped:      {digest['units_shipped']}",
    ]

    failures = digest.get("settlement_failures") or []
    if failures:
        lines += ["", f"** Settlement FAILED (shipped, balance not captured) ({len(failures)}): **"]
        for f in failures:
            lines.append(
                f"  {f['order_name']} | {f['partner_name']} | balance {_fmt_currency(f['balance_due'])}"
                f" | {f['attempts']} attempt(s)"
            )

    preorders = digest["outstanding_preorders"]
    if preorders:
        lines += ["", f"Outstanding preorders ({len(preorders)}):"]
        for p in preorders:
            ship_window = _wave_label(p)  # plain text: no HTML escaping
            soon = " [ships soon]" if p.get("ships_soon") else ""
            lines.append(
                f"  {p['order_name']} | {p['partner_name']} | deposit {_fmt_currency(p['amount_charged'])}"
                f" of {_fmt_currency(p['amount_total'])} (balance {_fmt_currency(p['balance_due'])} due at ship)"
                f" | {ship_window}{soon}"
            )
    else:
        lines += ["", "No outstanding preorders."]

    pickups = digest["pickups_awaiting"]
    if pickups:
        lines += ["", f"Pickups awaiting collection ({len(pickups)}):"]
        for pk in pickups:
            lines.append(f"  {pk['order_name']} | {pk['partner_name']} | ordered {_fmt_date(pk['date_order'])}")
    else:
        lines += ["", "No pickups awaiting collection."]

    return "\n".join(lines)


def render_digest_html(digest: dict) -> str:
    """HTML version for merchant inbox email."""
    ps = _fmt_date(digest["period_start"])
    pe = _fmt_date(digest["period_end"])
    parts = [
        "<h2>Weekly order rollup</h2>",
        f"<p><em>{ps} to {pe}</em></p>",
        "<table>",
        f"<tr><td><strong>Orders placed</strong></td><td>{digest['orders_placed']}</td></tr>",
        f"<tr><td><strong>Order value booked</strong></td><td>{_fmt_currency(digest['booked_total'])}</td></tr>",
        f"<tr><td><strong>Collected at checkout</strong></td><td>{_fmt_currency(digest['collected_total'])}</td></tr>",
        "<tr><td><strong>Deposit balance due at ship</strong></td>"
        f"<td>{_fmt_currency(digest['deposit_balance_due'])}</td></tr>",
        f"<tr><td><strong>Units ordered</strong></td><td>{digest['units_ordered']}</td></tr>",
        f"<tr><td><strong>Units shipped</strong></td><td>{digest['units_shipped']}</td></tr>",
        "</table>",
    ]

    failures = digest.get("settlement_failures") or []
    if failures:
        parts.append(f"<h3>Settlement failed (shipped, balance not captured) ({len(failures)})</h3>")
        parts.append("<ul>")
        for f in failures:
            parts.append(
                f"<li><strong>{html.escape(str(f['order_name']))}</strong> | "
                f"{html.escape(str(f['partner_name']))} | balance {_fmt_currency(f['balance_due'])} | "
                f"{f['attempts']} attempt(s)</li>"
            )
        parts.append("</ul>")

    preorders = digest["outstanding_preorders"]
    if preorders:
        parts.append(f"<h3>Outstanding preorders ({len(preorders)})</h3>")
        parts.append("<ul>")
        for p in preorders:
            # Every interpolated checkout-derived field is HTML-escaped
            # (partner_name, order_name, ship_season) per the GOL-1933/1978
            # merchant-notification convention in order_alerts.py.
            ship_window = _wave_label(p, esc=html.escape)
            soon_flag = " <strong>(ships soon)</strong>" if p.get("ships_soon") else ""
            parts.append(
                f"<li><strong>{html.escape(str(p['order_name']))}</strong> | {html.escape(str(p['partner_name']))} | "
                f"deposit {_fmt_currency(p['amount_charged'])} of {_fmt_currency(p['amount_total'])} "
                f"(balance {_fmt_currency(p['balance_due'])} due at ship) | "
                f"ship window: {ship_window}{soon_flag}</li>"
            )
        parts.append("</ul>")
    else:
        parts.append("<p>No outstanding preorders this week.</p>")

    pickups = digest["pickups_awaiting"]
    if pickups:
        parts.append(f"<h3>Pickups awaiting collection ({len(pickups)})</h3>")
        parts.append("<ul>")
        for pk in pickups:
            parts.append(
                f"<li><strong>{html.escape(str(pk['order_name']))}</strong> | {html.escape(str(pk['partner_name']))} | "
                f"ordered {_fmt_date(pk['date_order'])}</li>"
            )
        parts.append("</ul>")
    else:
        parts.append("<p>No pickups awaiting collection.</p>")

    return "\n".join(parts)


def resolve_email_from(
    default_from, company_email, catchall_alias, catchall_domain, config_default_from=""
) -> str:
    """Pick a from_filter-compliant ``email_from`` for outgoing grove_headless
    mail (GOL-2180).

    Odoo falls back to the cron/request author identity
    (``"OdooBot" <odoobot@example.com>``) when a ``mail.mail`` is created with no
    ``email_from``.  On prod that address fails the outgoing server's
    ``from_filter`` (``send.gatheringatthegrove.com``), so resolve an explicit
    From in priority order:

      1. ``ir.config_parameter`` ``mail.default.from``
      2. ``company.email``
      3. ``<mail.catchall.alias>@<mail.catchall.domain>``
      4. ``odoo.conf`` ``email_from`` (``config['email_from']``)

    The odoo.conf ``email_from`` is tried last but is the deployment's
    *versioned* from_filter-compliant sender (``.env EMAIL_FROM`` ->
    ``odoo/odoo.conf`` -> ``odoo.tools.config['email_from']``), configured in
    lockstep with the outgoing server's ``from_filter``.  It is the reliable
    safety net: the three sources above are live-DB state that can be unset or
    drift, in which case returning the odoo.conf value (rather than ``""``)
    keeps the From authorized instead of dropping back to
    ``odoobot@example.com`` -- the exact GOL-2180 failure.

    Pure/stdlib (env-reading lives in ``mail_from.resolve_mail_from``) so it is
    unit-testable without the Odoo runtime.  Returns ``""`` when nothing is
    configured, letting the caller omit ``email_from`` and defer to Odoo.
    """
    for candidate in (default_from, company_email):
        candidate = (candidate or "").strip()
        if candidate:
            return candidate
    alias = (catchall_alias or "").strip()
    domain = (catchall_domain or "").strip()
    if alias and domain:
        return f"{alias}@{domain}"
    config_default_from = (config_default_from or "").strip()
    if config_default_from:
        return config_default_from
    return ""


def _wave_label(entry: dict, esc=None) -> str:
    """Human ship-window label. ``esc`` (e.g. ``html.escape``) is applied to the
    untrusted ``ship_season``; dates are machine-formatted and need no escaping."""
    esc = esc or (lambda s: s)
    start = entry.get("ship_start")
    end = entry.get("ship_end")
    season = esc(entry.get("ship_season") or "")
    if start and end:
        label = f"{_fmt_date(start)} to {_fmt_date(end)}"
        return f"{season} {label}".strip() if season else label
    return "ship window TBD"
