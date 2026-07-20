import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ProductTemplate(models.Model):
    _inherit = "product.template"

    grove_featured = fields.Boolean(
        string="Grove Featured",
        default=False,
        help="Mark this product as featured in the headless storefront.",
    )
    grove_seo_description = fields.Text(
        string="Grove SEO Description",
        translate=True,
        help="SEO-optimized description used by the headless frontend meta tags.",
    )
    grove_slug = fields.Char(
        string="Grove Slug",
        compute="_compute_grove_slug",
        store=True,
        index=True,
        help=(
            "URL-safe slug derived from name. Stored + indexed so /grove/api/v1/products?slug=X "
            "is an indexed lookup. Recomputes on name change."
        ),
    )

    @api.depends("name", "company_id")
    def _compute_grove_slug(self):
        # The slug is auto-derived. If a name collides with another product in the
        # same company, append the id to break the tie deterministically. Hub URLs
        # stay stable because the id is also stable.
        for record in self:
            base = self._slugify(record.name or "")
            if not base:
                record.grove_slug = False
                continue
            domain = [
                ("grove_slug", "=", base),
                ("id", "!=", record.id),
                ("company_id", "in", [record.company_id.id, False]),
            ]
            collision = record.search(domain, limit=1)
            record.grove_slug = f"{base}-{record.id}" if collision else base

    # Shipping tier drives the per-tree zone rate at checkout
    # (models/shipping_zones.py). Default "potted" = the higher tier, so an
    # untagged product can never be undercharged.
    grove_shipping_tier = fields.Selection(
        [("bareroot", "Bareroot"), ("potted", "Potted")],
        string="Grove Shipping Tier",
        default="potted",
        help="Bareroot ships as a 4 lb slim box; potted as a ~25 lb box. "
        "Determines the per-tree shipping rate by destination zone.",
    )

    # ── Growing facts (2026-07-13 catalog spec) ─────────────────────────
    # Filterable facts live here (typed); display-only facts stay Char.
    # Narrative content deliberately does NOT live in Odoo (Ghost, keyed
    # by grove_slug — see the nursery product-pages spec).
    grove_botanical_name = fields.Char(string="Botanical Name")
    grove_zone_min = fields.Integer(string="USDA Zone Min")
    grove_zone_max = fields.Integer(string="USDA Zone Max")
    grove_layer = fields.Selection(
        [
            ("canopy", "Canopy"),
            ("understory", "Understory"),
            ("shrub", "Shrub"),
            ("ground", "Ground cover"),
            ("vine", "Vine"),
        ],
        string="Food Forest Layer",
    )
    grove_sun = fields.Selection(
        [("full", "Full sun"), ("partial", "Partial sun"), ("shade", "Shade")],
        string="Sun Requirement",
    )
    grove_mature_size = fields.Char(string="Mature Size")
    grove_spacing = fields.Char(string="Plant Spacing")
    grove_soil = fields.Char(string="Soil")

    @api.constrains("grove_zone_min", "grove_zone_max")
    def _check_zone_range(self):
        for record in self:
            if record.grove_zone_min and record.grove_zone_max and record.grove_zone_min > record.grove_zone_max:
                raise ValidationError("USDA zone min cannot exceed zone max.")

    @staticmethod
    def _slugify(value: str) -> str:
        # Lowercase → strip non-alphanumeric → collapse runs of non-alphanumeric
        # to a single dash → trim leading/trailing dashes.
        lowered = (value or "").lower()
        collapsed = re.sub(r"[^a-z0-9]+", "-", lowered)
        return collapsed.strip("-")
