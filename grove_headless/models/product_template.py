import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

try:
    from .image_resolution import GROVE_MIN_IMAGE_LONG_EDGE, is_low_res, read_image_dimensions
except ImportError:  # loaded standalone (tests import by file path)
    import importlib.util as _ilu
    import os as _os

    _ir_path = _os.path.join(_os.path.dirname(__file__), "image_resolution.py")
    _spec = _ilu.spec_from_file_location("grove_image_resolution", _ir_path)
    _ir = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_ir)
    GROVE_MIN_IMAGE_LONG_EDGE = _ir.GROVE_MIN_IMAGE_LONG_EDGE
    is_low_res = _ir.is_low_res
    read_image_dimensions = _ir.read_image_dimensions


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

    # ── Guide publishing gate (GATH-130 / GATH-121) ─────────────────────
    # The species "guide" body lives in the standard eCommerce field
    # website_description ("Description for the website"), drafted by the
    # Paperclip guide-drafting routine. Agent-authored HTML is a weaker trust
    # story than the human prose Wes wrote in Ghost, so it stays invisible to
    # the storefront until Wes reviews the draft and ticks this box. The detail
    # serializer withholds the body while this is False
    # (controllers/main.py:_gate_guide_fields), so an un-approved draft never
    # crosses the API boundary — defense in depth behind the frontend GuideBlock
    # sanitizer. CREATE-ONLY: the routine refuses to overwrite a non-empty
    # description, so re-drafting means a human clears the field first.
    grove_guide_ready = fields.Boolean(
        string="Guide Approved for Storefront",
        default=False,
        help="Tick once the website description (species guide) has been "
        "reviewed and is ready to show on the storefront. Until then the "
        "storefront shows a 'coming soon' guide placeholder instead of the body.",
    )

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

    # ── Product-photo resolution guardrail (GOL-837) ────────────────────
    # The storefront can't add resolution a source lacks, so we surface the
    # stored photo's pixel size here and flag anything below the storefront
    # minimum at upload time. Stored + computed off image_1920 so the flag is
    # queryable/searchable in the admin without re-reading the image bytes.
    grove_image_width = fields.Integer(
        string="Photo Width (px)",
        compute="_compute_grove_image_resolution",
        store=True,
        readonly=True,
        help="Pixel width of the stored product photo (image_1920). 0 if none set.",
    )
    grove_image_height = fields.Integer(
        string="Photo Height (px)",
        compute="_compute_grove_image_resolution",
        store=True,
        readonly=True,
        help="Pixel height of the stored product photo (image_1920). 0 if none set.",
    )
    grove_image_low_res = fields.Boolean(
        string="Low-resolution Photo",
        compute="_compute_grove_image_resolution",
        store=True,
        readonly=True,
        help=(
            "True when a photo is set but its long edge is below the "
            f"{GROVE_MIN_IMAGE_LONG_EDGE}px storefront minimum — it will render "
            "blurry on the product page. Re-shoot / re-upload a larger source."
        ),
    )

    @api.depends("image_1920")
    def _compute_grove_image_resolution(self):
        for record in self:
            width, height = read_image_dimensions(record.image_1920)
            record.grove_image_width = width
            record.grove_image_height = height
            record.grove_image_low_res = is_low_res(width, height, GROVE_MIN_IMAGE_LONG_EDGE)

    @api.onchange("image_1920")
    def _onchange_grove_image_low_res_warning(self):
        # Non-blocking upload-time guardrail: warn (don't reject) so content
        # owners can still stage a placeholder, but can't silently regress
        # storefront photo quality. Fires on the raw upload before Odoo's 1920
        # store-cap, so it sees the true source resolution.
        if not self.image_1920:
            return
        width, height = read_image_dimensions(self.image_1920)
        if is_low_res(width, height, GROVE_MIN_IMAGE_LONG_EDGE):
            long_edge = max(width, height)
            return {
                "warning": {
                    "title": _("Low-resolution product photo"),
                    "message": _(
                        "This photo is %(w)s×%(h)spx (long edge %(edge)spx), below the "
                        "%(minimum)spx storefront minimum. It will look blurry on the product "
                        "page hero (~1056px) and grid cards. Upload a higher-resolution source "
                        "before publishing.",
                        w=width,
                        h=height,
                        edge=long_edge,
                        minimum=GROVE_MIN_IMAGE_LONG_EDGE,
                    ),
                }
            }

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
