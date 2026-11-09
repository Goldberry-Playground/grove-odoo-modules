"""Pirate Ship label batch (GOL-2271, spec §B1).

Pirate Ship is the label channel (Josh, 2026-09-09): Odoo owns the batch, a
CSV round-trips through Pirate Ship (upload → buy → *Export Tracking Data*), and
the same files serve the automated ``grove-shipper`` runner AND the manual path.

Two models:

* ``grove.label.batch`` — one export/reconcile unit. ``open`` → ``exported`` →
  ``purchased`` | ``cancelled``. One open/exported batch per company and USDA
  zone at a time: spring orders are batched by zone, and every other order is
  fulfilled one at a time.
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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import config

from .shipping_boxes import BOXES
from .shipping_calendar import (
    SPRING_BUILD_LEAD_DAYS,
    USDA_ZONE_SELECTION,
    merge_calendar_override,
    spring_build_window,
)
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

# A bare order number ("S00245"): the batch CSV writes it in the Order and Rubber
# Stamp columns, and an operator can type it into Pirate Ship for a hand-bought
# label, so the tolerant import can place a row even when it has no Grove Ref.
ORDER_NAME_RE = re.compile(r"^S\d{3,}$")

# A Pirate Ship export row whose status says the label no longer ships.
REFUNDED_STATUS_RE = re.compile(r"refund|void|cancel", re.IGNORECASE)

# Magic bytes: xlsx is a zip (PK\x03\x04); legacy .xls is an OLE2 compound file.
_XLSX_MAGIC = b"PK\x03\x04"
_XLS_MAGIC = b"\xd0\xcf\x11\xe0"


# The shop's day: Mark all shipped waits for the zone's start date in New York.
_SHOP_TZ = ZoneInfo("America/New_York")
# Stages Mark all shipped leaves alone because the box has already left.
_SHIPPED_STAGES = ("shipped", "delivered")


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
    # Optional in the database because older closed batches have none. Every newly
    # built batch has one: build_open_batch refuses a blank zone.
    zone = fields.Selection(selection=USDA_ZONE_SELECTION, string="USDA Zone", index=True, readonly=True, copy=False)
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
    _one_open_per_zone = models.UniqueIndex(
        "(company_id, zone) WHERE state IN ('open', 'exported') AND zone IS NOT NULL",
        "A company can have only one open label batch per zone.",
    )

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
    def _open_batches(self, company):
        """Every non-terminal (open/exported) batch for ``company``."""
        return self.sudo().search([("company_id", "=", company.id), ("state", "in", ("open", "exported"))])

    @api.model
    def _open_batch(self, company, zone):
        """The one non-terminal batch for ``company`` and ``zone``, or an empty
        recordset. Enforces "one open batch per company and zone"."""
        return self.sudo().search(
            [("company_id", "=", company.id), ("zone", "=", zone), ("state", "in", ("open", "exported"))],
            limit=1,
        )

    # ── Build / export ─────────────────────────────────────────────────────
    def _eligible_orders(self, company, zone=None):
        """Orders that owe a Pirate Ship label right now, never pickup, Printify
        or already-tracked.

        With ``zone``, the orders a zone batch takes: spring-lane orders in that
        zone that are not held or cancelled, including those still at
        ``deposit_paid`` (the build assigns them to their wave). An order left
        with no label in an already-purchased batch can join a new one, where
        its rows wait for confirmation (see ``_earlier_batches_without_label``).

        Without ``zone``, every paid ship order awaiting a label (and preorders
        whose wave has opened), for the legacy reconcile.

        Orders already assigned to THIS batch are kept so an idempotent
        re-export re-packs them (only orders grabbed by ANOTHER open batch are
        excluded). The dormancy/wave gate is applied per order by
        ``_grove_pack_for_label`` at build time (an order it refuses is skipped,
        not fatal), so this domain is the cheap pre-filter."""
        domain = [
            ("company_id", "=", company.id),
            ("grove_fulfillment", "!=", "pickup"),
            # Printify prints and ships its own orders; they never need a
            # Pirate Ship label (spec Review Focus 2, GOL-3314).
            ("grove_is_printify", "=", False),
            ("grove_tracking_numbers", "in", (False, "")),
        ]
        own_or_free = ("grove_label_batch_id", "in", [False] + (self.ids or []))
        if zone:
            domain += [
                ("grove_lane", "=", "spring"),
                ("grove_usda_zone", "=", zone),
                ("grove_hold_until", "=", False),
                ("state", "!=", "cancel"),
                ("grove_fulfillment_stage", "in", ("deposit_paid", "awaiting_label", "wave_assigned")),
                "|",
                own_or_free,
                ("grove_label_batch_id.state", "=", "purchased"),
            ]
        else:
            domain += [("grove_fulfillment_stage", "in", ("awaiting_label", "wave_assigned")), own_or_free]
        return self.env["sale.order"].sudo().with_company(company).search(domain)

    def _earlier_batches_without_label(self, orders):
        """``{order id: batch}`` for orders with a row in an earlier purchased
        batch that never recorded a label.

        A purchased batch can hold an order whose label really was bought but
        whose import row was rejected (incomplete tracking), so a new batch must
        not offer it again unasked: its rows stay out of the CSV until confirmed,
        the same way as GOL-3083's aged rows. A row that did record a label, then
        refunded, does not count."""
        lines = (
            self.env["grove.label.batch.line"]
            .sudo()
            .search(
                [
                    ("order_id", "in", orders.ids),
                    ("batch_id", "!=", self.id),
                    ("batch_id.state", "=", "purchased"),
                    ("tracking_number", "in", (False, "")),
                ]
            )
        )
        return {line.order_id.id: line.batch_id for line in lines.sorted(key=lambda ln: ln.batch_id.id)}

    def _build_lines(self):
        """(Re)build this zone batch's rows from its eligible orders. Returns
        ``(skipped, needs_confirmation)``: the orders left out with why, and the
        orders whose rows wait for confirmation. Idempotent: an already-``exported``
        batch rebuilds cleanly (drops stale lines, re-packs).

        Each order is assigned to its wave and packed inside its own savepoint,
        so one that cannot ship a label yet (outside dormancy, unpriceable
        destination) is left out and stays exactly as it was. An order that has
        dropped out since the last build is released so a later batch can take
        it."""
        self.ensure_one()
        company = self.company_id
        # Preserve already-given age acknowledgements across an idempotent
        # rebuild (GOL-3083): grove_ref (order name + box index) is stable, so an
        # operator who confirmed an aged row is not re-prompted after a re-export.
        acked_refs = {ln.grove_ref for ln in self.line_ids if ln.age_ack}
        previous_orders = self.order_ids
        self.line_ids.unlink()
        skipped = []
        Order = self.env["sale.order"].sudo()
        packed_orders = failed_orders = Order
        eligible = self._eligible_orders(company, zone=self.zone)
        earlier_batches = self._earlier_batches_without_label(eligible)
        for order in eligible:
            try:
                with self.env.cr.savepoint():
                    self._pack_order_into_batch(order, earlier_batches.get(order.id))
            except (UserError, KeyError, ValueError) as exc:
                if not isinstance(exc, UserError):
                    _logger.warning("%s could not be packed into %s", order.name, self.name, exc_info=True)
                reason = str(exc)
                skipped.append(reason if reason.startswith(order.name) else f"{order.name}: {reason}")
                failed_orders |= order
                continue
            packed_orders |= order
        if acked_refs:
            self.line_ids.filtered(lambda ln: ln.grove_ref in acked_refs).age_ack = True
        for order in (previous_orders - packed_orders).filtered(lambda o: o.grove_label_batch_id == self):
            order.grove_label_batch_id = False
            order.message_post(body=f"Order released from {self.name}: a rebuild no longer includes it.")
            if order not in failed_orders:
                skipped.append(f"{order.name}: released from {self.name}; it no longer qualifies for this batch")
        packed_orders.write({"grove_label_batch_id": self.id})
        self.order_ids = [(6, 0, packed_orders.ids)]
        unconfirmed = self.line_ids.filtered(lambda ln: ln.prior_batch_id and not ln.age_ack)
        needs_confirmation = [
            f"{order.name} was in {batch.name} with no label recorded; confirm none was bought"
            for order, batch in {ln.order_id: ln.prior_batch_id for ln in unconfirmed}.items()
        ]
        sections = []
        if skipped:
            sections.append("Skipped (not shippable yet):\n" + "\n".join(skipped))
        if needs_confirmation:
            sections.append("Held out of the CSV until confirmed:\n" + "\n".join(needs_confirmation))
        self.notes = "\n\n".join(sections) or False
        return skipped, needs_confirmation

    def _pack_order_into_batch(self, order, prior_batch=None):
        """Assign one order to its wave if needed and write its box rows. Raises
        ``UserError`` when it cannot be labelled yet. ``prior_batch`` marks its
        rows as needing confirmation (``_earlier_batches_without_label``)."""
        self.ensure_one()
        if order.grove_fulfillment_stage == "deposit_paid":
            actor = self.env.user.name
            if not order.action_grove_assign_wave(wave_ref=f"spring (batch {self.name}, built by {actor})"):
                raise UserError(f"{order.name}: could not be assigned to its ship wave.")
        # build_open_batch has checked spring_build_window before any order gets here.
        address, plan, mode = order._grove_pack_for_label(ignore_ship_window=True)
        if not plan:
            raise UserError(f"{order.name}: no boxes to label.")
        Line = self.env["grove.label.batch.line"].sudo()
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
                    # Tier-aware (GOL-3291): a potted box in the shared p-box
                    # carton declares its potted weight, not the lighter
                    # bareroot dormant weight the box id alone would pick.
                    "weight_lb": max(1.0, pb.declared_weight_lb(mode)),
                    "length_in": box["length"],
                    "width_in": box["width"],
                    "height_in": box["height"],
                    "service": box_service_title(address["state"], pb.box_id),
                    "committed_rate": box_rate(address["state"], pb.box_id) or 0.0,
                    "prior_batch_id": prior_batch.id if prior_batch else False,
                }
            )

    @api.model
    def _check_build_window(self, company, zone):
        """Refuse a build outside the zone's spring window, naming when it opens."""
        from ..controllers.main import _parse_calendar_override

        today = fields.Date.context_today(self.with_company(company))
        calendar = merge_calendar_override(_parse_calendar_override(self.env))
        date_open, date_close = spring_build_window(int(zone), today, calendar)
        if not date_open <= today <= date_close:
            raise UserError(
                f"Zone {zone}'s spring batch can't be built today. It opens "
                f"{date_open:%B} {date_open.day}, {date_open.year}, 5 days before that zone starts shipping."
            )

    @api.model
    def build_open_batch(self, company, zone):
        """Get-or-create the open batch for ``company`` and ``zone`` and (re)build
        its rows. Idempotent per spec §B1: returns the same batch on repeated
        calls until it is purchased. Marks the batch ``exported`` once it has rows.

        Returns ``{"batch", "skipped", "needs_confirmation", "message"}``:
        ``skipped`` names the orders left out and why, ``needs_confirmation`` the
        orders held out of the CSV until someone confirms no label was bought.
        When nothing could be batched, ``batch`` is empty, ``message`` says so,
        and no empty open batch is left behind."""
        self.env["sale.order"]._grove_require_order_operator()
        company = company or self.env.company
        if not self.env.is_superuser() and company not in self.env.companies:
            raise AccessError("You can only build label batches for your own companies.")
        zone = str(zone) if zone else ""
        if zone not in dict(USDA_ZONE_SELECTION):
            raise UserError("Pick a USDA zone from 2 to 10 to build a batch.")
        self._check_build_window(company, zone)
        batch = self._open_batch(company, zone)
        created = not batch
        if created:
            batch = self.sudo().create({"name": self._next_name(company), "company_id": company.id, "zone": zone})
        skipped, needs_confirmation = batch._build_lines()
        if not batch.line_ids:
            if created:
                batch.unlink()
            else:
                batch.action_cancel()
            message = f"Nothing to batch for zone {zone}."
            if skipped:
                message += " These orders were left out:\n" + "\n".join(skipped)
            return {"batch": self.browse(), "skipped": skipped, "needs_confirmation": [], "message": message}
        batch._render_csv()
        batch.state = "exported"
        return {"batch": batch, "skipped": skipped, "needs_confirmation": needs_confirmation, "message": None}

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

    @staticmethod
    def _detect_order_idx(rows):
        """Index of the column whose values look like bare order numbers
        (``S00245``), or None. Same at-least-half rule as ``_detect_ref_idx``."""
        if len(rows) < 2:
            return None
        ncols = max((len(r) for r in rows), default=0)
        for ci in range(ncols):
            nonempty = [r[ci] for r in rows[1:] if ci < len(r) and r[ci]]
            if not nonempty:
                continue
            hits = sum(1 for v in nonempty if ORDER_NAME_RE.match(v))
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

        def first_idx(*needle_groups):
            # First matching column index across the candidate header names.
            # NB: a hit at column 0 is a valid index — test each result against
            # ``None`` explicitly rather than ``or``-chaining (``0 or next`` would
            # silently skip a column that happens to be first, GOL-3104).
            for needles in needle_groups:
                i = idx(*needles)
                if i is not None:
                    return i
            return None

        i_ref = idx("grove", "ref")
        if i_ref is None:
            i_ref = self._detect_ref_idx(rows)
        i_track = first_idx(("tracking", "number"), ("tracking",), ("track",))
        i_carrier = idx("carrier")
        i_cost = first_idx(("cost",), ("amount",), ("charge",), ("price",))
        i_service = first_idx(("service",), ("mail", "class"))
        i_email = idx("email")
        i_recipient = first_idx(("recipient",), ("name",))
        # Shipments-export-only columns (GOL-3091 legacy reconcile): the delivery
        # status ("Delivered") and the label/ship date. Absent on the normal
        # tracking export — purely additive, so import_tracking (which reads by
        # named key) is unaffected.
        i_order = self._detect_order_idx(rows)
        i_status = idx("status")
        i_date = first_idx(
            ("delivered", "date"),
            ("ship", "date"),
            ("label", "date"),
            ("created", "date"),
            ("date",),
        )
        missing = [
            label for label, i in (("Tracking Number", i_track), ("Carrier", i_carrier), ("Cost", i_cost)) if i is None
        ]
        if missing:
            raise LabelBatchError(f"Tracking file is missing required column(s): {', '.join(missing)}.")
        if i_ref is None and i_email is None and i_order is None:
            raise LabelBatchError(
                "Tracking file has neither a Grove Ref nor an Email column (nor an order number); "
                "cannot match rows to orders."
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
                    "order": cell(i_order),
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
        result = self._reconcile_rows(parsed, strict=True)
        if result["moved_to_purchased"]:
            self._announce_labels_recorded(result["labels"])
        return result

    def _reconcile_rows(self, parsed, strict=True):
        """Match parsed export rows to this batch's lines and write the complete
        orders. ``strict`` (the runner endpoint, ``import_tracking``) raises on
        the first problem and writes nothing. Non-strict (the operator's tolerant
        import) writes every order whose rows are all clean and returns the rest
        in ``errors``; an order touched by any bad row is held back whole."""
        self.ensure_one()
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
        bad_orders = set()  # orders a bad row touched: held back whole in non-strict mode
        seen_refs = set()
        claimed = set()  # line ids already matched by an earlier import row
        clean = []  # (row, line, cost, carrier_key, service_token, via_email)
        for row in parsed:
            ref = row["ref"]
            via_email = False
            if ref:
                if ref in seen_refs:
                    errors.append(f"{ref}: appears more than once in the import")
                    if ref in lines_by_ref:
                        bad_orders.add(lines_by_ref[ref].order_id.id)
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
                bad_orders.add(line.order_id.id)
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
            if order.id in bad_orders:
                continue
            if order.grove_tracking_numbers:
                # Idempotent re-import: this order was already reconciled — skip
                # every one of its rows, never re-write (spec: refuses to re-write
                # tracked orders).
                skipped_already += len(entries)
                continue
            if order.grove_hold_until:
                errors.append(self._on_hold_reason(order))
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

        if errors and strict:
            raise LabelBatchError(
                "Tracking import rejected; nothing written. " + "; ".join(errors),
                refs=[e.split(":")[0] for e in errors],
            )

        # ── All rows valid: write. ──────────────────────────────────────────
        orders_advanced = 0
        newly_total = 0.0
        was_purchased = self.state == "purchased"
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
            "orders": [o.name for o, _e in orders_to_write],
            "errors": errors,
            "orders_advanced": orders_advanced,
            "skipped_already_tracked": skipped_already,
            "total": round(newly_total, 2),
            "rows": len(clean),
            "manual_review": sorted(manual_review),
            "warnings": warnings,
            # For the caller to announce once it has finished the whole import.
            "moved_to_purchased": bool(orders_advanced and not was_purchased),
            "labels": sum(len(entries) for _order, entries in orders_to_write),
        }

    def _announce_labels_recorded(self, labels):
        """Tell the order channel the zone's labels are in, with the Mark all
        shipped button. Only zone batches post. Called after an import has
        finished all its rows, and never raises: a failed post must not touch the
        import (the labels are already recorded)."""
        self.ensure_one()
        if not self.zone or labels <= 0:
            return
        noun = "label" if labels == 1 else "labels"
        text = f"Zone {self.zone}: {labels} {noun} recorded"
        buttons = [{"label": "Mark all shipped", "style": 3, "customId": f"ord:markall:{self.id}"}]
        try:
            self.env["sale.order"]._grove_post_notice(self.company_id, f"Zone {self.zone} labels", [text], buttons)
        except Exception:  # noqa: BLE001 — an announcement is best-effort
            _logger.warning("Labels-recorded post failed for batch %s", self.id, exc_info=True)

    def _zone_ship_start(self, today):
        """First ship day of this batch's zone, in the spring window the batch was
        built in. Judged from the build date, not today, so a batch imported after
        the window closed is not pushed to next year's start."""
        self.ensure_one()
        from ..controllers.main import _parse_calendar_override

        built = self.create_date.replace(tzinfo=timezone.utc).astimezone(_SHOP_TZ).date() if self.create_date else today
        calendar = merge_calendar_override(_parse_calendar_override(self.env))
        lead_open, _ship_end = spring_build_window(int(self.zone), built, calendar)
        return lead_open + timedelta(days=SPRING_BUILD_LEAD_DAYS)

    def _grove_mark_batch_shipped(self, actor, now=None):
        """Mark every labelled order in this zone batch shipped, from the zone's
        ship start (New York date). Returns ``{"shipped", "already", "skipped"}``
        where ``skipped`` is a list of ``{"order", "reason"}``.

        Each shipment emails the customer and cannot be taken back, so every order
        runs in its own savepoint and is committed as soon as it succeeds: one
        order failing never undoes or stops the others, and the failure goes in
        ``skipped`` with a generic reason (the detail is logged). Orders already
        shipped count as ``already``; a repeat run therefore charges and emails
        nothing again. Raises ``UserError`` (a refusal, nothing changed) for a
        batch with no zone, one not yet purchased, or a day before the zone's
        ship start. ``now`` (UTC) is for tests."""
        self.ensure_one()
        from ..controllers.main import _operator_mark_shipped

        if not self.zone:
            raise UserError(f"{self.name} has no zone, so it can't be marked shipped from here.")
        if self.state != "purchased":
            raise UserError(f"{self.name} has no recorded labels yet. Import its labels first.")
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        today = now.astimezone(_SHOP_TZ).date()
        ship_start = self._zone_ship_start(today)
        if today < ship_start:
            raise UserError(
                f"Zone {self.zone} spring shipping starts {ship_start:%B} {ship_start.day}, {ship_start.year}. "
                f"Today is {today:%B} {today.day}, {today.year}, so nothing was marked shipped."
            )

        in_test = bool(config["test_enable"])
        shipped = already = 0
        skipped = []
        for order in sorted(self.line_ids.order_id, key=lambda o: o.name):
            stage = order.grove_fulfillment_stage
            if stage in _SHIPPED_STAGES:
                already += 1
                continue
            if order.grove_hold_until:
                until = order.grove_hold_until
                reason = f"on hold until {until:%b} {until.day}"
            elif not (order.grove_tracking_numbers or "").strip():
                reason = "no label recorded"
            elif stage != "label_purchased":
                reason = f"status is {order._grove_stage_label()}, not label purchased"
            else:
                reason = None
            if reason:
                skipped.append({"order": order.name, "reason": reason})
                continue
            try:
                with self.env.cr.savepoint():
                    result = _operator_mark_shipped(self.env, order, actor=actor, source="discord")
                if result["newly_shipped"]:
                    shipped += 1
                    # The email is out and cannot be recalled: keep this order's
                    # ship even if a later one fails.
                    if not in_test:
                        self.env.cr.commit()
                elif order.grove_fulfillment_stage in _SHIPPED_STAGES:
                    already += 1  # shipped by someone else between the check and the lock
                else:
                    skipped.append({"order": order.name, "reason": "could not be marked shipped yet"})
            except Exception:  # noqa: BLE001 — one failure must not stop the others
                _logger.exception("Mark all shipped failed for order %s in %s", order.id, self.name)
                skipped.append({"order": order.name, "reason": "could not be marked shipped; check it in Odoo"})
        return {"shipped": shipped, "already": already, "skipped": skipped}

    @api.model
    def _on_hold_reason(self, order):
        """Why an import did not record a label on a held order: the customer
        asked to wait, so neither the label nor the balance goes through."""
        return f"{order.name}: on hold until {order.grove_hold_until.isoformat()}; release the hold and re-import"

    @api.model
    def _is_valid_tracking(self, value):
        """Thin wrapper over shippo_client.is_valid_tracking (kept as the pure
        helper per spec §B1) so tests can stub it and the model has one seam."""
        from .shippo_client import is_valid_tracking

        return is_valid_tracking(value)

    # ── Tolerant operator import (any Pirate Ship export) ────────────────────
    @api.model
    def _order_with_tracking(self, company, tracking):
        """The order in ``company`` that already carries ``tracking`` as one of
        its boxes, or an empty recordset."""
        Order = self.env["sale.order"].sudo().with_company(company)
        if not tracking:
            return Order.browse()
        candidates = Order.search(
            [("company_id", "=", company.id), ("grove_tracking_numbers", "ilike", tracking)],
        )
        return candidates.filtered(lambda o: tracking in (o.grove_tracking_numbers or "").split())[:1]

    @api.model
    def _resolve_loose_order(self, row, company):
        """Place an export row that is not part of the open batch on an order.

        Order number first (from a Grove Ref ``S00245/1`` or a bare ``S00245``
        column), then recipient email when it points at exactly ONE ship order
        still on the label/ship path. Returns ``(order, via_email, problem)``."""
        Order = self.env["sale.order"].sudo().with_company(company)
        name = None
        if row.get("ref") and GROVE_REF_RE.match(row["ref"]):
            name = row["ref"].split("/")[0]
        elif row.get("order") and ORDER_NAME_RE.match(row["order"]):
            name = row["order"]
        if name:
            order = Order.search([("company_id", "=", company.id), ("name", "=", name)], limit=1)
            return (order, False, None) if order else (None, False, f"no order {name} in Odoo")
        email = (row.get("email") or "").strip().lower()
        if not email:
            return None, False, "no order number or email to match on"
        candidates = Order.search(
            [
                ("company_id", "=", company.id),
                ("grove_fulfillment", "=", "ship"),
                ("grove_fulfillment_stage", "in", ("awaiting_label", "wave_assigned", "label_purchased", "shipped")),
                "|",
                ("partner_id.email", "=ilike", email),
                ("partner_shipping_id.email", "=ilike", email),
            ]
        )
        if not candidates:
            return None, False, f"no open shipping order for {email}"
        if len(candidates) > 1:
            names = ", ".join(sorted(candidates.mapped("name")))
            return None, False, f"{email} matches {names}; record this label on the right order by hand"
        return candidates, True, None

    @api.model
    def _flag_refunded_row(self, row, company, summary):
        """A refunded/voided label in the export. If an order still carries it,
        stop the carrier poll (that label will never move) and say so once on
        the order. The tracking stays visible; the operator buys a new label
        (which the next import records as a replacement) or cancels."""
        tracking = row["tracking"]
        order = self._order_with_tracking(company, tracking)
        if not order:
            return
        marker = f"refunded in Pirate Ship: {tracking}"
        already = (
            self.env["mail.message"]
            .sudo()
            .search_count([("model", "=", "sale.order"), ("res_id", "=", order.id), ("body", "ilike", marker)])
        )
        if not already:
            order.grove_carrier_poll_stopped = True
            order.message_post(
                body=(
                    f"⚠️ Label {marker}. Tracking stopped for it. Buy a new label (the next "
                    f"Pirate Ship import records it as the replacement) or cancel the order."
                )
            )
        summary["refunded"].append(f"{order.name}: label {tracking} was refunded; needs a new label or a cancel")

    @api.model
    def _import_loose_rows(self, rows, company, summary):
        """Record rows that are not part of the open batch: a first label on an
        order awaiting one, or a replacement on an order that already has one.
        Anything that cannot be placed safely goes to ``summary["review"]``."""
        by_order = {}
        for row in rows:
            who = row.get("recipient") or row.get("email") or row.get("order") or row.get("ref") or row["tracking"]
            if not self._is_valid_tracking(row["tracking"]):
                summary["review"].append(f"{who}: invalid tracking number {row['tracking']!r}")
                continue
            carrier_key, service_token = _carrier_token(row["carrier_raw"])
            if not carrier_key:
                summary["review"].append(f"{who}: unrecognised carrier {row['carrier_raw']!r}")
                continue
            try:
                cost = round(float((row["cost_raw"] or "0").replace("$", "").replace(",", "")), 2)
            except (TypeError, ValueError):
                summary["review"].append(f"{who}: non-numeric cost {row['cost_raw']!r}")
                continue
            order, via_email, problem = self._resolve_loose_order(row, company)
            if not order:
                summary["review"].append(f"{who} ({row['tracking']}): {problem}")
                continue
            if order.grove_hold_until:
                summary["review"].append(self._on_hold_reason(order))
                continue
            by_order.setdefault(order, []).append((row, carrier_key, service_token, cost, via_email))
        for order, entries in by_order.items():
            if len(entries) > 1:
                tracks = ", ".join(e[0]["tracking"] for e in entries)
                summary["review"].append(
                    f"{order.name}: {len(entries)} labels in this file ({tracks}); record the right one by hand"
                )
                continue
            row, carrier_key, service_token, cost, via_email = entries[0]
            try:
                with self.env.cr.savepoint():
                    if order.grove_tracking_numbers:
                        old = order.grove_tracking_numbers.replace("\n", ", ")
                        if order.action_grove_replace_label(
                            row["tracking"],
                            carrier=carrier_key,
                            actual_cost=cost,
                            service=service_token,
                            source="Pirate Ship import",
                        ):
                            summary["replaced"].append(f"{order.name}: {old} → {row['tracking']}")
                    else:
                        order.action_grove_record_hand_label(
                            row["tracking"], carrier=carrier_key, actual_cost=cost, service=service_token
                        )
                        summary["labelled"].append(order.name)
                    if via_email:
                        summary["manual_review"].append(order.name)
                        order.message_post(
                            body=(
                                f"⚠️ Label {row['tracking']} matched to this order by EMAIL from a Pirate Ship "
                                f"import (no order number in the row). Check it belongs to this order."
                            )
                        )
            except UserError as exc:
                summary["review"].append(str(exc))

    @api.model
    def import_pirateship_file(self, raw_bytes, filename=None, company=None):
        """The operator's import: take ANY Pirate Ship export (.xlsx/.xls/.csv)
        and record everything that can be placed safely, instead of rejecting
        the whole file over one stray row.

        * rows for any open batch go through that batch's reconcile (non-strict:
          a bad row holds back only its own order), and an email-only row that
          matches two open batches is left for a human;
        * a row for an order awaiting a label records it (the single-order path,
          no batch needed);
        * a new tracking number on an order that already has one replaces it;
        * a tracking number already on an order is skipped (re-import is safe);
        * a refunded/voided row flags the order still carrying that label;
        * everything else is listed in ``review`` and nothing is written for it.

        Returns ``{labelled, replaced, refunded, already, review, warnings,
        manual_review}``. Raises ``LabelBatchError`` only when the file itself
        is unreadable or lacks the tracking/carrier/cost columns, or
        ``AccessError`` for a user limited to their own orders: the lookups run
        as sudo so the import can place any row in the company."""
        if not self.env.is_superuser() and not self.env.user.has_group("sales_team.group_sale_salesman_all_leads"):
            raise AccessError("Importing a Pirate Ship export needs access to all sales orders.")
        company = company or self.env.company
        parsed = self.browse()._parse_tracking_file(raw_bytes, filename=filename)
        summary = {
            "labelled": [],
            "replaced": [],
            "refunded": [],
            "already": 0,
            "review": [],
            "warnings": [],
            "manual_review": [],
        }
        batch_by_ref = {}
        batches_by_email = {}
        for batch in self._open_batches(company):
            for line in batch.line_ids:
                batch_by_ref[line.grove_ref] = batch
                email = (line.email or "").strip().lower()
                if email:
                    batches_by_email[email] = batches_by_email.get(email, self.browse()) | batch
        rows_by_batch, loose_rows, refund_rows = {}, [], []
        announce = []  # (batch, label count) for batches this import moved to purchased
        for row in parsed:
            if REFUNDED_STATUS_RE.search(row.get("status") or ""):
                refund_rows.append(row)
                continue
            if row["tracking"] and self._order_with_tracking(company, row["tracking"]):
                summary["already"] += 1
                continue
            email = (row.get("email") or "").strip().lower()
            if row["ref"]:
                target = batch_by_ref.get(row["ref"])
            else:
                target = batches_by_email.get(email) if email else None
                if target and len(target) > 1:
                    who = row.get("recipient") or email
                    summary["review"].append(
                        f"{who} <{email}>: matches rows in {', '.join(sorted(target.mapped('name')))}; "
                        f"re-upload with the Grove Ref column or record it by hand"
                    )
                    continue
            if target:
                rows_by_batch.setdefault(target, []).append(row)
            else:
                loose_rows.append(row)
        for batch, batch_rows in rows_by_batch.items():
            result = batch._reconcile_rows(batch_rows, strict=False)
            summary["labelled"].extend(result["orders"])
            summary["review"].extend(result["errors"])
            summary["warnings"].extend(result["warnings"])
            summary["manual_review"].extend(result["manual_review"])
            summary["already"] += result["skipped_already_tracked"]
            if result["moved_to_purchased"]:
                announce.append((batch, result["labels"]))
            if filename:
                batch.write({"tracking_import": base64.b64encode(raw_bytes), "tracking_import_filename": filename})
        self._import_loose_rows(loose_rows, company, summary)
        # Refunds last, so a refund row and its replacement in the same file end
        # on the replacement instead of flagging a label that was just swapped.
        for row in refund_rows:
            self._flag_refunded_row(row, company, summary)
        # Last of all, so a failed import never announces labels.
        for batch, labels in announce:
            batch._announce_labels_recorded(labels)
        return summary

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
        help="Tick to confirm this order has NOT already been shipped on a label bought earlier. "
        "Required before a flagged row is written to the Pirate Ship CSV (GOL-3083).",
    )
    prior_batch_id = fields.Many2one(
        "grove.label.batch",
        string="Earlier batch",
        readonly=True,
        copy=False,
        help="This order was in that purchased batch and no label was recorded for it. A label may "
        "have been bought anyway, so the row stays out of the CSV until confirmed.",
    )
    # Written back on reconcile.
    tracking_number = fields.Char(copy=False)
    carrier_token = fields.Char(copy=False, help="Canonical carrier key (UPS/USPS), as stored on the order.")
    service_token = fields.Char(copy=False, help="Ground service token (ups_ground/usps_ground_advantage).")
    actual_cost = fields.Float(digits=(8, 2), copy=False)

    @api.depends("order_date", "order_id.grove_fulfillment_stage", "prior_batch_id")
    def _compute_order_age(self):
        """Age-since-order and the already-labelled flag (GOL-3083).

        Only a regular paid ship order (``awaiting_label``) is age-flagged: a
        ``wave_assigned`` preorder is expected to be old (ordered seasons before
        its wave opens), so age-since-order is not a shipped-by-hand signal for it
        — the import-time email cross-check and the reconcile wizard cover that
        case instead. Any row carried over from a purchased batch with no label
        recorded is flagged whatever its age."""
        threshold = self.env["grove.label.batch"]._age_ack_days()
        now = fields.Datetime.now()
        for line in self:
            line.order_age_days = (now - line.order_date).days if line.order_date else 0
            line.age_flagged = bool(line.prior_batch_id) or (
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
