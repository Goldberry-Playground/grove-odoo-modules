"""Weekly order/preorder rollup cron (GOL-1978).

Scheduled via data/order_rollup_cron.xml (ir.cron, Monday 08:00 UTC).
Queries sale.order for active companies and emits a digest to the merchant
inbox + the ops Discord webhook.  Aggregation logic lives in order_digest.py
(pure/stdlib-testable).
"""

import logging
import os

import requests
from odoo import api, fields, models

from . import order_digest as od
from .shipping_calendar import _next_wave, usda_zone_for_zip

_logger = logging.getLogger(__name__)

# How many days back the "orders placed this week" window covers.
_ROLLUP_PERIOD_DAYS = 7

# Delivery statuses that indicate an order has shipped / been collected.
_TERMINAL_DELIVERY = {"label_purchased", "shipped", "transit", "delivered", "collected"}


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

        digest = od.build_digest(
            orders,
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
        the 7-day window for the placed/revenue/shipped metrics.
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
            is_pickup = (o.grove_delivery_status or "").startswith("pickup") or (
                # Pickup orders have no tracking numbers and no delivery charge;
                # detect by absence of shipping charge line + no tracking field.
                not o.grove_tracking_numbers
                and any(
                    (line.product_id.type == "service" and "pickup" in (line.name or "").lower())
                    for line in o.order_line
                )
            )
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
                    "partner_shipping_zip": partner_ship.zip or "" if partner_ship else "",
                    "is_pickup": is_pickup,
                }
            )
        return result

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
