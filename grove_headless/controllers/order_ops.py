"""Order action routes for the Discord bridge.

Each route runs one order action for an operator and answers ``{"card": ...}``
built by the same helper as the alert post, so the bridge re-renders from what
Odoo says. Bearer auth and company scoping match ``order_mark_shipped``; the
action runs under ``sudo()`` as the service user, which passes the operator guard
(``_grove_require_order_operator``). The bridge checks DISCORD_ORDER_OPERATOR_IDS
before forwarding, and sends the Discord user it checked as ``actor``.

A ``UserError`` or ``ValueError`` is a 422 carrying its message, which the bridge
shows to the operator as written. Nothing else escapes as a traceback.
"""

import json
import logging

from odoo import fields, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models.order_lanes import parse_hold_date
from .main import _json_response, _order_card_payload

_logger = logging.getLogger(__name__)

# "Today" for typed hold dates and for the hold methods' own date checks.
_SHOP_TZ = "America/New_York"


class OrderOpsController(http.Controller):
    def _run_order_action(self, order_id, action):
        """Parse the body, find the website company's order, run ``action(order,
        payload, actor)`` and answer with the card. ``action`` may raise."""
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
            .with_context(tz=_SHOP_TZ)
            .search([("id", "=", order_id), ("company_id", "=", current_company.id)], limit=1)
        )
        if not order:
            return _json_response({"error": "Order not found"}, status=404)

        actor = payload.get("actor")
        if not isinstance(actor, str) or not actor.strip():
            return _json_response({"error": "actor is required"}, status=422)

        try:
            action(order, payload, actor.strip())
        except (UserError, ValueError) as exc:
            return _json_response({"error": str(exc)}, status=422)
        except AccessError:
            _logger.warning("Order action refused for order %s", order_id, exc_info=True)
            return _json_response({"error": "Not allowed to change this order."}, status=403)
        except Exception:  # noqa: BLE001 — never leak a traceback body to the bridge
            _logger.exception("Order action failed for order %s", order_id)
            return _json_response({"error": "Something went wrong. Nothing was confirmed; try again."}, status=500)
        return _json_response({"card": _order_card_payload(order)})

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
        return self._run_order_action(order_id, lambda order, _payload, actor: order.action_grove_label_refunded(actor))
