"""grove.consult.plan and grove.consult.plan.section (spec C.4, C.8).

A plan is a versioned design for one lead, made of sections. Phase 2 ships the
models, the approval constraint and the training-capture snapshots so the
dashboard can create a draft plan with empty skeleton sections; the generator
that fills them is phase 3.

C.4 constraint: a plan can move to ``approved`` only once every section is
``approved``. C.8 capture: every section keeps what the machine drafted
(``draft_*``, written once), what a named person signed off (``approved_*``,
rewritten on each approval) and a one-line ``edit_note``. The snapshot fields are
read-only for every group; only this model's own capture paths write them, keyed
on the private ``consult_snapshot`` context flag. Any edit to an approved
section's body returns it to ``edited`` (C.8), so the plan's approval constraint
cannot be met by an edit after sign-off.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

# Fields no user may write directly — only the capture paths below, which set
# ``consult_snapshot`` in the context before writing.
_SNAPSHOT_FIELDS = ("draft_body", "draft_source", "draft_inputs", "approved_body", "approved_at")


class GroveConsultPlan(models.Model):
    _name = "grove.consult.plan"
    _description = "Grove consult plan"
    _order = "lead_id, version desc, id desc"

    lead_id = fields.Many2one(
        "crm.lead", string="Lead", required=True, index=True, ondelete="cascade"
    )
    version = fields.Integer(string="Version", default=1, required=True)
    status = fields.Selection(
        [("draft", "Draft"), ("approved", "Approved"), ("sent", "Sent"), ("installed", "Installed")],
        string="Status",
        default="draft",
        required=True,
        index=True,
    )
    plan_json = fields.Json(string="Plan", help="Rows and consortia, the planner-plan shape extended with areas.")
    approved_by = fields.Many2one("res.users", string="Approved by", readonly=True)
    approved_at = fields.Datetime(string="Approved at", readonly=True)
    sale_order_id = fields.Many2one("sale.order", string="Sale order", ondelete="set null")
    section_ids = fields.One2many("grove.consult.plan.section", "plan_id", string="Sections")

    @api.constrains("status")
    def _check_all_sections_approved(self):
        """C.4: a plan can only become ``approved`` when every section is approved."""
        for plan in self:
            if plan.status != "approved":
                continue
            unapproved = plan.section_ids.filtered(lambda s: s.status != "approved")
            if unapproved:
                raise ValidationError(
                    _(
                        "Plan %(plan)s cannot be approved: %(count)d section(s) are not approved yet (%(keys)s).",
                        plan=plan.display_name,
                        count=len(unapproved),
                        keys=", ".join(unapproved.mapped("key") or ["?"]),
                    )
                )

    def write(self, vals):
        """Record approver and time when a plan is approved (the constraint above
        guarantees every section is already approved)."""
        if vals.get("status") == "approved":
            vals = dict(vals)
            vals.setdefault("approved_by", self.env.user.id)
            vals.setdefault("approved_at", fields.Datetime.now())
        return super().write(vals)

    def create_skeleton_sections(self, module_ids=None):
        """Create one empty skeleton section per module (spec C.4/C.8).

        Empty body, ``status='generated'`` and ``draft_source='skeleton'`` so the
        phase-3 generator can tell a never-filled section from a drafted one.
        """
        self.ensure_one()
        modules = self.env["grove.consult.module"]
        if module_ids:
            modules = modules.browse(module_ids).exists()
        Section = self.env["grove.consult.plan.section"].with_context(consult_snapshot=True)
        created = Section
        for seq, module in enumerate(modules, start=1):
            created |= Section.create(
                {
                    "plan_id": self.id,
                    "module_id": module.id,
                    "key": module.key,
                    "title": module.section_title or module.name,
                    "status": "generated",
                    "draft_source": "skeleton",
                    "sequence": seq * 10,
                }
            )
        return created


class GroveConsultPlanSection(models.Model):
    _name = "grove.consult.plan.section"
    _description = "Grove consult plan section"
    _order = "plan_id, sequence, id"

    plan_id = fields.Many2one(
        "grove.consult.plan", string="Plan", required=True, index=True, ondelete="cascade"
    )
    module_id = fields.Many2one("grove.consult.module", string="Module", ondelete="set null")
    key = fields.Char(string="Key", required=True)
    title = fields.Char(string="Title")
    body = fields.Text(string="Body")
    figures = fields.Json(string="Figures")
    status = fields.Selection(
        [("generated", "Generated"), ("edited", "Edited"), ("approved", "Approved")],
        string="Status",
        default="generated",
        required=True,
        index=True,
    )
    by = fields.Many2one("res.users", string="By")
    sequence = fields.Integer(string="Sequence", default=10)

    # C.8 training-capture snapshots (read-only; written only by the paths below).
    draft_body = fields.Text(string="Draft body", readonly=True)
    draft_source = fields.Char(string="Draft source", readonly=True)
    draft_inputs = fields.Json(string="Draft inputs", readonly=True)
    approved_body = fields.Text(string="Approved body", readonly=True)
    approved_at = fields.Datetime(string="Approved at", readonly=True)
    edit_note = fields.Char(string="Edit note", help="One line on why the draft was wrong. Never required.")

    def write(self, vals):
        """Enforce C.8: snapshot fields are read-only for everyone; the first
        non-empty body fills ``draft_*`` exactly once; editing an approved
        section's body returns it to ``edited``."""
        internal = self.env.context.get("consult_snapshot")
        if not internal:
            blocked = [f for f in _SNAPSHOT_FIELDS if f in vals]
            if blocked:
                raise AccessError(
                    _("These consult snapshot fields are read-only: %s.") % ", ".join(blocked)
                )

        if "body" in vals and not internal:
            new_body = vals.get("body")
            for section in self:
                extra = {}
                # First fill captures the draft, once and never again.
                if not section.draft_body and new_body:
                    extra["draft_body"] = new_body
                    extra["draft_source"] = self.env.context.get("consult_draft_source") or "skeleton"
                    extra["draft_inputs"] = self.env.context.get("consult_draft_inputs") or {}
                # An edit after sign-off demotes the section so the plan's
                # approval constraint cannot be met by a post-approval edit.
                if (
                    section.status == "approved"
                    and vals.get("status") != "approved"
                    and new_body != section.body
                ):
                    extra["status"] = "edited"
                if extra:
                    super(GroveConsultPlanSection, section).with_context(consult_snapshot=True).write(extra)

        return super().write(vals)

    def action_approve(self):
        """Sign off the section: snapshot the approved body, approver and time."""
        now = fields.Datetime.now()
        for section in self:
            section.with_context(consult_snapshot=True).write(
                {
                    "status": "approved",
                    "approved_body": section.body,
                    "approved_at": now,
                    "by": self.env.user.id,
                }
            )
        return True
