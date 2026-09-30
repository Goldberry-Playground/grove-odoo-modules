"""Department tree metadata on ``product.public.category`` (GOL-2744).

The nursery is growing beyond orchard stock into whole product *families*
(mycoforestry, forest farming, seed & scion) that are browsed, filtered and —
eventually — grown differently. Rather than pile more category pills into one
bar, the storefront groups the public-category tree into **departments** (one
quiet tab row), a cross-cutting **Guilds** collection, and per-department facet
allowlists. See ``grove-sites``
``docs/superpowers/specs/2026-09-30-nursery-shop-departments-design.md``.

Odoo is the source of truth for the tree, its status and each department's
facets, so launching a family is a data change (``coming_soon`` -> ``live``),
never a code deploy. This model adds the fields that carry that intent; the
tree restructure itself runs as a migration (IDs kept) and the API reads these
fields via ``/grove/api/v1/catalog/nav``.
"""

from odoo import api, fields, models

from ..controllers.product_domain import slugify

# Facet keys a department may surface in the storefront sidebar. The API and
# the migration both validate ``grove_facets`` against this allowlist so a typo
# in Odoo can never push an unknown facet onto the storefront. Kept in sync with
# the spec's § "New fields on product.public.category" table.
GROVE_FACET_ALLOWLIST = (
    "zone",
    "layer",
    "sun",
    "uses",
    "on_offer",
    "host_tree",
    "fungus",
    "shade_level",
    "years_to_harvest",
    "form",
    "species",
    "ships",
)


class ProductPublicCategory(models.Model):
    _inherit = "product.public.category"

    grove_slug = fields.Char(
        string="Grove Slug",
        index=True,
        copy=False,
        help=(
            "Stable URL slug for storefront filters (?cat=, ?dept=). The API "
            "prefers this over slugify(name), so renaming a category no longer "
            "silently changes its URL. Backfilled to today's slugify(name) by "
            "the GOL-2744 migration, then authored per department."
        ),
    )
    grove_node_kind = fields.Selection(
        selection=[
            ("department", "Department"),
            ("category", "Category"),
            ("collection", "Collection"),
        ],
        string="Grove Node Kind",
        help=(
            "Distinguishes a department root, one of its categories, and the "
            "cross-cutting Guilds collection. Drives how /catalog/nav groups "
            "the tree."
        ),
    )
    grove_dept_status = fields.Selection(
        selection=[
            ("live", "Live"),
            ("coming_soon", "Coming soon"),
            ("hidden", "Hidden"),
        ],
        string="Department Status",
        help=(
            "Departments only. `live` renders a product grid, `coming_soon` a "
            "teaser + notify-me, `hidden` never renders. A live department is "
            "shown only when it has >=1 published product."
        ),
    )
    grove_teaser = fields.Text(
        string="Grove Teaser",
        translate=True,
        help="Department intro / coming-soon blurb (authored copy, not agent-generated).",
    )
    grove_facets = fields.Char(
        string="Grove Facets",
        help=(
            "Comma-separated facet keys this department shows, from the "
            "allowlist: " + ", ".join(GROVE_FACET_ALLOWLIST) + "."
        ),
    )
    grove_coming_list = fields.Text(
        string="Grove Coming List",
        translate=True,
        help=("'What's coming' list on teaser pages, one item per line as `Name | detail`, until real products exist."),
    )

    # Odoo 19 dropped the `_sql_constraints` list attribute (it warns and never
    # creates the constraint). Declared with the modern `models.Constraint` so
    # Odoo creates the UNIQUE(grove_slug) on `-u grove_headless`. NULLs are
    # distinct in Postgres, so legacy rows stay valid until the migration
    # backfills them.
    _grove_slug_uniq = models.Constraint(
        "unique(grove_slug)",
        "The Grove slug must be unique across public categories.",
    )

    def grove_effective_slug(self):
        """URL slug the API emits/matches for this category.

        Prefers the authored ``grove_slug``; falls back to ``slugify(name)`` so
        categories the backfill hasn't reached (or created after it) still
        resolve. Single-record helper — callers iterate.
        """
        self.ensure_one()
        return self.grove_slug or slugify(self.name or "")

    def grove_facet_list(self):
        """Ordered, allowlist-filtered facet keys for this department."""
        self.ensure_one()
        raw = (self.grove_facets or "").split(",")
        seen = set()
        out = []
        for token in raw:
            key = token.strip().lower()
            if key in GROVE_FACET_ALLOWLIST and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def grove_coming_items(self):
        """Parse ``grove_coming_list`` into ``[{name, detail}]`` rows.

        One item per non-blank line; ``Name | detail`` splits on the first
        pipe, and a line with no pipe is all name.
        """
        self.ensure_one()
        items = []
        for line in (self.grove_coming_list or "").splitlines():
            text = line.strip()
            if not text:
                continue
            name, sep, detail = text.partition("|")
            items.append({"name": name.strip(), "detail": detail.strip() if sep else ""})
        return items

    @api.model_create_multi
    def create(self, vals_list):
        # A category created without an explicit slug (Odoo UI, imports) gets a
        # deterministic one derived from its name, matching the backfill, so the
        # unique index and the API's slug preference hold for new rows too.
        records = super().create(vals_list)
        for record in records:
            if not record.grove_slug:
                record.grove_slug = record._grove_unique_slug(slugify(record.name or ""))
        return records

    def _grove_unique_slug(self, base):
        """Return ``base`` (or ``base-<id>``) not already taken by another row."""
        self.ensure_one()
        if not base:
            return False
        clash = self.sudo().search([("grove_slug", "=", base), ("id", "!=", self.id)], limit=1)
        return f"{base}-{self.id}" if clash else base
