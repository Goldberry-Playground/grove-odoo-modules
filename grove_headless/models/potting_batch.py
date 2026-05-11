"""Potting-up batch — one row per "we moved N plants from 1gal to 3gal" event.

Why a dedicated model instead of just letting staff create raw stock moves:
nursery potting days are batch operations (50–200 plants at once) and the
team needs ONE form to fill out, not three (consumption + production +
scrap). This model wraps the standard Odoo MRP + scrap primitives so the
audit trail stays canonical while the data-entry surface is a single record.

The variant pairing (source = 1gal Honeycrisp, target = 3gal Honeycrisp)
is captured per batch; we lazily create a tiny normal-type mrp.bom the
first time a given (source → target) pair is potted, then reuse it
forever. That keeps the catalog clean: no upfront BOM setup, but full
manufacturing-style accounting once the first batch runs.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class GrovePottingBatch(models.Model):
    _name = "grove.potting.batch"
    _description = "Nursery Potting-up Batch"
    _order = "date desc, id desc"
    _inherit = ["mail.thread"]

    name = fields.Char(
        string="Reference",
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _("New"),
    )
    date = fields.Datetime(
        string="Potting Date",
        required=True,
        default=fields.Datetime.now,
        tracking=True,
    )
    source_product_id = fields.Many2one(
        "product.product",
        string="Source Variant (e.g. 1gal)",
        required=True,
        domain=[("type", "in", ("consu", "product"))],
        tracking=True,
    )
    target_product_id = fields.Many2one(
        "product.product",
        string="Target Variant (e.g. 3gal)",
        required=True,
        domain=[("type", "in", ("consu", "product"))],
        tracking=True,
    )
    quantity = fields.Float(
        string="Plants Potted",
        required=True,
        default=1.0,
        tracking=True,
        help="Total plants moved out of the source variant for this batch.",
    )
    mortality = fields.Float(
        string="Mortality",
        default=0.0,
        tracking=True,
        help="Plants that died during potting. Will be scrapped from source inventory.",
    )
    successful_qty = fields.Float(
        string="Successfully Potted",
        compute="_compute_successful_qty",
        store=True,
        help="quantity − mortality. This is what lands as target inventory.",
    )
    notes = fields.Text(string="Notes")
    state = fields.Selection(
        [("draft", "Draft"), ("done", "Done"), ("cancel", "Cancelled")],
        default="draft",
        required=True,
        tracking=True,
    )
    production_id = fields.Many2one(
        "mrp.production",
        string="Manufacturing Order",
        readonly=True,
        copy=False,
        help="MO that materializes the source → target variant transformation.",
    )
    scrap_id = fields.Many2one(
        "stock.scrap",
        string="Mortality Scrap",
        readonly=True,
        copy=False,
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
    )

    @api.depends("quantity", "mortality")
    def _compute_successful_qty(self):
        for batch in self:
            batch.successful_qty = max(batch.quantity - batch.mortality, 0.0)

    @api.constrains("source_product_id", "target_product_id")
    def _check_variants_differ(self):
        for batch in self:
            if batch.source_product_id == batch.target_product_id:
                raise ValidationError(
                    _("Source and target variants must differ — potting up implies a pot-size change.")
                )

    @api.constrains("quantity", "mortality")
    def _check_quantities(self):
        for batch in self:
            if batch.quantity <= 0:
                raise ValidationError(_("Plants Potted must be greater than zero."))
            if batch.mortality < 0:
                raise ValidationError(_("Mortality cannot be negative."))
            if batch.mortality > batch.quantity:
                raise ValidationError(_("Mortality cannot exceed Plants Potted."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", _("New")) == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code("grove.potting.batch") or _("New")
        return super().create(vals_list)

    def _get_or_create_bom(self):
        """Return a normal-type BOM that consumes 1 source to produce 1 target.

        Reuses the first matching BOM so repeat potting days for the same
        variant pair don't litter the catalog with identical BOMs.

        Domain pre-filters by `bom_line_ids.product_id` so reshuffled BOMs
        from a prior batch don't mask our valid newer ones, and we sort
        `id desc` so when multiple valid candidates exist the freshest
        wins (defense in depth against future drift).
        """
        self.ensure_one()
        Bom = self.env["mrp.bom"]
        candidates = Bom.search(
            [
                ("product_id", "=", self.target_product_id.id),
                ("type", "=", "normal"),
                ("company_id", "in", (self.company_id.id, False)),
                ("bom_line_ids.product_id", "=", self.source_product_id.id),
            ],
            order="id desc",
        )
        # Confirm shape: exactly one line, mapping source → target. Drops
        # multi-component BOMs that happened to include the source variant.
        for bom in candidates:
            if len(bom.bom_line_ids) == 1 and bom.bom_line_ids.product_id == self.source_product_id:
                return bom
        return Bom.create(
            {
                "product_tmpl_id": self.target_product_id.product_tmpl_id.id,
                "product_id": self.target_product_id.id,
                "type": "normal",
                "product_qty": 1.0,
                "company_id": self.company_id.id,
                "bom_line_ids": [
                    (0, 0, {"product_id": self.source_product_id.id, "product_qty": 1.0}),
                ],
            }
        )

    def action_confirm(self):
        """Materialize the batch: optional scrap, then produce the target."""
        for batch in self:
            if batch.state != "draft":
                raise UserError(_("Only draft batches can be confirmed."))

            if batch.mortality > 0:
                batch.scrap_id = self.env["stock.scrap"].create(
                    {
                        "product_id": batch.source_product_id.id,
                        "product_uom_id": batch.source_product_id.uom_id.id,
                        "scrap_qty": batch.mortality,
                        "origin": batch.name,
                        "company_id": batch.company_id.id,
                    }
                )
                batch.scrap_id.action_validate()

            if batch.successful_qty > 0:
                bom = batch._get_or_create_bom()
                production = self.env["mrp.production"].create(
                    {
                        "product_id": batch.target_product_id.id,
                        "product_qty": batch.successful_qty,
                        "product_uom_id": batch.target_product_id.uom_id.id,
                        "bom_id": bom.id,
                        "origin": batch.name,
                        "company_id": batch.company_id.id,
                    }
                )
                production.action_confirm()
                # Auto-complete the MO: we're recording an event that
                # already happened, not scheduling future work.
                production.qty_producing = batch.successful_qty
                production._set_qty_producing()
                production.button_mark_done()
                batch.production_id = production

            batch.state = "done"

    def action_cancel(self):
        for batch in self:
            if batch.state == "done":
                raise UserError(
                    _("Cannot cancel a done batch — inventory already moved. Create a reverse batch instead.")
                )
            batch.state = "cancel"
