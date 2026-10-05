"""Customer-email delivery log (GOL-2903).

One row per customer email grove_headless sends, written at every send site
through the single :meth:`log_and_send` choke point so a new email kind cannot
forget to log. The outgoing message is tagged with ``X-Mailgun-Variables``
carrying this row's id, which Mailgun echoes on every event; the webhook
(controllers/mailgun_webhook.py) then advances the row's status — delivered /
opened / failed / blocked / complained — idempotently and without ever
regressing (a late "delivered" can't clobber an "opened"), and pings ops on
Discord once for any failed / blocked / complained outcome.
"""

import logging
import os
from datetime import datetime, timezone

import requests
from odoo import api, fields, models

from . import mailgun_gateway

_logger = logging.getLogger(__name__)

_KIND_SELECTION = [
    ("receipt", "Order receipt"),
    ("deposit", "Preorder deposit explainer"),
    ("shipped", "Shipped notice"),
    ("out_for_delivery", "Out-for-delivery notice"),
    ("delivered", "Delivered notice"),
    ("dunning", "Balance-due (dunning)"),
    ("oversell_apology", "Oversell apology"),
]

_STATUS_SELECTION = [
    ("pending", "Pending"),
    ("sent", "Sent"),
    ("delivered", "Delivered"),
    ("opened", "Opened (approx.)"),
    ("blocked", "Blocked / suppressed"),
    ("failed", "Failed"),
    ("complained", "Complained"),
]


class GroveEmailLog(models.Model):
    """Delivery record for one customer email tied to a sale order."""

    _name = "grove.email.log"
    _description = "Customer Email Delivery Log"
    _order = "create_date desc, id desc"
    _rec_name = "subject"

    order_id = fields.Many2one("sale.order", string="Order", ondelete="cascade", index=True, required=True)
    company_id = fields.Many2one("res.company", string="Company", index=True)
    kind = fields.Selection(_KIND_SELECTION, string="Email", required=True, index=True)
    recipient = fields.Char(string="Recipient", index=True)
    subject = fields.Char(string="Subject")
    sent_at = fields.Datetime(string="Sent")

    status = fields.Selection(_STATUS_SELECTION, string="Status", default="pending", required=True, index=True)
    # Approximate: Apple Mail pre-loads images and image-blocking clients report
    # none, so "opened" under-counts. Delivered/failed are the reliable signals
    # (GOL-2903 notes). Surfaced in the view label, not just here.
    delivered_at = fields.Datetime(string="Delivered")
    opened_at = fields.Datetime(string="Opened")
    failed_at = fields.Datetime(string="Failed/blocked")
    complained_at = fields.Datetime(string="Complained")
    last_event_at = fields.Datetime(string="Last event")

    failure_severity = fields.Selection(
        [("temporary", "Temporary"), ("permanent", "Permanent")], string="Failure severity"
    )
    failure_reason = fields.Text(string="Failure reason")

    mailgun_message_id = fields.Char(string="Mailgun message id", index=True)
    alerted = fields.Boolean(string="Ops alerted", default=False, help="A Discord alert has been posted for this row.")

    # ── Send side (§1) ────────────────────────────────────────────────────

    @api.model
    def log_and_send(self, order, kind, recipient, subject, *, mail_vals=None, template=None, template_res_id=None):
        """Log, tag, and send one customer email. Returns the log row.

        Exactly one of ``mail_vals`` (a ``mail.mail`` create dict) or ``template``
        (a ``mail.template`` record, sent via ``send_mail``) must be given. The
        row is created first so its id can ride the message as an
        ``X-Mailgun-Variables`` header; Mailgun echoes that back on every event,
        giving the webhook a durable match key independent of the SMTP Message-Id
        (which Mailgun replaces).

        Best-effort end to end: a send exception is recorded as a permanent
        ``failed`` on the row (and alerted) but never re-raised — every call site
        treats customer mail as non-fatal, the payment/fulfilment already stand.
        """
        if kind not in dict(_KIND_SELECTION):
            raise ValueError(f"unknown customer-email kind {kind!r}")
        log = self.sudo().create(
            {
                "order_id": order.id,
                "company_id": order.company_id.id,
                "kind": kind,
                "recipient": recipient,
                "subject": subject,
                "status": "pending",
            }
        )
        headers_literal = repr({"X-Mailgun-Variables": mailgun_gateway.variables_header(log.id, order.name)})
        try:
            if template is not None:
                values = dict(mail_vals or {})
                values["headers"] = headers_literal
                template.sudo().send_mail(
                    template_res_id if template_res_id is not None else order.id,
                    force_send=True,
                    email_values=values,
                )
            else:
                values = dict(mail_vals or {})
                values["headers"] = headers_literal
                self.env["mail.mail"].sudo().create(values).send()
            log.write({"status": "sent", "sent_at": fields.Datetime.now()})
        except Exception:  # noqa: BLE001 — customer mail is best-effort, never fatal
            _logger.warning("grove.email.log: send failed for %s (%s)", order.name, kind, exc_info=True)
            log.write(
                {
                    "status": "failed",
                    "failure_severity": "permanent",
                    "failure_reason": "Local send error — see Odoo log",
                    "failed_at": fields.Datetime.now(),
                }
            )
            log._maybe_alert()
        return log

    # ── Receive side (§2) ─────────────────────────────────────────────────

    @api.model
    def match_event(self, parsed):
        """Find the log row a parsed Mailgun event belongs to.

        Primary key is the ``grove_log_id`` we round-tripped through
        ``X-Mailgun-Variables``. Falls back to the most recent not-yet-terminal
        row for the event's recipient so a message whose header was stripped (or
        sent before this shipped) can still resolve. Returns an empty recordset
        if nothing matches.
        """
        if parsed.get("log_id"):
            row = self.sudo().browse(parsed["log_id"]).exists()
            if row:
                return row
        recipient = parsed.get("recipient")
        if recipient:
            return self.sudo().search(
                [("recipient", "=", recipient), ("status", "in", ("pending", "sent", "delivered"))],
                order="create_date desc",
                limit=1,
            )
        return self.sudo().browse()

    def apply_event(self, parsed):
        """Apply a parsed Mailgun event to this row. Idempotent, never regresses.

        Returns ``True`` if the event advanced the status to a fresh alert-worthy
        state (failed / blocked / complained) and an ops alert was posted.
        """
        self.ensure_one()
        new_status = parsed.get("status")
        vals = {"last_event_at": fields.Datetime.now()}
        if parsed.get("mailgun_message_id") and not self.mailgun_message_id:
            vals["mailgun_message_id"] = parsed["mailgun_message_id"]
        if not new_status:
            self.write(vals)
            return False

        new_rank = mailgun_gateway.status_rank(new_status, parsed.get("severity"))
        cur_rank = mailgun_gateway.status_rank(self.status, self.failure_severity)
        if new_rank <= cur_rank:
            # Duplicate delivery or an out-of-order event — record that we saw it
            # but never move the status backwards (§2).
            self.write(vals)
            return False

        event_dt = self._event_datetime(parsed.get("event_timestamp"))
        vals["status"] = new_status
        if new_status == "delivered":
            vals["delivered_at"] = event_dt
        elif new_status == "opened":
            vals["opened_at"] = event_dt
        elif new_status in ("failed", "blocked"):
            vals["failed_at"] = event_dt
            vals["failure_severity"] = parsed.get("severity") or "permanent"
            vals["failure_reason"] = parsed.get("failure_reason") or ""
        elif new_status == "complained":
            vals["complained_at"] = event_dt
        self.write(vals)

        if new_status in mailgun_gateway.ALERT_STATUSES:
            return self._maybe_alert()
        return False

    @staticmethod
    def _event_datetime(epoch):
        """Mailgun's event timestamp (float epoch seconds) → naive UTC Datetime.

        Odoo stores datetimes as naive UTC, so strip the tzinfo after converting.
        """
        if not epoch:
            return fields.Datetime.now()
        try:
            return datetime.fromtimestamp(float(epoch), tz=timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError, OSError):
            return fields.Datetime.now()

    def _maybe_alert(self):
        """Post one ops Discord alert for a failed/blocked/complained email.

        Guarded by ``alerted`` so a retry or a later same-status event never
        double-pings. Best-effort — a missing webhook or a failed POST never
        breaks webhook processing. Prefers the ops channel (this is an
        observability alert, not an order summary), falling back to the orders
        webhook so a missing ops URL still surfaces somewhere."""
        self.ensure_one()
        if self.alerted:
            return False
        url = os.environ.get("DISCORD_OPS_WEBHOOK_URL", "") or os.environ.get("DISCORD_ORDERS_WEBHOOK_URL", "")
        self.write({"alerted": True})
        if not url:
            return False
        label = dict(_STATUS_SELECTION).get(self.status, self.status)
        reason = f" — {self.failure_reason}" if self.failure_reason else ""
        order_ref = self.order_id.name or f"order {self.order_id.id}"
        message = (
            f":warning: Customer email **{label}** for {order_ref} "
            f"({dict(_KIND_SELECTION).get(self.kind, self.kind)} → {self.recipient}){reason}"
        )
        try:
            requests.post(url, json={"content": message[:2000]}, timeout=10)
        except Exception:  # noqa: BLE001 — alert is best-effort
            _logger.warning("grove.email.log: Discord alert failed for %s", order_ref, exc_info=True)
        return True


class SaleOrder(models.Model):
    """Expose this order's customer emails for the "Emails" tab (GOL-2903 §3)."""

    _inherit = "sale.order"

    grove_email_log_ids = fields.One2many("grove.email.log", "order_id", string="Customer emails")
