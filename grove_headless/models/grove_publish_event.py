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
# Emitted when a product crosses an availability boundary (GOL-1896) so the
# grove-sites `/shop` ISR grid stops advertising a stale "In stock". Rides the
# same signed/deduped/replayable channel as guide.publish; the receiver
# revalidates the same two paths.
EVENT_PRODUCT_AVAILABILITY = "product.availability"

# Truncate stored response bodies — this is a debugging aid, not a mirror.
_RESPONSE_LIMIT = 2000

# Storm guard (GOL-1896): a bulk import or mass inventory adjustment can flip
# hundreds of products' availability in one transaction. We coalesce to one
# event per template (earliest→final state), then cap how many we actually send
# per transaction; anything past the cap degrades to the (shortened) `/shop` ISR
# window rather than firing a webhook storm at the receiver.
_AVAILABILITY_EMIT_CAP = 50

# Transaction-scoped scratch keys on `cr.precommit.data` (cleared when the flush
# runs, i.e. at commit — or manually in tests).
_AVAIL_BEFORE_KEY = "grove_availability_before"
_AVAIL_REGISTERED_KEY = "grove_availability_flush_registered"


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

    # Odoo 19 dropped the `_sql_constraints` list attribute (warns, never creates
    # the constraint). Declared with `models.Constraint` so the UNIQUE(delivery_id)
    # is actually created on `-u grove_headless`, keeping the audit/replay ledger
    # one row per logical publish.
    _delivery_id_uniq = models.Constraint(
        "unique(delivery_id)",
        "This publish delivery id has already been recorded.",
    )

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
        url = os.environ.get(f"GROVE_PUBLISH_WEBHOOK_URL_{key}") or os.environ.get("GROVE_PUBLISH_WEBHOOK_URL", "")
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

    # ── Availability transitions (GOL-1896) ────────────────────────────
    # Storefront availability is three storefront-visible booleans:
    #   * in stock          (qty_available > 0, read in the product's company)
    #   * sale_ok           (purchasable vs "Coming soon")
    #   * website_published  (present vs absent on /shop)
    # The stock.quant and product.template write hooks call
    # `note_availability_candidates` with the pre-mutation state; a single
    # transaction-commit flush emits `product.availability` for the templates
    # that actually crossed a boundary. Emitting on the *transition* (not every
    # write) is the whole point: a confirmed order moves stock on every line, so
    # a naive per-write emit would storm the receiver — we fire once per template
    # that changed, computed earliest→final state.
    @api.model
    def _availability_signature(self, templates):
        """{template_id: (in_stock, sale_ok, website_published)} for `templates`.

        Read in each template's own company so the boolean matches what that
        tenant's storefront sees. sudo: this runs from low-level stock hooks that
        may not carry catalog read rights, and the availability booleans are not
        sensitive.
        """
        signature = {}
        for template in templates.sudo():
            company = template.company_id or self.env.company
            scoped = template.with_company(company)
            signature[template.id] = (
                (scoped.qty_available or 0.0) > 0.0,
                bool(template.sale_ok),
                bool(template.website_published),
            )
        return signature

    @api.model
    def note_availability_candidates(self, templates):
        """Record `templates`' pre-mutation availability and schedule a flush.

        Call this BEFORE the mutation (stock write / template write). Only the
        first signature seen for a template this transaction is kept, so a
        multi-write transaction still emits at most one event per template,
        computed earliest→final state. Safe to call with an empty recordset.
        """
        templates = templates.exists()
        if not templates:
            return
        data = self.env.cr.precommit.data
        before = data.setdefault(_AVAIL_BEFORE_KEY, {})
        missing = templates.filtered(lambda t: t.id not in before)
        if missing:
            before.update(self._availability_signature(missing))
        if not data.get(_AVAIL_REGISTERED_KEY):
            data[_AVAIL_REGISTERED_KEY] = True
            # Flush at commit so a rolled-back stock change emits nothing, and so
            # the webhook round-trip never blocks the business logic (it runs
            # after all SQL is staged). sudo() binds the flush to a system env —
            # audit rows are system-owned, like the guide-publish path.
            self.env.cr.precommit.add(self.sudo()._flush_availability_events)

    def _flush_availability_events(self):
        """Emit `product.availability` for every noted template that changed.

        Runs once per transaction (at commit, or directly in tests). Idempotent:
        pops the scratch state, so a second call is a no-op.
        """
        data = self.env.cr.precommit.data
        before = data.pop(_AVAIL_BEFORE_KEY, None)
        data.pop(_AVAIL_REGISTERED_KEY, None)
        if not before:
            return
        templates = self.env["product.template"].browse(sorted(before)).exists()
        # The stock write already happened; drop the cached compute so the
        # after-signature reflects the new on-hand quantity.
        templates.invalidate_recordset(["qty_available"])
        after = self._availability_signature(templates)
        changed = [t for t in templates if after.get(t.id, before[t.id]) != before[t.id]]
        for index, template in enumerate(changed):
            if index >= _AVAILABILITY_EMIT_CAP:
                _logger.warning(
                    "product.availability: %s templates changed availability in one "
                    "transaction; emitted the first %s and left the remaining %s to the "
                    "/shop ISR window (GOL-1896).",
                    len(changed),
                    _AVAILABILITY_EMIT_CAP,
                    len(changed) - _AVAILABILITY_EMIT_CAP,
                )
                break
            self.sudo()._emit_safe(template, EVENT_PRODUCT_AVAILABILITY)

    def _emit_safe(self, product_tmpl, event_type):
        """`_emit` that never raises — for automatic emits behind a user's write.

        A misconfigured tenant (no webhook URL/secret) or any unexpected error
        must not abort or 500 the stock/template write that triggered it; the
        storefront simply degrades to its ISR window. Delivery failures are
        already non-raising (see `_deliver`) and keep the row for replay.
        """
        try:
            return self._emit(product_tmpl, event_type)
        except UserError as exc:
            _logger.info("%s emit skipped for template %s: %s", event_type, product_tmpl.id, exc)
        except Exception:  # pragma: no cover - defensive: never break the write
            _logger.exception("%s emit failed for template %s", event_type, product_tmpl.id)
        return self.browse()

    @api.model
    def _emit(self, product_tmpl, event_type):
        product_tmpl.ensure_one()
        company = product_tmpl.company_id or self.env.company
        tenant = self._tenant_slug_for_company(company)
        if not tenant:
            raise UserError(_("No storefront tenant is mapped to company '%s'. Cannot publish.") % company.display_name)
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
            _logger.warning("publish.event %s rejected: HTTP %s", self.delivery_id, response.status_code)
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
                raise UserError(_("Publish webhook is not configured for tenant '%s'.") % event.tenant)
            event.write({"state": "pending", "error": False})
            # Reuse the exact stored payload so the delivery_id (and therefore the
            # receiver's dedupe key) stays stable across retries.
            event._deliver(url, secret, json.loads(event.payload))
        return True
