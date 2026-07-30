"""grove.publish.event — audit + replay ledger for storefront publish webhooks.

One row per publish delivery (GOL-985). Emitting an event signs a small JSON
payload with the tenant's HMAC secret and POSTs it to that tenant's grove-sites
webhook (see `grove_publish.py` for the wire contract). Every attempt is
recorded here so publishes are auditable and replayable: a failed delivery keeps
its row (state=failed) and can be re-sent from the form with the SAME
`delivery_id`, which the receiver uses to dedupe.

Secrets/URLs are read from the environment (never stored in the DB), one pair
per tenant, matching the existing `SHIPPO_API_KEY` env pattern:

    GROVE_PUBLISH_WEBHOOK_URL_<TENANT>      (falls back to GROVE_PUBLISH_WEBHOOK_URL)
    GROVE_PUBLISH_WEBHOOK_SECRET_<TENANT>   (falls back to GROVE_PUBLISH_WEBHOOK_SECRET)

where <TENANT> is the uppercased tenant slug (GOLDBERRY / GGG / NURSERY). The
per-tenant secret is the same key grove-sites is wired with (checkout-flip
follow-up). Delivery fails closed with a clear UserError if either is missing.
"""

import json
import logging
import os
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError

try:
    from . import grove_publish
except ImportError:  # loaded standalone (tests import by file path)
    import importlib.util as _ilu
    import os as _os

    _gp_path = _os.path.join(_os.path.dirname(__file__), "grove_publish.py")
    _spec = _ilu.spec_from_file_location("grove_publish", _gp_path)
    grove_publish = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(grove_publish)

_logger = logging.getLogger(__name__)

EVENT_GUIDE_PUBLISH = "guide.publish"

# Truncate stored response bodies — this is a debugging aid, not a mirror.
_RESPONSE_LIMIT = 2000


class GrovePublishEvent(models.Model):
    _name = "grove.publish.event"
    _description = "Storefront publish webhook delivery (audit + replay)"
    _order = "create_date desc"
    _log_access = True
    _rec_name = "delivery_id"

    delivery_id = fields.Char(
        required=True,
        index=True,
        copy=False,
        help="Opaque id, unique per logical publish. Sent as X-Grove-Delivery; "
        "the receiver dedupes on it, so a retry reuses the same value.",
    )
    event_type = fields.Char(required=True, index=True, default=EVENT_GUIDE_PUBLISH)
    tenant = fields.Char(index=True, help="Tenant slug: goldberry / ggg / nursery.")
    company_id = fields.Many2one("res.company", ondelete="set null")
    product_tmpl_id = fields.Many2one("product.template", ondelete="set null", string="Product")
    endpoint_url = fields.Char(help="grove-sites webhook the event was POSTed to.")
    payload = fields.Text(help="Exact JSON bytes that were signed and sent.")
    signature = fields.Char(help="X-Grove-Signature-256 value we sent.")
    state = fields.Selection(
        [("pending", "Pending"), ("delivered", "Delivered"), ("failed", "Failed")],
        default="pending",
        required=True,
        index=True,
    )
    http_status = fields.Integer(help="Response status from the receiver (blank if never reached).")
    response_body = fields.Text(help="Truncated response body, for debugging a rejection.")
    error = fields.Text(help="Failure reason (transport error or non-2xx).")

    _sql_constraints = [
        ("delivery_id_uniq", "unique(delivery_id)", "This publish delivery id has already been recorded."),
    ]

    # ── Config resolution ──────────────────────────────────────────────
    @api.model
    def _tenant_slug_for_company(self, company):
        """Resolve a company to its storefront tenant slug, or None.

        Delegates to the website model so the slug↔name mapping stays in one
        place (models/website.py). A company with no matching website (or a
        renamed one) returns None and the caller fails loud.
        """
        website = self.env["website"].sudo().search([("company_id", "=", company.id)], limit=1)
        return website.grove_tenant_slug() if website else None

    @api.model
    def _webhook_config(self, tenant):
        """(url, secret) for a tenant slug from the environment. Either may be ''."""
        key = tenant.upper()
        url = os.environ.get(f"GROVE_PUBLISH_WEBHOOK_URL_{key}") or os.environ.get(
            "GROVE_PUBLISH_WEBHOOK_URL", ""
        )
        secret = os.environ.get(f"GROVE_PUBLISH_WEBHOOK_SECRET_{key}") or os.environ.get(
            "GROVE_PUBLISH_WEBHOOK_SECRET", ""
        )
        return url, secret

    # ── Payload ────────────────────────────────────────────────────────
    @api.model
    def _build_payload(self, product_tmpl, *, event_type, delivery_id, tenant, occurred_at):
        """The JSON the receiver revalidates from. `slug` is the storefront route key."""
        return {
            "event": event_type,
            "delivery_id": delivery_id,
            "occurred_at": occurred_at,
            "tenant": tenant,
            "kind": "product",
            "product": {
                "id": product_tmpl.id,
                "template_id": product_tmpl.id,
                "slug": product_tmpl.grove_slug or "",
                "name": product_tmpl.name or "",
            },
            "guide_ready": bool(product_tmpl.grove_guide_ready),
        }

    # ── Emit / deliver ─────────────────────────────────────────────────
    @api.model
    def publish_guide(self, product_tmpl):
        """Emit a `guide.publish` event for a product and deliver it. Returns the row."""
        return self._emit(product_tmpl, EVENT_GUIDE_PUBLISH)

    @api.model
    def _emit(self, product_tmpl, event_type):
        product_tmpl.ensure_one()
        company = product_tmpl.company_id or self.env.company
        tenant = self._tenant_slug_for_company(company)
        if not tenant:
            raise UserError(
                _("No storefront tenant is mapped to company '%s'. Cannot publish.") % company.display_name
            )
        url, secret = self._webhook_config(tenant)
        if not url or not secret:
            key = tenant.upper()
            raise UserError(
                _(
                    "Publish webhook is not configured for tenant '%(tenant)s'. Set "
                    "GROVE_PUBLISH_WEBHOOK_URL_%(key)s and GROVE_PUBLISH_WEBHOOK_SECRET_%(key)s "
                    "on the Odoo server."
                )
                % {"tenant": tenant, "key": key}
            )

        delivery_id = uuid.uuid4().hex
        occurred_at = fields.Datetime.now().isoformat() + "Z"
        payload = self._build_payload(
            product_tmpl,
            event_type=event_type,
            delivery_id=delivery_id,
            tenant=tenant,
            occurred_at=occurred_at,
        )
        event = self.create(
            {
                "delivery_id": delivery_id,
                "event_type": event_type,
                "tenant": tenant,
                "company_id": company.id,
                "product_tmpl_id": product_tmpl.id,
                "endpoint_url": url,
                "payload": grove_publish.serialize(payload).decode("utf-8"),
                "state": "pending",
            }
        )
        event._deliver(url, secret, payload)
        return event

    def _deliver(self, url, secret, payload):
        """POST the payload, recording the outcome on this row. Never raises."""
        self.ensure_one()
        try:
            body, signature, response = grove_publish.deliver(
                url,
                secret,
                payload,
                event_type=self.event_type,
                delivery_id=self.delivery_id,
                tenant=self.tenant or "",
            )
        except grove_publish.PublishDeliveryError as exc:
            _logger.warning("publish.event %s transport failure: %s", self.delivery_id, exc)
            self.write({"state": "failed", "error": str(exc)})
            return self

        ok = 200 <= response.status_code < 300
        self.write(
            {
                "payload": body.decode("utf-8"),
                "signature": signature,
                "http_status": response.status_code,
                "response_body": (getattr(response, "text", "") or "")[:_RESPONSE_LIMIT],
                "state": "delivered" if ok else "failed",
                "error": False if ok else _("Receiver returned HTTP %s") % response.status_code,
            }
        )
        if not ok:
            _logger.warning(
                "publish.event %s rejected: HTTP %s", self.delivery_id, response.status_code
            )
        return self

    def action_retry(self):
        """Re-send stored events with their original delivery_id (idempotent replay)."""
        for event in self:
            if not event.payload:
                raise UserError(_("Nothing to replay: this event has no stored payload."))
            if not event.tenant:
                raise UserError(_("Cannot replay: this event has no tenant."))
            url, secret = self._webhook_config(event.tenant)
            if not url or not secret:
                raise UserError(
                    _("Publish webhook is not configured for tenant '%s'.") % event.tenant
                )
            event.write({"state": "pending", "error": False})
            # Reuse the exact stored payload so the delivery_id (and therefore the
            # receiver's dedupe key) stays stable across retries.
            event._deliver(url, secret, json.loads(event.payload))
        return True
