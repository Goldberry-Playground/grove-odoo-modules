"""Pirate Ship label batch (GOL-2271, spec §B1).

Pirate Ship is the label channel (Josh, 2026-09-09): Odoo owns the batch, a
CSV round-trips through Pirate Ship (upload → buy → *Export Tracking Data*), and
the same files serve the automated ``grove-shipper`` runner AND the manual path.

Two models:

* ``grove.label.batch`` — one export/reconcile unit. ``open`` → ``exported`` →
  ``purchased`` | ``cancelled``. One open/exported batch per company at a time.
* ``grove.label.batch.line`` — one row per PACKED BOX (Box Engine v2). ``grove_ref``
  (``S01234/1``) is the round-trip key that survives into Pirate Ship's tracking
  export; the reconcile matches imported rows back to these lines.

The reconcile is **all-or-nothing**: every imported row is validated before any
order is written, and a single order is advanced only when *all* of its batch
lines have a matching, valid imported row. Any failure raises
``LabelBatchError`` — the controller turns that into a 400 with the offending
refs and nothing is written (the request cursor rolls back).
"""

import base64
import csv
import io
import logging
import re
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from .shipping_boxes import BOXES, actual_weight_lb
from .shipping_zones import box_rate, box_service_title

_logger = logging.getLogger(__name__)

# CSV columns, in order. Pirate Ship accepts arbitrary headers, per-row
# weight/dims, and carries imported columns through to its tracking export —
# which is exactly what makes the round trip safe (spec §B1).
CSV_COLUMNS = [
    "Grove Ref",
    "Order",
    "Name",
    "Email",
    "Phone",
    "Address 1",
    "Address 2",
    "City",
    "State",
    "Zip",
    "Country",
    "Weight (lb)",
    "Length",
    "Width",
    "Height",
    "Service",
    "Committed Rate",
    "Rubber Stamp 1",
]

# Grove carrier vocabulary. grove_shipping_carriers stores the canonical carrier
# KEY ("UPS"/"USPS") exactly as the Shippo path does — shipment_email.normalize_carrier
# and sub-project C's carrier-event poller (GOL-2272) both fold that field with
# normalize_carrier, which only recognises the bare key. Writing a two-word
# "UPS ups_ground" here would break the customer tracking-email link AND make the
# poller skip the box (unmappable carrier). The service token rides alongside in
# grove_shipping_services (again mirroring Shippo), from shippo_client's
# GROUND_SERVICE_ALLOWLIST. UPS Ground Saver still tracks on the UPS client, so it
# folds into the UPS pair. Map: Pirate Ship carrier prefix → (carrier_key, service_token).
_CARRIER_TOKENS = {
    "UPS": ("UPS", "ups_ground"),
    "USPS": ("USPS", "usps_ground_advantage"),
}

# Already-labelled guard (GOL-3083). A batch row whose order has been sitting
# awaiting a label longer than this many days is "likely already shipped by
# hand" (the duplicate-label failure in LB-20261005-01) and must be explicitly
# acknowledged before it is written to the Pirate Ship CSV. Operator-tunable via
# the ir.config_parameter below so the number is never a hard code constant.
AGE_ACK_DAYS_PARAM = "grove_headless.label_batch_age_ack_days"
DEFAULT_AGE_ACK_DAYS = 14

# Import-time cross-check window (GOL-3083 item 3): warn when a recipient already
# has a recorded (Purchased) label within this many days outside the current batch.
PRIOR_LABEL_WINDOW_DAYS = 60

# A Grove Ref value ("S01234/1"): order name + box index. Pirate Ship carries the
# upload's columns through to its tracking export, but the pass-through column is
# not always named "Grove Ref", so the reconcile also detects it by VALUE pattern.
GROVE_REF_RE = re.compile(r"^S\d+/\d+$")

# Magic bytes: xlsx is a zip (PK\x03\x04); legacy .xls is an OLE2 compound file.
_XLSX_MAGIC = b"PK\x03\x04"
_XLS_MAGIC = b"\xd0\xcf\x11\xe0"


class LabelBatchError(UserError):
    """Reconcile validation failure. Carries the offending Grove Refs so the
    endpoint can return them in a 400 body. Subclasses UserError so a manual
    import wizard surfaces it as a user-facing message too."""

    def __init__(self, message, refs=None):
        super().__init__(message)
        self.refs = list(refs or [])


def _carrier_token(carrier_raw):
    """Map a Pirate Ship export carrier string to a ``(carrier_key, service_token)``
    pair in the Grove vocabulary, or ``(None, None)`` when unmappable."""
    c = (carrier_raw or "").strip().upper()
    for prefix, pair in _CARRIER_TOKENS.items():
        if c.startswith(prefix):
            return pair
    return (None, None)


def _find_column(header, *needles):
    """Return the header cell whose lowercased text contains all of ``needles``,
    or None. Pirate Ship's *Export Tracking Data* column names are not a stable
    contract, so match loosely (case-insensitive substring) rather than pin an
    exact header we don't control."""
    for cell in header:
        low = (cell or "").strip().lower()
        if all(n in low for n in needles):
            return cell
    return None


class GroveLabelBatch(models.Model):
    _name = "grove.label.batch"
    _description = "Pirate Ship label batch"
    _order = "create_date desc, id desc"

    name = fields.Char(required=True, copy=False, readonly=True, index=True)
    state = fields.Selection(
        [
            ("open", "Open"),
            ("exported", "Exported"),
            ("purchased", "Purchased"),
            ("cancelled", "Cancelled"),
        ],
        default="open",
        required=True,
        index=True,
        copy=False,
    )
    company_id = fields.Many2one("res.company", required=True, index=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id", readonly=True)
    line_ids = fields.One2many("grove.label.batch.line", "batch_id", copy=False)
    order_ids = fields.Many2many("sale.order", copy=False)
    row_count = fields.Integer(compute="_compute_totals", store=True)
    expected_total = fields.Monetary(compute="_compute_totals", store=True, help="Sum of the committed per-box rates.")
    purchased_total = fields.Monetary(copy=False, help="Actual total charged at Pirate Ship.")
    purchased_at = fields.Datetime(copy=False)
    csv_export = fields.Binary(copy=False, attachment=True)
    csv_export_filename = fields.Char(copy=False)
    tracking_import = fields.Binary(copy=False, attachment=True)
    tracking_import_filename = fields.Char(copy=False)
    notes = fields.Text(copy=False)
    unacked_flagged_count = fields.Integer(
        compute="_compute_unacked_flagged",
        help="Rows flagged as likely-already-shipped (old) that have NOT been acknowledged. "
        "These are held OUT of the exported Pirate Ship CSV until confirmed (GOL-3083).",
    )

    _sql_constraints = [
        ("name_uniq", "unique(name)", "A label batch name must be unique."),
    ]

    @api.depends("line_ids.committed_rate")
    def _compute_totals(self):
        for batch in self:
            batch.row_count = len(batch.line_ids)
            batch.expected_total = round(sum(batch.line_ids.mapped("committed_rate")), 2)

    @api.depends("line_ids.age_flagged", "line_ids.age_ack")
    def _compute_unacked_flagged(self):
        for batch in self:
            batch.unacked_flagged_count = len(batch.line_ids.filtered(lambda ln: ln.age_flagged and not ln.age_ack))

    @api.model
    def _age_ack_days(self):
        """Already-labelled acknowledgement threshold in days (GOL-3083),
        from ``ir.config_parameter`` so an operator can tune it without a deploy.
        Falls back to ``DEFAULT_AGE_ACK_DAYS`` when unset or non-integer."""
        raw = self.env["ir.config_parameter"].sudo().get_param(AGE_ACK_DAYS_PARAM, DEFAULT_AGE_ACK_DAYS)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return DEFAULT_AGE_ACK_DAYS

    # ── Name allocation ──────────────────────────────────────────────────────
    @api.model
    def _next_name(self, company):
        """``LB-YYYYMMDD-NN`` — NN is the day's next free index for this company.

        Derived by scanning the day's existing names (not an ir.sequence) so the
        NN resets per day without date-range bookkeeping and the value is fully
        deterministic in tests."""
        today = fields.Date.context_today(self.with_company(company))
        prefix = f"LB-{today.strftime('%Y%m%d')}-"
        existing = self.sudo().search([("name", "=like", prefix + "%")])
        used = set()
        for rec in existing:
            tail = rec.name[len(prefix) :]
            if tail.isdigit():
                used.add(int(tail))
        nn = 1
        while nn in used:
            nn += 1
        return f"{prefix}{nn:02d}"

    # ── Open-batch discipline ──────────────────────────────────────────────
    @api.model
    def _open_batch(self, company):
        """The one non-terminal (open/exported) batch for ``company``, or an
        empty recordset. Enforces "one open batch at a time per company"."""
        return self.sudo().search(
            [("company_id", "=", company.id), ("state", "in", ("open", "exported"))],
            limit=1,
        )

    # ── Build / export ─────────────────────────────────────────────────────
    def _eligible_orders(self, company):
        """Orders that owe a Pirate Ship label right now: paid ship orders
        awaiting a label (and preorders whose wave has opened), never pickup,
        never already-tracked. Orders already assigned to THIS batch are kept so
        an idempotent re-export re-packs them (only orders grabbed by ANOTHER
        open batch are excluded). The dormancy/wave gate is applied per order by
        ``_grove_pack_for_label`` at build time (an order it refuses is skipped,
        not fatal), so this domain is the cheap pre-filter."""
        return (
            self.env["sale.order"]
            .sudo()
            .with_company(company)
            .search(
                [
                    ("company_id", "=", company.id),
                    ("grove_fulfillment", "!=", "pickup"),
                    ("grove_fulfillment_stage", "in", ("awaiting_label", "wave_assigned")),
                    ("grove_tracking_numbers", "in", (False, "")),
                    ("grove_label_batch_id", "in", [False] + (self.ids or [])),
                ]
            )
        )

    def _build_lines(self):
        """(Re)build this batch's rows from its eligible orders. Idempotent: an
        already-``exported`` batch rebuilds cleanly (drops stale lines, re-packs).
        Orders that cannot ship a label yet (wave closed, outside dormancy,
        unpriceable destination) are skipped and noted, never fatal."""
        self.ensure_one()
        company = self.company_id
        # Preserve already-given age acknowledgements across an idempotent
        # rebuild (GOL-3083): grove_ref (order name + box index) is stable, so an
        # operator who confirmed an aged row is not re-prompted after a re-export.
        acked_refs = {ln.grove_ref for ln in self.line_ids if ln.age_ack}
        self.line_ids.unlink()
        Line = self.env["grove.label.batch.line"].sudo()
        skipped = []
        packed_orders = self.env["sale.order"].sudo()
        for order in self._eligible_orders(company):
            try:
                address, plan, mode = order._grove_pack_for_label()
            except UserError as exc:
                skipped.append(f"{order.name}: {exc}")
                continue
            if not plan:
                continue
            for idx, pb in enumerate(plan, start=1):
                box = BOXES[pb.box_id]
                grove_ref = f"{order.name}/{idx}"
                Line.create(
                    {
                        "batch_id": self.id,
                        "grove_ref": grove_ref,
                        "order_id": order.id,
                        "box_index": idx,
                        "box_id": pb.box_id,
                        "recipient_name": address["name"] or "",
                        "email": address["email"] or "",
                        "phone": address["phone"] or "",
                        "street1": address["street1"] or "",
                        "street2": address["street2"] or "",
                        "city": address["city"] or "",
                        "state": address["state"] or "",
                        "zip": address["zip"] or "",
                        "country": address["country"] or "US",
                        "weight_lb": max(1.0, actual_weight_lb(pb.box_id, pb.count, mode)),
                        "length_in": box["length"],
                        "width_in": box["width"],
                        "height_in": box["height"],
                        "service": box_service_title(address["state"], pb.box_id),
                        "committed_rate": box_rate(address["state"], pb.box_id) or 0.0,
                    }
                )
            packed_orders |= order
        if acked_refs:
            self.line_ids.filtered(lambda ln: ln.grove_ref in acked_refs).age_ack = True
        packed_orders.write({"grove_label_batch_id": self.id})
        self.order_ids = [(6, 0, packed_orders.ids)]
        self.notes = ("Skipped (not shippable yet):\n" + "\n".join(skipped)) if skipped else False
        return packed_orders

    @api.model
    def build_open_batch(self, company):
        """Get-or-create the open batch for ``company`` and (re)build its rows.

        Idempotent per spec §B1: returns the same batch on repeated calls unless
        it is ``purchased``. A ``purchased`` open batch is impossible (purchased
        is terminal), so this always returns an ``open``/``exported`` batch with
        fresh rows. Marks the batch ``exported`` once it has any rows."""
        company = company or self.env.company
        batch = self._open_batch(company)
        if not batch:
            batch = self.sudo().create({"name": self._next_name(company), "company_id": company.id})
        batch._build_lines()
        batch._render_csv()
        if batch.line_ids:
            batch.state = "exported"
        return batch

    def _render_csv(self):
        """Serialise the rows to the Pirate Ship upload CSV and store it."""
        self.ensure_one()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(CSV_COLUMNS)
        for line in self.line_ids.sorted(key=lambda ln: (ln.order_id.name or "", ln.box_index)):
            # Already-labelled guard (GOL-3083): an aged, unacknowledged row is
            # likely already shipped by hand — never silently export it. It stays
            # visible (flagged) on the batch until the operator confirms it is not
            # already shipped (ticks the ack) or resolves it another way.
            if line.age_flagged and not line.age_ack:
                continue
            writer.writerow(line._csv_row())
        data = buf.getvalue().encode("utf-8")
        self.csv_export = base64.b64encode(data)
        self.csv_export_filename = f"{self.name}.csv"
        return data

    def csv_bytes(self):
        """The stored CSV as bytes (rendering it if absent). Used by the manual
        export server action and the runner's ``pull`` step."""
        self.ensure_one()
        if not self.csv_export:
            return self._render_csv()
        return base64.b64decode(self.csv_export)

    # ── Reconcile (tracking import) ─────────────────────────────────────────
    def _read_tabular(self, raw_bytes, filename=None):
        """Read a Pirate Ship export (CSV *or* .xls/.xlsx) into a list of string
        rows (GOL-3083 item 4). Pirate Ship exports .xls/.xlsx, not CSV, so the
        importer must accept them directly — xlrd/openpyxl ship with Odoo.

        Format is sniffed from the leading magic bytes (robust when the upload
        carries no/filename-less extension); ``filename`` is only a fallback hint.
        Every cell is coerced to a trimmed string so the downstream parser sees the
        same shape regardless of source. Integral spreadsheet floats are rendered
        without a trailing ``.0`` (so a cost cell ``9.0`` and a numeric ZIP survive
        cleanly)."""
        head = raw_bytes[:4] if isinstance(raw_bytes, (bytes, bytearray)) else b""
        name = (filename or "").lower()
        if head == _XLSX_MAGIC or name.endswith(".xlsx"):
            return self._read_xlsx(raw_bytes)
        if head == _XLS_MAGIC or name.endswith(".xls"):
            return self._read_xls(raw_bytes)
        try:
            text = raw_bytes.decode("utf-8-sig")
        except (UnicodeDecodeError, AttributeError) as exc:
            raise LabelBatchError(f"Tracking file is not valid UTF-8 CSV: {exc}") from exc
        return [[(c or "").strip() for c in row] for row in csv.reader(io.StringIO(text))]

    @api.model
    def _cell_str(self, value):
        """Render a spreadsheet cell as a trimmed string without a spurious
        ``.0`` on integral floats (openpyxl/xlrd hand back numbers as floats)."""
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value).strip()

    def _read_xlsx(self, raw_bytes):
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover - openpyxl ships with Odoo
            raise LabelBatchError("Cannot read .xlsx: openpyxl is not installed on this server.") from exc
        try:
            wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
        except Exception as exc:
            raise LabelBatchError(f"Tracking file is not a readable .xlsx workbook: {exc}") from exc
        ws = wb.active
        rows = [[self._cell_str(c) for c in row] for row in ws.iter_rows(values_only=True)]
        wb.close()
        return rows

    def _read_xls(self, raw_bytes):
        try:
            import xlrd
        except ImportError as exc:  # pragma: no cover - xlrd ships with Odoo
            raise LabelBatchError("Cannot read .xls: xlrd is not installed on this server.") from exc
        try:
            book = xlrd.open_workbook(file_contents=raw_bytes)
        except Exception as exc:
            raise LabelBatchError(f"Tracking file is not a readable .xls workbook: {exc}") from exc
        sheet = book.sheet_by_index(0)
        return [[self._cell_str(sheet.cell_value(r, c)) for c in range(sheet.ncols)] for r in range(sheet.nrows)]

    @staticmethod
    def _detect_ref_idx(rows):
        """Index of the column whose values look like Grove Refs (``S\\d+/\\d+``),
        or None. Pirate Ship may rename the pass-through Grove Ref column, so the
        reconcile detects it by VALUE when the header name is absent (GOL-3083
        item 4). A column qualifies when at least half of its non-empty cells
        match the pattern."""
        if len(rows) < 2:
            return None
        ncols = max((len(r) for r in rows), default=0)
        for ci in range(ncols):
            nonempty = [r[ci] for r in rows[1:] if ci < len(r) and r[ci]]
            if not nonempty:
                continue
            hits = sum(1 for v in nonempty if GROVE_REF_RE.match(v))
            if hits and hits >= len(nonempty) / 2:
                return ci
        return None

    def _parse_tracking_file(self, raw_bytes, filename=None):
        """Parse a Pirate Ship *Export Tracking Data* file (CSV/.xls/.xlsx) into
        row dicts.

        Loose header matching (§B1): tracking number, carrier and cost are always
        required. For IDENTITY we need *either* Grove Ref (the proven round-trip
        key — matched by header name OR, when Pirate Ship renamed the pass-through
        column, by the ``S\\d+/\\d+`` value pattern, GOL-3083) *or* Email — the
        manual-review fallback. The real 2026-09-15 per-recipient export carried
        NO Grove Ref column at all (columns were Created Date, Recipient, Email,
        Tracking Number, Cost, …), so refusing a file that lacks Grove Ref would
        dead-end the operators' actual export. Raises ``LabelBatchError`` only when
        a genuinely required column (tracking / carrier / cost) or *both* identity
        columns are absent."""
        rows = self._read_tabular(raw_bytes, filename=filename)
        if not rows:
            raise LabelBatchError("Tracking file is empty.")
        header = rows[0]

        def idx(*needles):
            cell = _find_column(header, *needles)
            return header.index(cell) if cell is not None else None

        i_ref = idx("grove", "ref")
        if i_ref is None:
            i_ref = self._detect_ref_idx(rows)
        i_track = idx("tracking", "number") or idx("tracking") or idx("track")
        i_carrier = idx("carrier")
        i_cost = idx("cost") or idx("amount") or idx("charge") or idx("price")
        i_service = idx("service") or idx("mail", "class")
        i_email = idx("email")
        i_recipient = idx("recipient") or idx("name")
        # Shipments-export-only columns (GOL-3091 legacy reconcile): the delivery
        # status ("Delivered") and the label/ship date. Absent on the normal
        # tracking export — purely additive, so import_tracking (which reads by
        # named key) is unaffected.
        i_status = idx("status")
        i_date = (
            idx("delivered", "date")
            or idx("ship", "date")
            or idx("label", "date")
            or idx("created", "date")
            or idx("date")
        )
        missing = [
            label for label, i in (("Tracking Number", i_track), ("Carrier", i_carrier), ("Cost", i_cost)) if i is None
        ]
        if missing:
            raise LabelBatchError(f"Tracking file is missing required column(s): {', '.join(missing)}.")
        if i_ref is None and i_email is None:
            raise LabelBatchError(
                "Tracking file has neither a Grove Ref nor an Email column; cannot match rows to the batch."
            )
        parsed = []
        for raw in rows[1:]:
            if not any((c or "").strip() for c in raw):
                continue  # blank line

            def cell(i):
                return raw[i].strip() if i is not None and i < len(raw) else ""

            parsed.append(
                {
                    "ref": cell(i_ref),
                    "tracking": cell(i_track),
                    "carrier_raw": cell(i_carrier),
                    "service": cell(i_service),
                    "cost_raw": cell(i_cost),
                    "email": cell(i_email),
                    "recipient": cell(i_recipient),
                    "status": cell(i_status),
                    "date": cell(i_date),
                }
            )
        return parsed

    @api.model
    def _order_emails(self, order):
        """Lowercased recipient email(s) for an order (ship-to first, then the
        order partner). Used to spot a recipient that already has a recorded label
        (GOL-3083 item 3)."""
        emails = set()
        for partner in (order.partner_shipping_id, order.partner_id):
            if partner and partner.email:
                emails.add(partner.email.strip().lower())
        return emails

    def _recent_labelled_by_email(self, exclude_orders, within_days=PRIOR_LABEL_WINDOW_DAYS):
        """Index recipient email → orders that already have a recorded label within
        ``within_days``, EXCLUDING this batch and ``exclude_orders`` (GOL-3083 item
        3). A hard refund is not recorded in Odoo, so "Purchased, non-refunded"
        reduces to "has recorded tracking" — a sibling order whose hand-bought
        label WAS recorded is exactly the LB-20261005-01 duplicate signature."""
        self.ensure_one()
        cutoff = fields.Datetime.now() - timedelta(days=within_days)
        recent = (
            self.env["sale.order"]
            .sudo()
            .with_company(self.company_id)
            .search(
                [
                    ("company_id", "=", self.company_id.id),
                    ("grove_label_purchased_at", ">=", cutoff),
                    ("grove_tracking_numbers", "not in", (False, "")),
                    ("grove_label_batch_id", "!=", self.id),
                    ("id", "not in", [o.id for o in exclude_orders]),
                ]
            )
        )
        index = {}
        for order in recent:
            for email in self._order_emails(order):
                index.setdefault(email, self.env["sale.order"])
                index[email] |= order
        return index

    def import_tracking(self, raw_bytes, filename=None):
        """All-or-nothing reconcile (spec §B1). Validate EVERY row before any
        write; advance an order only when all of its batch lines matched. Rows are
        matched on Grove Ref (the proven round-trip key) and, for rows that arrive
        without one, on recipient email as an unambiguous-only fallback (§B1 field
        report 2026-09-15) — any email-matched order is flagged for manual review,
        never written silently. Returns ``{orders_advanced, skipped_already_tracked,
        total, rows, manual_review, warnings}`` (``manual_review`` = grove_refs
        reconciled by the email fallback; ``warnings`` = non-fatal already-labelled
        notices, GOL-3083 item 3). Raises ``LabelBatchError`` (→ 400) on any
        validation failure, writing nothing."""
        self.ensure_one()
        if self.state == "cancelled":
            raise LabelBatchError(f"{self.name} is cancelled; cannot import tracking.")
        parsed = self._parse_tracking_file(raw_bytes, filename=filename)
        if filename:
            self.tracking_import = base64.b64encode(raw_bytes)
            self.tracking_import_filename = filename

        lines_by_ref = {line.grove_ref: line for line in self.line_ids}
        # Email fallback index (spec §B1, 2026-09-15 field report): lowercased
        # recipient email → the batch lines carrying it. Used ONLY for rows that
        # arrive without a usable Grove Ref, and ONLY when the email resolves to a
        # single unclaimed line. Multi-box orders and duplicate emails (two labels
        # across channels, the case that needed human judgment in the real run)
        # stay ambiguous and hard-fail — the fallback never silently guesses.
        empty_lines = self.env["grove.label.batch.line"]
        lines_by_email = {}
        for line in self.line_ids:
            key = (line.email or "").strip().lower()
            if key:
                lines_by_email[key] = lines_by_email.get(key, empty_lines) | line
        errors = []
        seen_refs = set()
        claimed = set()  # line ids already matched by an earlier import row
        clean = []  # (row, line, cost, carrier_key, service_token, via_email)
        for row in parsed:
            ref = row["ref"]
            via_email = False
            if ref:
                if ref in seen_refs:
                    errors.append(f"{ref}: appears more than once in the import")
                    continue
                seen_refs.add(ref)
                line = lines_by_ref.get(ref)
                if not line:
                    errors.append(f"{ref}: not a row in {self.name}")
                    continue
            else:
                # No Grove Ref on this row → loud, unambiguous-only email fallback.
                email = (row["email"] or "").strip().lower()
                who = row["recipient"] or row["email"] or "(row with no Grove Ref)"
                if not email:
                    errors.append(f"{who}: no Grove Ref and no Email — cannot match to {self.name}")
                    continue
                candidates = lines_by_email.get(email, empty_lines).filtered(lambda ln: ln.id not in claimed)
                if not candidates:
                    errors.append(f"{who} <{email}>: no unmatched batch row for this email in {self.name}")
                    continue
                if len(candidates) > 1:
                    errors.append(
                        f"{who} <{email}>: ambiguous — matches {', '.join(sorted(candidates.mapped('grove_ref')))}; "
                        f"re-upload with the Grove Ref column or reconcile manually"
                    )
                    continue
                line = candidates
                via_email = True
            if line.id in claimed:
                errors.append(f"{line.grove_ref}: matched by more than one import row")
                continue
            if not self.env["grove.label.batch"]._is_valid_tracking(row["tracking"]):
                errors.append(f"{line.grove_ref}: invalid tracking number {row['tracking']!r}")
                continue
            carrier_key, service_token = _carrier_token(row["carrier_raw"])
            if not carrier_key:
                errors.append(f"{line.grove_ref}: unrecognised carrier {row['carrier_raw']!r}")
                continue
            try:
                cost = round(float(row["cost_raw"].replace("$", "").replace(",", "")), 2)
            except (TypeError, ValueError):
                errors.append(f"{line.grove_ref}: non-numeric cost {row['cost_raw']!r}")
                continue
            claimed.add(line.id)
            clean.append((row, line, cost, carrier_key, service_token, via_email))

        # Group by order and enforce per-order completeness + state.
        rows_by_order = {}
        for row, line, cost, carrier_key, service_token, via_email in clean:
            rows_by_order.setdefault(line.order_id, []).append((line, cost, carrier_key, service_token, row, via_email))
        orders_to_write = []
        skipped_already = 0
        for order, entries in rows_by_order.items():
            batch_line_refs = {bl.grove_ref for bl in self.line_ids.filtered(lambda x: x.order_id == order)}
            got_refs = {ln.grove_ref for (ln, _c, _ca, _s, _r, _v) in entries}
            if order.grove_tracking_numbers:
                # Idempotent re-import: this order was already reconciled — skip
                # every one of its rows, never re-write (spec: refuses to re-write
                # tracked orders).
                skipped_already += len(entries)
                continue
            stage = order.grove_fulfillment_stage
            if stage not in ("awaiting_label", "wave_assigned"):
                errors.append(f"{order.name}: not awaiting a label (stage {stage})")
                continue
            missing_refs = batch_line_refs - got_refs
            if missing_refs:
                errors.append(f"{order.name}: incomplete tracking — missing {', '.join(sorted(missing_refs))}")
                continue
            orders_to_write.append((order, entries))

        if errors:
            raise LabelBatchError(
                "Tracking import rejected; nothing written. " + "; ".join(errors),
                refs=[e.split(":")[0] for e in errors],
            )

        # ── All rows valid: write. ──────────────────────────────────────────
        orders_advanced = 0
        newly_total = 0.0
        manual_review = []  # grove_refs reconciled via the weaker email fallback
        warnings = []  # non-fatal already-labelled notices (GOL-3083 item 3)
        prior_by_email = self._recent_labelled_by_email([o for o, _e in orders_to_write])
        for order, entries in orders_to_write:
            entries.sort(key=lambda e: e[0].box_index)
            tracking = [row["tracking"] for (_ln, _c, _ca, _s, row, _v) in entries]
            carriers = [carrier_key for (_ln, _c, carrier_key, _s, _r, _v) in entries]
            services = [service_token for (_ln, _c, _ca, service_token, _r, _v) in entries]
            actual = round(sum(cost for (_ln, cost, _ca, _s, _r, _v) in entries), 2)
            fallback_refs = sorted(ln.grove_ref for (ln, _c, _ca, _s, _r, via_email) in entries if via_email)
            # Advance the watermark FIRST, while the order still derives to
            # awaiting_label/wave_assigned. Setting grove_delivery_status =
            # "label_purchased" up front would make the derived stage already
            # label_purchased, so _grove_advance_state would no-op (no watermark,
            # no audit note). Advancing stamps grove_label_purchased_at + the
            # chatter note; the customer shipment email itself is fired later by
            # the GOL-2272 carrier poll once UPS/USPS reports transit — which is
            # exactly why grove_shipping_carriers below must carry the canonical
            # "UPS"/"USPS" key the poll folds with normalize_carrier.
            order._grove_advance_state("label_purchased", source="pirateship")
            order.write(
                {
                    "grove_tracking_numbers": "\n".join(tracking),
                    "grove_shipping_carriers": "\n".join(carriers),
                    "grove_shipping_services": "\n".join(services),
                    "grove_label_urls": "\n".join([""] * len(tracking)),  # labels print from Pirate Ship
                    "grove_actual_shipping_cost": actual,
                    "grove_delivery_status": "label_purchased",
                }
            )
            for line, cost, carrier_key, service_token, row, _via in entries:
                line.write(
                    {
                        "tracking_number": row["tracking"],
                        "carrier_token": carrier_key,
                        "service_token": service_token,
                        "actual_cost": cost,
                    }
                )
            # Every box is bought and the ACTUAL shipping cost is persisted, so a
            # deposit-only (preorder) balance can settle off-session now — exactly
            # as the Shippo path does right after its own label_purchased advance
            # (GOL-2053, action_buy_shipping_labels). Without this the balance only
            # captures later at operator mark-shipped; an order the GOL-2272 carrier
            # poll advances straight to shipped/delivered bypasses that seam and the
            # deferred balance strands (GOL-2901, same class as GOL-2893). Best-effort
            # by contract: _grove_settle_at_ship is idempotent (already_settled
            # no-op) and never raises, so a decline cannot roll back a persisted
            # label.
            order._grove_settle_at_ship()
            if fallback_refs:
                # Loud, per-order flag: this order reconciled without a Grove Ref
                # round-trip (matched on recipient email). A human should confirm
                # the tracking landed on the right order before we trust it.
                manual_review.extend(fallback_refs)
                order.message_post(
                    body=(
                        f"⚠️ Tracking reconciled by EMAIL FALLBACK from {self.name} "
                        f"(no Grove Ref in the upload): {', '.join(fallback_refs)}. "
                        f"Verify the tracking number belongs to this order."
                    )
                )
            # Already-labelled cross-check (GOL-3083 item 3): warn, never block,
            # when this recipient already has a recorded label elsewhere recently.
            priors = self.env["sale.order"]
            for email in self._order_emails(order):
                priors |= prior_by_email.get(email, self.env["sale.order"])
            if priors:
                parts = []
                for p in priors:
                    track = (p.grove_tracking_numbers or "").splitlines()
                    when = f", {p.grove_label_purchased_at.date()}" if p.grove_label_purchased_at else ""
                    parts.append(f"{p.name} ({track[0] if track else 'no tracking'}{when})")
                msg = (
                    f"{order.name}: recipient already has a recent recorded label on {', '.join(parts)}. "
                    f"Confirm this is not a duplicate of an order already shipped by hand (GOL-3083)."
                )
                warnings.append(msg)
                order.message_post(body="⚠️ " + msg)
            orders_advanced += 1
            newly_total += actual

        if orders_advanced:
            batch_vals = {
                "state": "purchased",
                "purchased_total": round((self.purchased_total or 0.0) + newly_total, 2),
                "purchased_at": fields.Datetime.now(),
            }
            if manual_review:
                stamp = fields.Datetime.now()
                note = f"[{stamp}] EMAIL-FALLBACK reconcile (no Grove Ref): {', '.join(manual_review)}"
                batch_vals["notes"] = (self.notes + "\n" + note) if self.notes else note
            self.write(batch_vals)
        return {
            "orders_advanced": orders_advanced,
            "skipped_already_tracked": skipped_already,
            "total": round(newly_total, 2),
            "rows": len(clean),
            "manual_review": sorted(manual_review),
            "warnings": warnings,
        }

    @api.model
    def _is_valid_tracking(self, value):
        """Thin wrapper over shippo_client.is_valid_tracking (kept as the pure
        helper per spec §B1) so tests can stub it and the model has one seam."""
        from .shippo_client import is_valid_tracking

        return is_valid_tracking(value)

    # ── UI actions (manual path — always available, spec §B1) ───────────────
    def action_export_csv(self):
        """Download the batch CSV (Fulfillment menu → *Export Pirate Ship batch*)."""
        self.ensure_one()
        if self.state not in ("open", "exported"):
            raise UserError(f"{self.name} is {self.state}; only open/exported batches export.")
        # Re-render (not csv_bytes()) so a just-ticked already-labelled
        # acknowledgement is reflected: _render_csv reads age_ack live and now
        # includes the row the operator confirmed still needs a label (GOL-3083).
        self._render_csv()
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/grove.label.batch/{self.id}/csv_export/{self.csv_export_filename}?download=true",
            "target": "self",
        }

    def action_cancel(self):
        """Cancel an open/exported batch: release its orders back to the pool."""
        self.ensure_one()
        if self.state == "purchased":
            raise UserError(f"{self.name} is purchased and cannot be cancelled.")
        self.order_ids.write({"grove_label_batch_id": False})
        self.state = "cancelled"


class GroveLabelBatchLine(models.Model):
    _name = "grove.label.batch.line"
    _description = "Pirate Ship label batch row (one packed box)"
    _order = "order_id, box_index"

    batch_id = fields.Many2one("grove.label.batch", required=True, ondelete="cascade", index=True)
    grove_ref = fields.Char(required=True, index=True)
    order_id = fields.Many2one("sale.order", required=True, ondelete="cascade", index=True)
    box_index = fields.Integer(required=True)
    box_id = fields.Char(required=True)
    recipient_name = fields.Char()
    email = fields.Char()
    phone = fields.Char()
    street1 = fields.Char()
    street2 = fields.Char()
    city = fields.Char()
    state = fields.Char()
    zip = fields.Char()
    country = fields.Char(default="US")
    weight_lb = fields.Float(digits=(6, 1))
    length_in = fields.Integer()
    width_in = fields.Integer()
    height_in = fields.Integer()
    service = fields.Char(help="Carrier/service title (informational for the buyer).")
    committed_rate = fields.Float(digits=(8, 2))
    # Already-labelled guard (GOL-3083). order_age_days / age_flagged are NOT
    # stored: they are read-time derived from "now" so the form always shows a
    # fresh age. age_ack is the operator's explicit confirmation that an aged row
    # has NOT already been shipped by hand; it is preserved across rebuilds.
    order_date = fields.Datetime(related="order_id.date_order", string="Order date", store=True)
    order_age_days = fields.Integer(compute="_compute_order_age", string="Age (days)")
    age_flagged = fields.Boolean(
        compute="_compute_order_age",
        string="Likely already shipped",
        help="Order has been awaiting a label longer than the acknowledgement threshold "
        "(grove_headless.label_batch_age_ack_days, default 14) — confirm it was not already "
        "shipped by hand before exporting it (GOL-3083).",
    )
    age_ack = fields.Boolean(
        string="Confirmed not already shipped",
        copy=False,
        help="Tick to confirm this aged order has NOT already been shipped on a hand-bought "
        "label. Required before an aged row is written to the Pirate Ship CSV (GOL-3083).",
    )
    # Written back on reconcile.
    tracking_number = fields.Char(copy=False)
    carrier_token = fields.Char(copy=False, help="Canonical carrier key (UPS/USPS), as stored on the order.")
    service_token = fields.Char(copy=False, help="Ground service token (ups_ground/usps_ground_advantage).")
    actual_cost = fields.Float(digits=(8, 2), copy=False)

    @api.depends("order_date", "order_id.grove_fulfillment_stage")
    def _compute_order_age(self):
        """Age-since-order and the already-labelled flag (GOL-3083).

        Only a regular paid ship order (``awaiting_label``) is age-flagged: a
        ``wave_assigned`` preorder is expected to be old (ordered seasons before
        its wave opens), so age-since-order is not a shipped-by-hand signal for it
        — the import-time email cross-check and the reconcile wizard cover that
        case instead."""
        threshold = self.env["grove.label.batch"]._age_ack_days()
        now = fields.Datetime.now()
        for line in self:
            line.order_age_days = (now - line.order_date).days if line.order_date else 0
            line.age_flagged = (
                line.order_id.grove_fulfillment_stage == "awaiting_label" and line.order_age_days >= threshold
            )

    def _csv_row(self):
        """This line as a CSV_COLUMNS-ordered list of strings."""
        self.ensure_one()
        return [
            self.grove_ref,
            self.order_id.name or "",
            self.recipient_name or "",
            self.email or "",
            self.phone or "",
            self.street1 or "",
            self.street2 or "",
            self.city or "",
            self.state or "",
            self.zip or "",
            self.country or "US",
            f"{self.weight_lb:.1f}",
            str(self.length_in),
            str(self.width_in),
            str(self.height_in),
            self.service or "",
            f"{self.committed_rate:.2f}",
            self.order_id.name or "",  # Rubber Stamp 1
        ]
