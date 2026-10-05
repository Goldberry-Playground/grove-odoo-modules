"""Mailgun event webhook receiver (GOL-2903 §2).

Mailgun POSTs a signed JSON body for every delivery event on
``send.gatheringatthegrove.com``. We verify the HMAC signature against the
account's HTTP webhook signing key (rejecting anything unsigned or stale), map
the event to our status vocabulary, and advance the matching grove.email.log
row — idempotently and without regressing a status.

The signing key is read from the process environment (``mailgun_webhook_signing_key``,
lowercase to match the stripe_*_secret convention) which Terra wires through the
compose ``environment:`` block / credential broker. A single sending account
means a single key — no per-tenant fan-out like the Stripe webhook.
"""

import json
import logging
import os

from odoo import http
from odoo.http import Response, request

from ..models import mailgun_gateway

_logger = logging.getLogger(__name__)

MAILGUN_SIGNING_KEY_ENV = "mailgun_webhook_signing_key"


def _json_response(data, status=200):
    return Response(json.dumps(data, default=str), status=status, content_type="application/json")


class GroveMailgunWebhook(http.Controller):
    @http.route(
        "/grove/api/v1/mailgun/webhook",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def mailgun_webhook(self, **_kwargs):
        """Receive Mailgun delivery-event webhooks.

        ``type="http"`` (not "json") so Mailgun sees real HTTP status codes: a
        4xx halts retries (forged / malformed → never reprocessed), a 5xx lets
        Mailgun retry a transient handler error. Signature-verified against the
        signature triple in the body before any side effect.
        """
        signing_key = os.environ.get(MAILGUN_SIGNING_KEY_ENV, "")
        if not signing_key:
            _logger.warning("Mailgun webhook rejected: no signing key configured")
            return _json_response({"error": "signature verification failed"}, status=400)

        raw = request.httprequest.get_data() or b""
        try:
            payload = json.loads(raw or b"{}")
        except (json.JSONDecodeError, ValueError):
            return _json_response({"error": "bad json"}, status=400)

        signature = payload.get("signature") if isinstance(payload, dict) else None
        signature = signature or {}
        try:
            mailgun_gateway.verify_signature(
                signing_key,
                signature.get("timestamp"),
                signature.get("token"),
                signature.get("signature"),
            )
        except mailgun_gateway.MailgunError as exc:
            _logger.warning("Mailgun webhook rejected: %s", exc)
            return _json_response({"error": "signature verification failed"}, status=400)

        try:
            parsed = mailgun_gateway.parse_event(payload)
        except mailgun_gateway.MailgunError as exc:
            return _json_response({"error": str(exc)}, status=400)

        env = request.env
        row = env["grove.email.log"].sudo().match_event(parsed)
        if not row:
            # No row owns this event (non-customer email, or sent before this
            # shipped — no backfill). Ack so Mailgun stops retrying.
            return _json_response({"ok": True, "matched": False})

        try:
            row.apply_event(parsed)
        except Exception:  # noqa: BLE001
            _logger.exception("Mailgun webhook apply failed for grove.email.log %s", row.id)
            env.cr.rollback()
            return _json_response({"error": "handler error"}, status=500)

        return _json_response({"ok": True, "status": row.status})
