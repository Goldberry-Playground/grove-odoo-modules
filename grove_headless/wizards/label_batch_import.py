"""Operator "Import Pirate Ship export" wizard.

Upload ANY Pirate Ship export (.xlsx, .xls or .csv) and Odoo records every row
it can place: labels for the open batch, single hand-bought labels, replacement
labels for reprints, and refund flags. Rows it cannot place are listed in the
result and nothing is written for them, so one stray row no longer bounces the
whole file (``grove.label.batch.import_pirateship_file``).

The automated runner keeps the strict all-or-nothing reconcile on the
``/labels/batch/<id>/tracking`` endpoint (``import_tracking``).
"""

import base64

from odoo import fields, models


def _default_batch(self):
    if self.env.context.get("active_model") == "grove.label.batch":
        return self.env.context.get("active_id")
    return False


class GroveLabelBatchImport(models.TransientModel):
    _name = "grove.label.batch.import"
    _description = "Import a Pirate Ship export"

    batch_id = fields.Many2one("grove.label.batch", default=_default_batch)
    data = fields.Binary(string="Pirate Ship export", required=True)
    filename = fields.Char()

    def action_import(self):
        self.ensure_one()
        raw = base64.b64decode(self.data)
        company = self.batch_id.company_id or self.env.company
        result = self.env["grove.label.batch"].import_pirateship_file(
            raw, filename=self.filename or "pirateship-export", company=company
        )
        parts = []
        if result["labelled"]:
            parts.append(f"Labelled: {', '.join(result['labelled'])}.")
        if result["replaced"]:
            parts.append(f"Replaced: {'; '.join(result['replaced'])}.")
        if result["already"]:
            parts.append(f"{result['already']} row(s) already recorded.")
        if result["refunded"]:
            parts.append(f"⚠️ Refunded: {'; '.join(result['refunded'])}.")
        if result["manual_review"]:
            parts.append(f"⚠️ Matched by email, check: {', '.join(result['manual_review'])}.")
        if result["warnings"]:
            parts.append("⚠️ " + " ".join(result["warnings"]))
        if result["review"]:
            parts.append(f"⚠️ Not imported ({len(result['review'])}): " + "; ".join(result["review"]))
        if not parts:
            parts.append("Nothing to import in this file.")
        needs_eyes = any(result[k] for k in ("refunded", "manual_review", "warnings", "review"))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Pirate Ship import",
                "message": " ".join(parts),
                "type": "warning" if needs_eyes else "success",
                "sticky": needs_eyes,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
