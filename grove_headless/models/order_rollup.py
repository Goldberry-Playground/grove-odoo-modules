"""Weekly order/preorder rollup cron (GOL-1978).

Scheduled via data/order_rollup_cron.xml (ir.cron, Monday 08:00 UTC).
Queries sale.order for active companies and emits a digest to the merchant
inbox + the ops Discord webhook.  Aggregation logic lives in order_digest.py
(pure/stdlib-testable).
"""

import logging
import os
from datetime import datetime, timedelta

import requests
from odoo import api, fields, models

from . import order_digest as od
from .shipping_calendar import _next_wave, usda_zone_for_zip

_logger = logging.getLogger(__name__)

# How many days back the "orders placed this week" window covers.
_ROLLUP_PERIOD_DAYS = 7

# The storefront shipping-charge line's product code (mirrors
# controllers/main.SHIPPING_PRODUCT_CODE). A confirmed order with no such line
# was farm pickup — pickup is the one legitimate $0-shipping fulfillment
# (GOL-1057), so absence of the line is the reliable pickup signal.
_SHIPPING_PRODUCT_CODE = "GROVE-SHIP"


class SaleOrderRollup(models.AbstractModel):
    """Container for the ir.cron method; no table of its own."""

    _name = "grove.order.rollup"
    _description = "Weekly order/preorder rollup digest"

    @api.model
    def _cron_send_weekly_digest(self):
        """Called weekly by ir.cron.  Iterates active companies and emits a
        digest for each; errors on one company do not abort the others."""
        today = fields.Date.context_today(self)
        for company in self.env["res.company"].sudo().search([("active", "=", True)]):
            try:
                self._send_digest_for_company(company, today)
            except Exception:
                _logger.error(
                    "grove.order.rollup: digest failed for company %s",
                    company.name,
                    exc_info=True,
                )

    def _send_digest_for_company(self, company, today):
        """Build and emit the digest for a single company."""
        orders = self._fetch_orders(company)
        if not orders:
            _logger.info("grove.order.rollup: no orders found for %s — skipping", company.name)
            return
        pickings = self._fetch_pickings(company, today)

        digest = od.build_digest(
            orders,
            pickings=pickings,
            today=today,
            period_days=_ROLLUP_PERIOD_DAYS,
            next_wave_fn=_next_wave,
            usda_zone_fn=usda_zone_for_zip,
        )

        subject = f"Weekly order rollup — {company.name}"
        html_body = od.render_digest_html(digest)
        text_body = od.render_digest_text(digest)

        self._mail_digest(company, subject, html_body)
        self._discord_digest(company, text_body)
        _logger.info(
            "grove.order.rollup: sent digest for %s — %d orders, %d preorders, %d pickups",
            company.name,
            digest["orders_placed"],
            digest["preorder_count"],
            digest["pickup_count"],
        )

    def _fetch_orders(self, company) -> list[dict]:
        """Return all orders relevant to the digest for *company*.

        Confirmed:       all non-cancelled sale.orders for this company.
        We deliberately include all-time orders for the outstanding-preorder
        and pickup sections (those are unbounded), and let build_digest filter
        the 7-day window for the placed/revenue/units-ordered metrics.
        """
        env = self.env["sale.order"].sudo()
        orders = env.search(
            [
                ("company_id", "=", company.id),
                ("state", "not in", ("draft", "cancel")),
            ]
        )
        result = []
        for o in orders:
            partner_ship = o.partner_shipping_id
            # Farm pickup is the absence of a storefront shipping-charge line
            # (pickup is the one legitimate $0-shipping fulfillment, GOL-1057),
            # mirroring controllers/main.is_ship_order. Also count physical tree
            # units for the units-ordered metric (units-shipped is measured
            # separately from stock.picking in _fetch_pickings).
            is_pickup = True
            units = 0
            for line in o.order_line:
                product = line.product_id
                if line.display_type or not product:
                    continue
                if product.default_code == _SHIPPING_PRODUCT_CODE:
                    is_pickup = False
                    continue
                if product.product_tmpl_id.type == "service":
                    continue
                units += int(line.product_uom_qty)
            # The ship window keys off the destination zone: the FARM's zone for
            # pickup (we lift on our schedule, not the buyer's, GOL-1669), else
            # the customer's shipping ZIP.
            if is_pickup:
                window_zip = self._farm_pickup_zip(company)
            else:
                window_zip = partner_ship.zip if partner_ship else ""
            result.append(
                {
                    "id": o.id,
                    "name": o.name,
                    "date_order": o.date_order.date() if o.date_order else None,
                    "amount_total": o.amount_total,
                    "grove_checkout_status": o.grove_checkout_status,
                    "grove_delivery_status": o.grove_delivery_status,
                    "grove_preorder_variant_ids": o.grove_preorder_variant_ids,
                    "partner_name": o.partner_id.name or "",
                    "partner_shipping_zip": (window_zip or "").strip(),
                    "is_pickup": is_pickup,
                    "units": units,
                }
            )
        return result

    def _fetch_pickings(self, company, today) -> list[dict]:
        """Outgoing stock.picking records completed within the rollup window,
        mapped to ``{date_done, units}``.

        Units shipped is measured here — at the picking, on ``date_done`` — not
        from orders placed in the window (GOL-1978 review, Josh's decision), so
        a preorder booked this week but not yet fulfilled does not inflate the
        shipped figure. The ``date_done >=`` filter is a coarse prefilter; the
        pure aggregator (order_digest.build_digest) applies the exact window.
        """
        period_start = today - timedelta(days=_ROLLUP_PERIOD_DAYS)
        start_dt = datetime.combine(period_start, datetime.min.time())
        pickings = (
            self.env["stock.picking"]
            .sudo()
            .search(
                [
                    ("company_id", "=", company.id),
                    ("picking_type_id.code", "=", "outgoing"),
                    ("state", "=", "done"),
                    ("date_done", ">=", start_dt),
                ]
            )
        )
        result = []
        for pk in pickings:
            units = 0
            for move in pk.move_ids:
                product = move.product_id
                # Skip service lines; count only physical tree units actually moved.
                if not product or product.product_tmpl_id.type == "service":
                    continue
                units += int(move.quantity)
            result.append(
                {
                    "date_done": pk.date_done.date() if pk.date_done else None,
                    "units": units,
                }
            )
        return result

    def _farm_pickup_zip(self, company):
        """Farm origin ZIP (warehouse -> company partner -> config param), the
        same precedence controllers/main._farm_pickup_zip uses."""
        warehouse = self.env["stock.warehouse"].sudo()
        wh = warehouse.search([("company_id", "=", company.id)], limit=1) if company else warehouse.browse()
        for candidate in (
            wh.partner_id.zip if wh and wh.partner_id else None,
            company.partner_id.zip if company and company.partner_id else None,
            self.env["ir.config_parameter"].sudo().get_param("grove_headless.farm_pickup_zip"),
        ):
            candidate = (candidate or "").strip()
            if candidate:
                return candidate
        return None

    def _mail_digest(self, company, subject, html_body):
        """Send the HTML digest to the company's merchant email (best-effort)."""
        merchant_email = company.email
        if not merchant_email:
            _logger.warning("grove.order.rollup: company %s has no email — skipping mail", company.name)
            return
        try:
            self.env["mail.mail"].sudo().create(
                {
                    "subject": subject,
                    "email_to": merchant_email,
                    "body_html": html_body,
                    "auto_delete": True,
                }
            ).send()
        except Exception:
            _logger.warning("grove.order.rollup: mail failed for %s", company.name, exc_info=True)

    def _discord_digest(self, company, text_body):
        """Post the text digest to DISCORD_OPS_WEBHOOK_URL (best-effort)."""
        url = os.environ.get("DISCORD_OPS_WEBHOOK_URL", "")
        if not url:
            return
        try:
            requests.post(url, json={"content": text_body[:2000]}, timeout=10)
        except Exception:
            _logger.warning("grove.order.rollup: Discord notify failed", exc_info=True)
