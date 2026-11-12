"""Staff consult routes ``/grove/api/v1/consult/*`` (spec C.7).

The consult dashboard (grove-sites ``apps/consult``, phase 2 D) talks to Odoo only
through these routes, which its own BFF calls with the headless API key — never a
browser. ``auth="bearer"`` accepts the API-key login; the routes then run under
``sudo()`` scoped to the calling website's company, like ``order_ops.py``. Every
route returns **503** when ``grove_headless.consult_enabled`` is off, so the whole
surface can be dark in production until the consult phase goes live (the kill
switch is the requirement, so the default is never a code constant).

Section writes all go through one route (``…/section/<key>``) so the C.8 snapshots
are captured in one place (the model layer actually writes them). A ``UserError``
or ``ValidationError`` becomes a 422 carrying its message; nothing else escapes as
a traceback.

Route shapes are the contract for GOL-3576 (Iris's ``packages/consult-client``);
they are documented in the PR body.
"""

import json
import logging

from odoo import http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from ..models import consult_travel
from .main import _farm_pickup_zip, _json_response

_logger = logging.getLogger(__name__)

CONSULT_ENABLED_PARAM = "grove_headless.consult_enabled"

_LEAD_STAGES = ("intake", "site", "design", "quote", "install", "stewardship", "archived")
_HANDS_ON = ("we_install", "they_install", "plan_only")
# crm.lead consult fields a POST /lead may set (C.5). Relations handled separately.
_LEAD_SCALAR_FIELDS = (
    "grove_consult_stage",
    "grove_hands_on",
    "grove_goals",
    "grove_existing",
    "grove_property_uuid",
)


class ConsultController(http.Controller):
    # ── gate + helpers ──────────────────────────────────────────────────────
    def _enabled(self):
        value = request.env["ir.config_parameter"].sudo().get_param(CONSULT_ENABLED_PARAM, "False")
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _disabled_response(self):
        return _json_response({"error": "consult disabled"}, status=503)

    def _company(self):
        return request.website.company_id

    def _lead(self, lead_id):
        """The company's consult lead, or ``None``."""
        company = self._company()
        return (
            request.env["crm.lead"]
            .sudo()
            .with_company(company)
            .search([("id", "=", lead_id), ("company_id", "in", (company.id, False))], limit=1)
        )

    def _body(self):
        try:
            payload = json.loads(request.httprequest.data or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    # ── serializers ─────────────────────────────────────────────────────────
    def _area_dict(self, area):
        return {
            "id": area.id,
            "name": area.name,
            "practice_id": area.practice_id.id or None,
            "practice_key": area.practice_id.key or None,
            "acres": area.acres,
            "wet": area.wet,
            "poor_soil": area.poor_soil,
            "shade": area.shade,
            "aspect": area.aspect or None,
            "farmos_land_uuid": area.farmos_land_uuid or None,
            "sequence": area.sequence,
        }

    def _lead_dict(self, lead, *, full=False):
        data = {
            "id": lead.id,
            "name": lead.name,
            "contact_name": lead.contact_name or None,
            "email": lead.email_from or None,
            "phone": lead.phone or None,
            "zip": lead.zip or None,
            "consult_stage": lead.grove_consult_stage or None,
            "hands_on": lead.grove_hands_on or None,
            "consult_step_id": lead.grove_consult_step.id or None,
            "module_ids": lead.grove_module_ids.ids,
            "travel_estimate": lead.grove_travel_estimate or None,
        }
        if full:
            data.update(
                {
                    "goals": lead.grove_goals or None,
                    "existing": lead.grove_existing or None,
                    "property_uuid": lead.grove_property_uuid or None,
                    "stock_location_id": lead.grove_stock_location_id.id or None,
                    "areas": [self._area_dict(a) for a in lead.grove_area_ids],
                    "module_keys": lead.grove_module_ids.mapped("key"),
                }
            )
        return data

    def _module_dict(self, module):
        return {
            "id": module.id,
            "key": module.key,
            "name": module.name,
            "objective": module.objective or None,
            "section_title": module.section_title or None,
        }

    def _practice_dict(self, practice):
        return {
            "id": practice.id,
            "key": practice.key,
            "name": practice.name,
            "strata": practice.strata or [],
            "row_spacing_m": practice.row_spacing_m,
            "in_row_spacing_m": practice.in_row_spacing_m,
            "infrastructure_defaults": practice.infrastructure_defaults or [],
            "retail_track": practice.retail_track or None,
        }

    def _plan_dict(self, plan):
        return {
            "id": plan.id,
            "lead_id": plan.lead_id.id,
            "version": plan.version,
            "status": plan.status,
            "section_ids": [
                {"id": s.id, "key": s.key, "title": s.title, "status": s.status, "module_id": s.module_id.id or None}
                for s in plan.section_ids
            ],
        }

    # ── read routes ─────────────────────────────────────────────────────────
    @http.route("/grove/api/v1/consult/pipeline", type="http", auth="bearer", website=True, methods=["GET"], csrf=False)
    def consult_pipeline(self, **_kw):
        if not self._enabled():
            return self._disabled_response()
        company = self._company()
        leads = (
            request.env["crm.lead"]
            .sudo()
            .with_company(company)
            .search(
                [("grove_consult_stage", "!=", False), ("company_id", "in", (company.id, False))],
                order="grove_consult_stage, id desc",
            )
        )
        return _json_response({"leads": [self._lead_dict(lead) for lead in leads]})

    @http.route(
        "/grove/api/v1/consult/lead/<int:lead_id>",
        type="http",
        auth="bearer",
        website=True,
        methods=["GET"],
        csrf=False,
    )
    def consult_lead_get(self, lead_id, **_kw):
        if not self._enabled():
            return self._disabled_response()
        lead = self._lead(lead_id)
        if not lead:
            return _json_response({"error": "lead not found"}, status=404)
        return _json_response({"lead": self._lead_dict(lead, full=True)})

    @http.route("/grove/api/v1/consult/modules", type="http", auth="bearer", website=True, methods=["GET"], csrf=False)
    def consult_modules(self, **_kw):
        if not self._enabled():
            return self._disabled_response()
        modules = request.env["grove.consult.module"].sudo().search([])
        return _json_response({"modules": [self._module_dict(m) for m in modules]})

    @http.route(
        "/grove/api/v1/consult/practices", type="http", auth="bearer", website=True, methods=["GET"], csrf=False
    )
    def consult_practices(self, **_kw):
        if not self._enabled():
            return self._disabled_response()
        practices = request.env["grove.consult.practice"].sudo().search([])
        return _json_response({"practices": [self._practice_dict(p) for p in practices]})

    @http.route("/grove/api/v1/consult/travel", type="http", auth="bearer", website=True, methods=["GET"], csrf=False)
    def consult_travel(self, **kw):
        if not self._enabled():
            return self._disabled_response()
        client_zip = (kw.get("zip") or "").strip()
        if not client_zip:
            return _json_response({"error": "zip is required"}, status=422)
        farm_zip = _farm_pickup_zip(request.env, self._company())
        result = consult_travel.estimate_for_zips(farm_zip, client_zip)
        if result is None:
            return _json_response({"error": "no centroid for zip", "zip": client_zip, "farm_zip": farm_zip}, status=422)
        return _json_response({"travel": result})

    # ── write routes ────────────────────────────────────────────────────────
    def _apply_lead_vals(self, lead, payload):
        """Apply the consult fields in ``payload`` to ``lead`` (both create+update).
        Only known fields are touched; unknown keys are ignored."""
        vals = {}
        for field in _LEAD_SCALAR_FIELDS:
            if field in payload:
                vals[field] = payload[field]
        if "consult_step_id" in payload:
            vals["grove_consult_step"] = payload["consult_step_id"] or False
        if "stock_location_id" in payload:
            vals["grove_stock_location_id"] = payload["stock_location_id"] or False
        if "module_ids" in payload and isinstance(payload["module_ids"], list):
            vals["grove_module_ids"] = [(6, 0, [int(m) for m in payload["module_ids"]])]
        if vals.get("grove_consult_stage") and vals["grove_consult_stage"] not in _LEAD_STAGES:
            raise UserError("unknown consult stage")
        if vals.get("grove_hands_on") and vals["grove_hands_on"] not in _HANDS_ON:
            raise UserError("unknown hands_on value")
        if vals:
            lead.write(vals)
        # Areas: a full replace when provided (simplest contract for the dashboard).
        if isinstance(payload.get("areas"), list):
            lead.grove_area_ids.unlink()
            Area = request.env["grove.consult.area"].sudo().with_company(self._company())
            for seq, area in enumerate(payload["areas"], start=1):
                if not isinstance(area, dict):
                    continue
                Area.create(
                    {
                        "lead_id": lead.id,
                        "name": area.get("name") or "Area",
                        "practice_id": area.get("practice_id") or False,
                        "acres": area.get("acres") or 0.0,
                        "wet": bool(area.get("wet")),
                        "poor_soil": bool(area.get("poor_soil")),
                        "shade": bool(area.get("shade")),
                        "aspect": area.get("aspect") or False,
                        "sequence": seq * 10,
                    }
                )

    @http.route("/grove/api/v1/consult/lead", type="http", auth="bearer", website=True, methods=["POST"], csrf=False)
    def consult_lead_upsert(self, **_kw):
        if not self._enabled():
            return self._disabled_response()
        payload = self._body()
        company = self._company()
        try:
            with request.env.cr.savepoint():
                lead_id = payload.get("id")
                if lead_id:
                    lead = self._lead(int(lead_id))
                    if not lead:
                        return _json_response({"error": "lead not found"}, status=404)
                else:
                    name = (payload.get("name") or "").strip()
                    if not name:
                        return _json_response({"error": "name is required"}, status=422)
                    lead = (
                        request.env["crm.lead"]
                        .sudo()
                        .with_company(company)
                        .create({"name": name, "company_id": company.id, "type": "lead"})
                    )
                self._apply_lead_vals(lead, payload)
        except (UserError, ValidationError) as err:
            return _json_response({"error": str(err)}, status=422)
        return _json_response({"lead": self._lead_dict(lead, full=True)})

    @http.route(
        "/grove/api/v1/consult/lead/<int:lead_id>/stage",
        type="http",
        auth="bearer",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def consult_lead_stage(self, lead_id, **_kw):
        if not self._enabled():
            return self._disabled_response()
        lead = self._lead(lead_id)
        if not lead:
            return _json_response({"error": "lead not found"}, status=404)
        stage = (self._body().get("stage") or "").strip()
        if stage not in _LEAD_STAGES:
            return _json_response({"error": "unknown stage"}, status=422)
        lead.write({"grove_consult_stage": stage})
        return _json_response({"lead": self._lead_dict(lead)})

    @http.route(
        "/grove/api/v1/consult/lead/<int:lead_id>/plan",
        type="http",
        auth="bearer",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def consult_plan_create(self, lead_id, **_kw):
        if not self._enabled():
            return self._disabled_response()
        lead = self._lead(lead_id)
        if not lead:
            return _json_response({"error": "lead not found"}, status=404)
        payload = self._body()
        try:
            with request.env.cr.savepoint():
                Plan = request.env["grove.consult.plan"].sudo().with_company(self._company())
                prior = Plan.search([("lead_id", "=", lead.id)], order="version desc", limit=1)
                version = (prior.version + 1) if prior else 1
                plan = Plan.create({"lead_id": lead.id, "version": version, "status": "draft"})
                module_ids = payload.get("module_ids")
                if not isinstance(module_ids, list):
                    module_ids = lead.grove_module_ids.ids
                plan.create_skeleton_sections(module_ids=[int(m) for m in module_ids])
        except (UserError, ValidationError) as err:
            return _json_response({"error": str(err)}, status=422)
        return _json_response({"plan": self._plan_dict(plan)})

    @http.route(
        "/grove/api/v1/consult/lead/<int:lead_id>/plan/<int:version>/section/<string:key>",
        type="http",
        auth="bearer",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def consult_plan_section(self, lead_id, version, key, **_kw):
        """Write one section (C.7/C.8): ``body``, ``edit_note``, or ``approve: true``.
        All capture goes through the model's ``write``/``action_approve`` so the
        snapshots are taken in one place."""
        if not self._enabled():
            return self._disabled_response()
        lead = self._lead(lead_id)
        if not lead:
            return _json_response({"error": "lead not found"}, status=404)
        plan = (
            request.env["grove.consult.plan"]
            .sudo()
            .with_company(self._company())
            .search([("lead_id", "=", lead.id), ("version", "=", version)], limit=1)
        )
        if not plan:
            return _json_response({"error": "plan not found"}, status=404)
        section = plan.section_ids.filtered(lambda s: s.key == key)[:1]
        if not section:
            return _json_response({"error": "section not found"}, status=404)
        payload = self._body()
        try:
            with request.env.cr.savepoint():
                vals = {}
                if "body" in payload:
                    vals["body"] = payload["body"]
                    vals["status"] = "edited"
                if "edit_note" in payload:
                    vals["edit_note"] = payload["edit_note"]
                if vals:
                    ctx = {}
                    if payload.get("draft_source"):
                        ctx["consult_draft_source"] = payload["draft_source"]
                    if payload.get("draft_inputs"):
                        ctx["consult_draft_inputs"] = payload["draft_inputs"]
                    section.with_context(**ctx).write(vals)
                if payload.get("approve") is True:
                    section.action_approve()
        except (AccessError, UserError, ValidationError) as err:
            return _json_response({"error": str(err)}, status=422)
        return _json_response(
            {"section": {"id": section.id, "key": section.key, "status": section.status, "title": section.title}}
        )
