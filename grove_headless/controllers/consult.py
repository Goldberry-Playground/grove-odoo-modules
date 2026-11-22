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

from odoo import http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from ..models import consult_travel
from ..models.shipping_calendar import usda_zone_for_zip
from .main import _farm_pickup_zip, _json_response

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

    def _zone(self, lead):
        """USDA hardiness zone for the lead's ZIP as a string, or ``None``.

        The matrix carries integer zones only (no a/b suffix) and is pre-filtered
        to the served green states, so a lead outside that span reads ``None`` and
        the app simply shows no zone. Serving it here saves the app a ``/zone``
        call per pipeline row (GOL-4093)."""
        zone = usda_zone_for_zip(lead.zip or "")
        return str(zone) if zone is not None else None

    def _lead_dict(self, lead, *, full=False):
        data = {
            "id": lead.id,
            "name": lead.name,
            "contact_name": lead.contact_name or None,
            "email": lead.email_from or None,
            "phone": lead.phone or None,
            "zip": lead.zip or None,
            "consult_stage": lead.grove_consult_stage or None,
            "stage_since": lead.grove_stage_since.isoformat() if lead.grove_stage_since else None,
            "zone": self._zone(lead),
            "hands_on": lead.grove_hands_on or None,
            "consult_step_id": lead.grove_consult_step.id or None,
            "module_ids": lead.grove_module_ids.ids,
            "travel_estimate": lead.grove_travel_estimate or None,
            # Areas ride the pipeline brief too so the dashboard needs no extra
            # read per lead to show them (GOL-4093).
            "areas": [self._area_dict(a) for a in lead.grove_area_ids],
        }
        if full:
            data.update(
                {
                    "goals": lead.grove_goals or None,
                    "existing": lead.grove_existing or None,
                    "property_uuid": lead.grove_property_uuid or None,
                    "stock_location_id": lead.grove_stock_location_id.id or None,
                    "module_keys": lead.grove_module_ids.mapped("key"),
                    # Always an object once the field exists, so the app can tell
                    # "supported, empty" from "not supported" (GOL-4093).
                    "walk_checklist": lead.grove_walk_checklist or {},
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
            "site_needs": practice.site_needs or None,
            "infrastructure_defaults": practice.infrastructure_defaults or [],
            "retail_track": practice.retail_track or None,
        }

    def _step_dict(self, product):
        return {
            "id": product.id,
            "name": product.display_name,
            "price": product.lst_price,
        }

    def _section_dict(self, section):
        """Full section shape for the Design tab (GOL-4093): the live body, the
        machine draft kept beside it, the one-line edit note, who signed off (the
        verified actor, not the API user) and when."""
        return {
            "id": section.id,
            "key": section.key,
            "title": section.title,
            "status": section.status,
            "module_id": section.module_id.id or None,
            "body": section.body or "",
            "draft_body": section.draft_body or "",
            "edit_note": section.edit_note or "",
            "by": section.by_actor or (section.by.name or ""),
            "approved_at": section.approved_at.isoformat() if section.approved_at else None,
        }

    def _plan_dict(self, plan):
        return {
            "id": plan.id,
            "lead_id": plan.lead_id.id,
            "version": plan.version,
            "status": plan.status,
            "section_ids": [self._section_dict(s) for s in plan.section_ids],
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

    @http.route(
        "/grove/api/v1/consult/lead/<int:lead_id>/plans",
        type="http",
        auth="bearer",
        website=True,
        methods=["GET"],
        csrf=False,
    )
    def consult_plans(self, lead_id, **_kw):
        """Every plan version for a lead with its full sections (GOL-4093), so the
        Design tab can read a plan back after the generator or a human fills it."""
        if not self._enabled():
            return self._disabled_response()
        lead = self._lead(lead_id)
        if not lead:
            return _json_response({"error": "lead not found"}, status=404)
        plans = (
            request.env["grove.consult.plan"]
            .sudo()
            .with_company(self._company())
            .search([("lead_id", "=", lead.id)], order="version desc")
        )
        return _json_response({"plans": [self._plan_dict(p) for p in plans]})

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

    @http.route("/grove/api/v1/consult/steps", type="http", auth="bearer", website=True, methods=["GET"], csrf=False)
    def consult_steps(self, **_kw):
        """The consult service menu (C.6): the service products the step picker
        offers (GOL-4093). Scoped to the company and flagged with
        ``grove_consult_step_ok`` so unrelated services (S&H, labor) never leak in."""
        if not self._enabled():
            return self._disabled_response()
        company = self._company()
        steps = (
            request.env["product.product"]
            .sudo()
            .with_company(company)
            .search(
                [
                    ("grove_consult_step_ok", "=", True),
                    ("company_id", "in", (company.id, False)),
                ],
                order="lst_price, name",
            )
        )
        return _json_response({"steps": [self._step_dict(s) for s in steps]})

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
        # Native lead fields the intake form also writes (GOL-4093): a client can
        # be renamed and its ZIP set on an update, not just at create.
        if "name" in payload and str(payload["name"]).strip():
            vals["name"] = str(payload["name"]).strip()
        if "zip" in payload:
            vals["zip"] = payload["zip"] or False
        if "walk_checklist" in payload:
            walk = payload["walk_checklist"]
            vals["grove_walk_checklist"] = walk if isinstance(walk, dict) else {}
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
        snapshots are taken in one place. An ``actor`` (the verified Cloudflare
        Access email the app holds) is recorded as who signed off, not the API
        user (GOL-4093)."""
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
        actor = (payload.get("actor") or "").strip()
        try:
            with request.env.cr.savepoint():
                vals = {}
                if "body" in payload:
                    vals["body"] = payload["body"]
                    vals["status"] = "edited"
                if "edit_note" in payload:
                    vals["edit_note"] = payload["edit_note"]
                if actor:
                    vals["by_actor"] = actor
                if vals:
                    ctx = {}
                    if payload.get("draft_source"):
                        ctx["consult_draft_source"] = payload["draft_source"]
                    if payload.get("draft_inputs"):
                        ctx["consult_draft_inputs"] = payload["draft_inputs"]
                    section.with_context(**ctx).write(vals)
                if payload.get("approve") is True:
                    section.action_approve(actor=actor or None)
        except (AccessError, UserError, ValidationError) as err:
            return _json_response({"error": str(err)}, status=422)
        return _json_response({"section": self._section_dict(section)})

    @http.route(
        "/grove/api/v1/consult/lead/<int:lead_id>/walk",
        type="http",
        auth="bearer",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def consult_lead_walk(self, lead_id, **_kw):
        """Save the site-walk checklist (GOL-4093). Body ``{"walk": {key: bool}}``;
        a full replace of the stored map. (``POST /lead`` with ``walk_checklist``
        writes the same field, so the app may use either.)"""
        if not self._enabled():
            return self._disabled_response()
        lead = self._lead(lead_id)
        if not lead:
            return _json_response({"error": "lead not found"}, status=404)
        walk = self._body().get("walk")
        if not isinstance(walk, dict):
            return _json_response({"error": "walk must be an object"}, status=422)
        lead.write({"grove_walk_checklist": walk})
        return _json_response({"lead": self._lead_dict(lead, full=True)})

    @http.route(
        "/grove/api/v1/consult/lead/<int:lead_id>/plan/<int:version>/approve",
        type="http",
        auth="bearer",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def consult_plan_approve(self, lead_id, version, **_kw):
        """Approve a whole plan (C.4 / GOL-4093). Refuses with 422 unless every
        section is approved; on success the plan is locked to ``approved`` and the
        verified ``actor`` is recorded as approver."""
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
        actor = (self._body().get("actor") or "").strip()
        try:
            with request.env.cr.savepoint():
                plan.action_approve(actor=actor or None)
        except (AccessError, UserError, ValidationError) as err:
            return _json_response({"error": str(err)}, status=422)
        return _json_response({"plan": self._plan_dict(plan)})
