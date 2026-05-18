import re

from odoo import api, fields, models


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

    @staticmethod
    def _slugify(value: str) -> str:
        # Lowercase → strip non-alphanumeric → collapse runs of non-alphanumeric
        # to a single dash → trim leading/trailing dashes.
        lowered = (value or "").lower()
        collapsed = re.sub(r"[^a-z0-9]+", "-", lowered)
        return collapsed.strip("-")
