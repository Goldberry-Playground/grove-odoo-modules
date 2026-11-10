"""Operator "Build Pirate Ship batch" wizard: pick a zone, build its spring batch.

Spring orders are batched per USDA zone (``grove.label.batch.build_open_batch``),
so the Build menu asks which zone first. A build outside that zone's window is
refused with the date it opens.
"""

from odoo import fields, models

from ..models.shipping_calendar import USDA_ZONE_SELECTION


class GroveLabelBatchBuild(models.TransientModel):
    _name = "grove.label.batch.build"
    _description = "Build a Pirate Ship batch for one zone"

    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        domain=lambda self: [("id", "in", self.env.companies.ids)],
    )
    zone = fields.Selection(selection=USDA_ZONE_SELECTION, string="USDA Zone", required=True)

    def _notify(self, message, next_action=None):
        params = {"title": "Pirate Ship batch", "message": message, "type": "warning", "sticky": True}
        if next_action:
            params["next"] = next_action
        return {"type": "ir.actions.client", "tag": "display_notification", "params": params}

    def action_build(self):
        self.ensure_one()
        result = self.env["grove.label.batch"].build_open_batch(self.company_id, self.zone)
        batch = result["batch"]
        if not batch:
            return self._notify(result["message"])
        open_batch = {
            "type": "ir.actions.act_window",
            "res_model": "grove.label.batch",
            "res_id": batch.id,
            "view_mode": "form",
            "views": [(False, "form")],
        }
        warnings = []
        if result["skipped"]:
            warnings.append("These orders were left out:\n" + "\n".join(result["skipped"]))
        if result["needs_confirmation"]:
            warnings.append("Held out of the CSV until confirmed:\n" + "\n".join(result["needs_confirmation"]))
        if warnings:
            return self._notify(f"{batch.name} was built.\n" + "\n".join(warnings), next_action=open_batch)
        return open_batch
