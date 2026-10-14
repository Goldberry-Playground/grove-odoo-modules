"""Legacy label reconcile wizard (GOL-3091, split from GOL-3083 item 2).

Josh shipped a stretch of orders by hand in Pirate Ship before the Odoo ship
flow existed, so those orders still sit in Odoo "awaiting a label" even though
they were bought, shipped and (usually) delivered weeks ago. Left alone, the
label batch keeps proposing to re-buy their labels (the GOL-3083 duplicate), and
the normal ship path would re-email customers a tracking notice for an order
long delivered.

This one-time (repeatable) wizard uploads the Pirate Ship **Shipments export**,
matches each row to an open order by **recipient email + name** (reusing the
export parser that already reads .xls/.xlsx + value-pattern Grove Ref from
``grove.label.batch``), and lets the operator record each match with ONE click.
Recording goes through ``sale.order.action_grove_record_historical_label`` — a
records-only backfill that lands the order at ``delivered``/``shipped`` WITHOUT
the customer shipment email and WITHOUT re-running settlement.

Matching never auto-applies and refuses ambiguity: a row that matches two open
orders with the same recipient is flagged ``ambiguous`` and offers no Record
button, so a human resolves it by hand. A row whose tracking number is already
recorded on some order is flagged ``duplicate`` (nothing to do).
"""

import base64
import re

from odoo import fields, models
from odoo.exceptions import UserError

from ..models.label_batch import LabelBatchError, _carrier_token

# Parse a label/ship date out of a Pirate Ship export cell. Pirate Ship writes
# a few shapes ("2026-09-15", "09/15/2026", "September 15, 2026 2:04 PM"); we
# only need the date, so sniff the common ones and fall back to None (the record
# method then stamps now() rather than guessing a wrong date).
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})|(\d{1,2})/(\d{1,2})/(\d{2,4})")


def _normalize(text):
    """Lowercased, whitespace-collapsed key for tolerant email/name matching."""
    return " ".join((text or "").strip().lower().split())


def _parse_date(raw):
    """Best-effort ``YYYY-MM-DD`` date string from an export cell, or None."""
    m = _DATE_RE.search(raw or "")
    if not m:
        return None
    if m.group(1):  # ISO yyyy-mm-dd
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    mm, dd, yy = m.group(4), m.group(5), m.group(6)
    if len(yy) == 2:
        yy = f"20{yy}"
    return f"{yy}-{int(mm):02d}-{int(dd):02d}"


class GroveLabelReconcile(models.TransientModel):
    _name = "grove.label.reconcile"
    _description = "Reconcile a Pirate Ship Shipments export against open orders"

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    data = fields.Binary(string="Shipments export", required=True)
    filename = fields.Char()
    scanned = fields.Boolean(default=False)
    summary = fields.Text(readonly=True)
    line_ids = fields.One2many("grove.label.reconcile.line", "reconcile_id")

    def _reopen(self):
        """Re-render this same wizard record (after a scan / a one-click record)."""
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "new",
        }

    def action_scan(self):
        """Parse the uploaded export and match every row to an open order."""
        self.ensure_one()
        if not self.data:
            raise UserError("Upload the Pirate Ship Shipments export first.")
        Batch = self.env["grove.label.batch"]
        raw = base64.b64decode(self.data)
        try:
            rows = Batch._parse_tracking_file(raw, filename=self.filename or "shipments.xlsx")
        except LabelBatchError as exc:
            raise UserError(str(exc)) from exc

        company = self.company_id or self.env.company
        eligible = Batch.with_company(company)._eligible_orders(company)
        # Index the open pool by (email, recipient-name). Both keys are required
        # to match — email alone is not enough, and two open orders sharing the
        # same recipient collapse to one bucket so the row is flagged ambiguous.
        by_identity = {}
        for order in eligible:
            name_key = _normalize(order.partner_shipping_id.name or order.partner_id.name)
            for email in Batch._order_emails(order):
                by_identity.setdefault((email, name_key), self.env["sale.order"])
                by_identity[(email, name_key)] |= order

        self.line_ids.unlink()
        Line = self.env["grove.label.reconcile.line"]
        counts = {"matched": 0, "ambiguous": 0, "unmatched": 0, "duplicate": 0}
        for row in rows:
            tracking = (row.get("tracking") or "").strip()
            email = _normalize(row.get("email"))
            recipient = row.get("recipient") or ""
            name_key = _normalize(recipient)
            status_raw = row.get("status") or ""
            delivered = "deliver" in status_raw.lower()
            carrier_key, service_token = _carrier_token(row.get("carrier_raw"))
            try:
                cost = round(float((row.get("cost_raw") or "0").replace("$", "").replace(",", "")), 2)
            except (TypeError, ValueError):
                cost = 0.0
            label_date = _parse_date(row.get("date"))

            vals = {
                "reconcile_id": self.id,
                "recipient": recipient,
                "email": row.get("email") or "",
                "tracking": tracking,
                "carrier": carrier_key or (row.get("carrier_raw") or ""),
                "service": service_token or "",
                "cost": cost,
                "label_date": label_date or "",
                "export_status": status_raw,
                "delivered": delivered,
            }

            # A row whose tracking is already recorded on any order is done — the
            # eligible pool excludes tracked orders, so search the whole company.
            prior = tracking and self.env["sale.order"].sudo().with_company(company).search(
                [("company_id", "=", company.id), ("grove_tracking_numbers", "like", tracking)], limit=1
            )
            if prior:
                vals.update(status="duplicate", order_id=prior.id, evidence=f"Tracking already on {prior.name}.")
                counts["duplicate"] += 1
            elif not email or not name_key:
                vals.update(status="unmatched", evidence="Row has no usable recipient email + name to match on.")
                counts["unmatched"] += 1
            else:
                candidates = by_identity.get((email, name_key), self.env["sale.order"])
                if len(candidates) == 1:
                    order = candidates
                    vals.update(
                        status="matched",
                        order_id=order.id,
                        evidence=(
                            f"{order.name} · {order.partner_shipping_id.name or order.partner_id.name} "
                            f"<{email}>"
                            + (f" · export status {status_raw}" if status_raw else "")
                            + (f" · {label_date}" if label_date else "")
                        ),
                    )
                    counts["matched"] += 1
                elif len(candidates) > 1:
                    vals.update(
                        status="ambiguous",
                        evidence=(
                            "Refused — same recipient on "
                            + ", ".join(sorted(candidates.mapped("name")))
                            + "; resolve by hand."
                        ),
                    )
                    counts["ambiguous"] += 1
                else:
                    vals.update(status="unmatched", evidence="No open order with this recipient email + name.")
                    counts["unmatched"] += 1
            Line.create(vals)

        self.scanned = True
        self.summary = (
            f"{len(rows)} export row(s): {counts['matched']} match(es) ready to record, "
            f"{counts['ambiguous']} ambiguous (refused), {counts['unmatched']} unmatched, "
            f"{counts['duplicate']} already recorded."
        )
        return self._reopen()


class GroveLabelReconcileLine(models.TransientModel):
    _name = "grove.label.reconcile.line"
    _description = "A Pirate Ship Shipments export row matched to an open order"

    reconcile_id = fields.Many2one("grove.label.reconcile", required=True, ondelete="cascade")
    order_id = fields.Many2one("sale.order", readonly=True)
    recipient = fields.Char(readonly=True)
    email = fields.Char(readonly=True)
    tracking = fields.Char(readonly=True)
    carrier = fields.Char(readonly=True)
    service = fields.Char(readonly=True)
    cost = fields.Float(readonly=True)
    label_date = fields.Char(readonly=True)
    export_status = fields.Char(string="Export status", readonly=True)
    delivered = fields.Boolean(readonly=True)
    status = fields.Selection(
        [
            ("matched", "Matched"),
            ("ambiguous", "Ambiguous — refused"),
            ("unmatched", "No match"),
            ("duplicate", "Already recorded"),
        ],
        readonly=True,
    )
    evidence = fields.Char(readonly=True)
    recorded = fields.Boolean(readonly=True, default=False)

    def action_record_one(self):
        """Record THIS matched row's historical label on its order (one click)."""
        self.ensure_one()
        if self.status != "matched":
            raise UserError("Only an unambiguous match can be recorded.")
        if self.recorded:
            raise UserError(f"{self.order_id.name} was already recorded in this scan.")
        self.order_id.action_grove_record_historical_label(
            tracking_number=self.tracking,
            carrier=self.carrier or None,
            actual_cost=self.cost,
            service=self.service or None,
            label_date=self.label_date or None,
            delivered=self.delivered,
        )
        self.recorded = True  # hides the Record button; the match stays green
        self.evidence = f"Recorded on {self.order_id.name} (no customer email sent)."
        return self.reconcile_id._reopen()
