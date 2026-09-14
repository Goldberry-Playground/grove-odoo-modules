"""Manual "Import Pirate Ship tracking" wizard (GOL-2271, spec §B1).

The manual path must always exist and use the SAME reconcile code as the runner:
this wizard just uploads the *Export Tracking Data* CSV and calls
``grove.label.batch.import_tracking`` — identical to what the
``/labels/batch/<id>/tracking`` endpoint runs. A validation failure raises
``LabelBatchError`` (a ``UserError``), which Odoo shows in the wizard dialog with
the offending refs, and nothing is written.
"""

import base64

from odoo import fields, models


class GroveLabelBatchImport(models.TransientModel):
    _name = "grove.label.batch.import"
    _description = "Import Pirate Ship tracking into a label batch"

    batch_id = fields.Many2one(
        "grove.label.batch",
        required=True,
        default=lambda self: self.env.context.get("active_id"),
    )
    data = fields.Binary(string="Tracking CSV", required=True)
    filename = fields.Char()

    def action_import(self):
        self.ensure_one()
        raw = base64.b64decode(self.data)
        result = self.batch_id.import_tracking(raw, filename=self.filename or "tracking.csv")
        message = (
            f"{result['orders_advanced']} order(s) advanced, "
            f"{result['skipped_already_tracked']} already-tracked skipped, "
            f"${result['total']:.2f} reconciled."
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.batch_id.name,
                "message": message,
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
