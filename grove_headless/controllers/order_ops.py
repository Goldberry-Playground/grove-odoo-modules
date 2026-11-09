"""Order action routes for the Discord bridge.

Each route runs one order action for an operator and answers ``{"card": ...}``
built by the same helper as the alert post, so the bridge re-renders from what
Odoo says. Bearer auth and company scoping match ``order_mark_shipped``; the
action runs under ``sudo()`` as the service user, which passes the operator guard
(``_grove_require_order_operator``). The bridge checks DISCORD_ORDER_OPERATOR_IDS
before forwarding, and sends the Discord user it checked as ``actor``.

A ``UserError`` or ``ValueError`` is a 422 carrying its message, which the bridge
shows to the operator as written. Nothing else escapes as a traceback.

``label-batches/<id>/mark-shipped`` is the one route that answers 409 for a refusal
(a `UserError`, nothing changed) and commits each order as it ships, because every
shipment emails the customer and cannot be recalled.
"""

import json
import logging

from odoo import fields, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models.order_lanes import parse_hold_date
from .main import ORDER_CARD_TZ, _json_response, _safe_order_card

_logger = logging.getLogger(__name__)


class OrderOpsController(http.Controller):
    def _run_order_action(self, order_id, action):
        """Parse the body, find the website company's order, run ``action(order,
        payload, actor)`` and answer with the card, plus any keys ``action`` returns
        in a dict. ``action`` may raise."""
        try:
            payload = json.loads(request.httprequest.data or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        current_company = request.website.company_id
        order = (
            request.env["sale.order"]
            .sudo()
            .with_company(current_company)
            .with_context(tz=ORDER_CARD_TZ)
            .search([("id", "=", order_id), ("company_id", "=", current_company.id)], limit=1)
        )
        if not order:
            return _json_response({"error": "Order not found"}, status=404)

        actor = payload.get("actor")
        if not isinstance(actor, str) or not actor.strip():
            return _json_response({"error": "actor is required"}, status=422)

        try:
            # A savepoint, so an unexpected failure leaves nothing half-written
            # (the error response below is a normal return and would commit).
            with request.env.cr.savepoint():
                result = action(order, payload, actor.strip())
        except AccessError:
            # Before UserError: AccessError is a subclass of it.
            _logger.warning("Order action refused for order %s", order_id, exc_info=True)
            return _json_response({"error": "Not allowed to change this order."}, status=403)
        except (UserError, ValueError) as exc:
            return _json_response({"error": str(exc)}, status=422)
        except Exception:  # noqa: BLE001 — never leak a traceback body to the bridge
            _logger.exception("Order action failed for order %s", order_id)
            return _json_response({"error": "Something went wrong. Nothing was confirmed; try again."}, status=500)
        extra = result if isinstance(result, dict) else {}
        return _json_response({"card": _safe_order_card(order), **extra})

    @http.route(
        "/grove/api/v1/orders/<int:order_id>/hold",
        type="http",
        auth="bearer",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def order_hold(self, order_id, **_kwargs):
        """Body ``{"actor", "until", "reason"}``; ``until`` is typed text such as
        "Oct 25", "10/25" or "2027-03-01"."""

        def act(order, payload, actor):
            until = payload.get("until")
            reason = payload.get("reason")
            parsed = parse_hold_date(until if isinstance(until, str) else "", fields.Date.context_today(order))
            order.action_grove_set_hold(parsed, reason if isinstance(reason, str) else "", actor)

        return self._run_order_action(order_id, act)

    @http.route(
        "/grove/api/v1/orders/<int:order_id>/hold/release",
        type="http",
        auth="bearer",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def order_hold_release(self, order_id, **_kwargs):
        return self._run_order_action(order_id, lambda order, _payload, actor: order.action_grove_release_hold(actor))

    @http.route(
        "/grove/api/v1/orders/<int:order_id>/hold/snooze",
        type="http",
        auth="bearer",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def order_hold_snooze(self, order_id, **_kwargs):
        """Always the default snooze; any ``days`` in the body is ignored."""
        return self._run_order_action(order_id, lambda order, _payload, actor: order.action_grove_snooze_hold(actor))

    @http.route(
        "/grove/api/v1/orders/<int:order_id>/move",
        type="http",
        auth="bearer",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def order_move(self, order_id, **_kwargs):
        """Body ``{"actor", "wave"}`` with wave ``fall`` or ``spring``."""

        def act(order, payload, actor):
            wave = payload.get("wave")
            order.action_grove_move_wave(wave if isinstance(wave, str) else "", actor)

        return self._run_order_action(order_id, act)

    @http.route(
        "/grove/api/v1/orders/<int:order_id>/label-refunded",
        type="http",
        auth="bearer",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def order_label_refunded(self, order_id, **_kwargs):
        def refund(order, _payload, actor):
            boxes = len((order.grove_tracking_numbers or "").split())
            order.action_grove_label_refunded(actor)
            return {"boxes_cleared": boxes}

        return self._run_order_action(order_id, refund)

    @http.route(
        "/grove/api/v1/label-batches/<int:batch_id>/mark-shipped",
        type="http",
        auth="bearer",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def label_batch_mark_shipped(self, batch_id, **_kwargs):
        """Mark every labelled order in a zone batch shipped. Body ``{"actor"}``.

        Answers ``{"shipped", "already", "skipped": [{"order", "reason"}]}``. A 409
        is a refusal with nothing changed (no zone, labels not imported, or before
        the zone's ship start); its ``error`` is shown to the operator as written.
        Not wrapped in a savepoint: each order is committed as it ships, since its
        customer email cannot be recalled. Safe to repeat."""
        try:
            payload = json.loads(request.httprequest.data or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        current_company = request.website.company_id
        batch = (
            request.env["grove.label.batch"]
            .sudo()
            .with_company(current_company)
            .search([("id", "=", batch_id), ("company_id", "=", current_company.id)], limit=1)
        )
        if not batch:
            return _json_response({"error": "Batch not found"}, status=404)

        actor = payload.get("actor")
        if not isinstance(actor, str) or not actor.strip():
            return _json_response({"error": "actor is required"}, status=422)

        try:
            result = batch._grove_mark_batch_shipped(actor.strip())
        except AccessError:
            # Before UserError: AccessError is a subclass of it.
            _logger.warning("Mark all shipped refused for batch %s", batch_id, exc_info=True)
            return _json_response({"error": "Not allowed to change these orders."}, status=403)
        except UserError as exc:
            return _json_response({"error": str(exc)}, status=409)
        except Exception:  # noqa: BLE001 — never leak a traceback body to the bridge
            _logger.exception("Mark all shipped failed for batch %s", batch_id)
            return _json_response(
                {"error": "Something went wrong. Some orders may have shipped; check the orders before retrying."},
                status=500,
            )
        return _json_response(result)
