"""Public planner-learning routes (GOL-3549, planner winter beta spec B).

Same shape as ``/grove/api/v1/cart``: ``type="http"``, ``auth="public"``,
``website=True``, ``csrf=False``, a JSON body read from the raw request, strict
400s on anything malformed, and ``_json_response`` for the reply. All routes are
called server-side by the storefront BFF (not the browser), so there is no
extra CORS beyond the cart's.

Every route returns 503 when ``grove_headless.planner_learning_enabled`` is
false (the kill switch); the storefront treats that as a silent no-op. The
public write routes (signal/suggest/save) are capped at 60 requests per browser
id per hour; dispute/propose require a valid tester token.
"""

import json
from datetime import datetime, timedelta, timezone

from odoo import http
from odoo.http import request

from ..services.planner.rules import (
    RATE_LIMIT_GLOBAL_PER_HOUR,
    RATE_LIMIT_PER_HOUR,
    RATE_LIMIT_WINDOW_SECONDS,
    validate_dispute,
    validate_propose,
    validate_save,
    validate_signal,
    validate_suggestion,
)
from .main import _json_response

LEARNING_ENABLED_PARAM = "grove_headless.planner_learning_enabled"
TESTER_COOKIE = "grove_planner_tester"


class PlannerLearningController(http.Controller):
    # ── shared guards ────────────────────────────────────────────────────────

    def _learning_enabled(self):
        value = request.env["ir.config_parameter"].sudo().get_param(LEARNING_ENABLED_PARAM, "True")
        return str(value).strip().lower() not in ("false", "0", "", "off", "no")

    def _disabled_response(self):
        return _json_response({"error": "planner learning disabled"}, status=503)

    def _parse_body(self):
        """Return ``(payload, None)`` or ``(None, error_response)``."""
        try:
            return json.loads(request.httprequest.data or "{}"), None
        except json.JSONDecodeError:
            return None, _json_response({"error": "Invalid JSON body"}, status=400)

    def _rate_limited(self, model_name, browser_id):
        """True once this browser id has written ``RATE_LIMIT_PER_HOUR`` rows to
        the given model in the sliding hour window (spec B.2), or once *all*
        browser ids together have written ``RATE_LIMIT_GLOBAL_PER_HOUR`` — a
        flood backstop that does not trust the client-supplied browser id
        (GOL-3549 audit, finding 5)."""
        since = datetime.now(timezone.utc) - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
        since_str = since.strftime("%Y-%m-%d %H:%M:%S")
        model = request.env[model_name].sudo()
        if model.search_count([("create_date", ">=", since_str)]) >= RATE_LIMIT_GLOBAL_PER_HOUR:
            return True
        if not browser_id:
            return False
        count = model.search_count(
            [
                ("browser_id", "=", browser_id),
                ("create_date", ">=", since_str),
            ]
        )
        return count >= RATE_LIMIT_PER_HOUR

    def _resolve_template(self, product_tmpl_id):
        """This storefront's company-scoped template, or empty recordset."""
        if not product_tmpl_id:
            return request.env["product.template"].sudo().browse()
        company = request.website.company_id
        return (
            request.env["product.template"]
            .sudo()
            .with_company(company)
            .search([("id", "=", product_tmpl_id), ("company_id", "in", [company.id, False])], limit=1)
        )

    def _tester_name(self, payload):
        """Tester name for the token in the body or the HttpOnly cookie, or None."""
        token = payload.get("tester_token") or request.httprequest.cookies.get(TESTER_COOKIE)
        return request.env["grove.planner.tester"].sudo().name_for_token(token)

    # ── public write routes ───────────────────────────────────────────────────

    @http.route("/grove/api/v1/planner/signal", type="http", auth="public", website=True, methods=["POST"], csrf=False)
    def planner_signal(self, **_kwargs):
        if not self._learning_enabled():
            return self._disabled_response()
        payload, error = self._parse_body()
        if error:
            return error
        clean, message = validate_signal(payload)
        if message:
            return _json_response({"error": message}, status=400)
        if self._rate_limited("grove.planner.signal", clean["browser_id"]):
            return _json_response({"error": "rate limit exceeded"}, status=429)
        if clean["product_tmpl_id"]:
            product = self._resolve_template(clean["product_tmpl_id"])
            if not product:
                return _json_response({"error": "Product not found"}, status=400)
        request.env["grove.planner.signal"].sudo().create(clean)
        return _json_response({"ok": True})

    @http.route("/grove/api/v1/planner/suggest", type="http", auth="public", website=True, methods=["POST"], csrf=False)
    def planner_suggest(self, **_kwargs):
        if not self._learning_enabled():
            return self._disabled_response()
        payload, error = self._parse_body()
        if error:
            return error
        clean, message = validate_suggestion(payload)
        if message:
            return _json_response({"error": message}, status=400)
        if self._rate_limited("grove.planner.suggestion", clean["browser_id"]):
            return _json_response({"error": "rate limit exceeded"}, status=429)
        # A valid tester token names the suggester; otherwise it is public.
        by = self._tester_name(payload) or "public"
        request.env["grove.planner.suggestion"].sudo().create(
            {
                "name": clean["name"],
                "stratum": clean["stratum"],
                "note": clean["note"],
                "browser_id": clean["browser_id"],
                "by": by,
            }
        )
        return _json_response({"ok": True})

    @http.route("/grove/api/v1/planner/save", type="http", auth="public", website=True, methods=["POST"], csrf=False)
    def planner_save(self, **_kwargs):
        if not self._learning_enabled():
            return self._disabled_response()
        payload, error = self._parse_body()
        if error:
            return error
        clean, message = validate_save(payload)
        if message:
            return _json_response({"error": message}, status=400)
        if self._rate_limited("grove.planner.plan", clean["browser_id"]):
            return _json_response({"error": "rate limit exceeded"}, status=429)

        line_commands = []
        for line in clean["lines"]:
            product = self._resolve_template(line["product_tmpl_id"])
            if not product:
                return _json_response({"error": f"Product {line['product_tmpl_id']} not found"}, status=400)
            line_commands.append(
                (
                    0,
                    0,
                    {
                        "product_tmpl_id": product.id,
                        "qty": line["qty"],
                        "role": line["role"],
                        "stratum": line["stratum"],
                    },
                )
            )
        plan = (
            request.env["grove.planner.plan"]
            .sudo()
            .create(
                {
                    "name": clean["name"],
                    "plan_url": clean["plan_url"],
                    "zone": clean["zone"],
                    "track": clean["track"],
                    "browser_id": clean["browser_id"],
                    "status": "saved",
                    "line_ids": line_commands,
                }
            )
        )
        return _json_response({"ok": True, "id": plan.id})

    # ── tester-token routes ────────────────────────────────────────────────────

    @http.route("/grove/api/v1/planner/dispute", type="http", auth="public", website=True, methods=["POST"], csrf=False)
    def planner_dispute(self, **_kwargs):
        if not self._learning_enabled():
            return self._disabled_response()
        payload, error = self._parse_body()
        if error:
            return error
        tester = self._tester_name(payload)
        if not tester:
            return _json_response({"error": "valid tester token required"}, status=403)
        clean, message = validate_dispute(payload)
        if message:
            return _json_response({"error": message}, status=400)
        product = self._resolve_template(clean["product_tmpl_id"])
        if not product:
            return _json_response({"error": "Product not found"}, status=400)
        request.env["grove.planner.evidence"].sudo().create(
            {
                "product_tmpl_id": product.id,
                "fact": clean["fact"],
                "contradicts": clean["contradicts"],
                "note": clean["note"],
                "zone": clean["zone"],
                "source": "tester",
                "by": tester,
            }
        )
        return _json_response({"ok": True})

    @http.route("/grove/api/v1/planner/propose", type="http", auth="public", website=True, methods=["POST"], csrf=False)
    def planner_propose(self, **_kwargs):
        if not self._learning_enabled():
            return self._disabled_response()
        payload, error = self._parse_body()
        if error:
            return error
        tester = self._tester_name(payload)
        if not tester:
            return _json_response({"error": "valid tester token required"}, status=403)
        clean, message = validate_propose(payload)
        if message:
            return _json_response({"error": message}, status=400)
        rule = (
            request.env["grove.planner.rule"]
            .sudo()
            .search([("key", "=", clean["key"]), ("scope", "=", "retail")], limit=1)
        )
        request.env["grove.planner.proposal"].sudo().create(
            {
                "kind": "rule",
                "rule_id": rule.id if rule else False,
                "change": {"value": clean["value"]},
                "reason": f"tester {tester} proposed {clean['key']} = {clean['value']}",
            }
        )
        return _json_response({"ok": True})
