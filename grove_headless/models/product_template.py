import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# ── Preorder cap (GOL-2171) ──────────────────────────────────────────────
# The global default preorder cap lives in ir.config_parameter under this key
# (seeded to 50 by data/grove_config_params.xml, noupdate so an admin edit
# survives upgrades). A per-template override (grove_preorder_cap) wins when
# set; 0/unset means "inherit this global". Kept as a named constant so the
# controller and tests reference the same key.
PREORDER_CAP_PARAM = "grove_headless.preorder_cap_default"
PREORDER_CAP_SEED = 50


def _parse_preorder_variant_ids(raw):
    """Parse a sale.order.grove_preorder_variant_ids Char into a set of ints.

    The field is a comma-separated list of the variant ids charged as a $10
    deposit at checkout ("trees owed"). Mirrors the webhook's own parse
    (controllers/main.py) so the count and the refund logic agree on which
    lines are preorders.
    """
    ids = set()
    for token in (raw or "").split(","):
        token = token.strip()
        if token.isdigit():
            ids.add(int(token))
    return ids


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

    # Availability fields that live on the template itself (GOL-1896). A flip of
    # any of these changes what the /shop grid shows — sale_ok toggles
    # purchasable vs "Coming soon", website_published / active add or remove the
    # card — so snapshot before the write and let grove.publish.event emit a
    # `product.availability` webhook if the state actually crossed. qty_available
    # is deliberately NOT here: on-hand never changes via a template write, it
    # crosses through stock.quant (see stock_quant.py).
    _GROVE_AVAILABILITY_FIELDS = frozenset({"sale_ok", "website_published", "is_published", "active"})

    def write(self, vals):
        if self._GROVE_AVAILABILITY_FIELDS.intersection(vals):
            self.env["grove.publish.event"].sudo().note_availability_candidates(self)
        return super().write(vals)

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

    # ── Preorder cap (GOL-2171) ─────────────────────────────────────────
    # A product also sells out once its preorder count *exceeds* its cap — the
    # 51st deposit against a cap of 50 flips it (strict >, ratified by Josh
    # 2026-09-07). Two knobs, both editable in Odoo (Josh's explicit
    # requirement): a global default in ir.config_parameter and this per-
    # template override. The override WINS when > 0; 0/unset means "inherit the
    # global", never "cap of zero" — so a freshly created product with no value
    # set behaves as the global 50, not as instantly sold out. That inherit
    # case is the obvious production trap, so it is covered explicitly by
    # test_preorder_cap.
    grove_preorder_cap = fields.Integer(
        string="Preorder Cap (override)",
        default=0,
        help="Maximum preorders before this product shows as sold out on the "
        "storefront. Leave 0 to inherit the global default (System Parameter "
        f"'{PREORDER_CAP_PARAM}', seeded to {PREORDER_CAP_SEED}). Set a positive "
        "value to override for this product only (e.g. 100 for elderberry, 20 "
        "for persimmon). The product sells out once its preorder count exceeds "
        "this number.",
    )
    # Live derived state, non-stored so it always reflects current orders. The
    # compute is batched (one query per recordset) so the /shop grid can read
    # it for a whole page without an N+1 — the controller warms the cache with a
    # single mapped() before serialising.
    grove_preorder_count = fields.Integer(
        string="Preorders (deposit paid)",
        compute="_compute_grove_preorder_state",
        help="Trees owed: sum of deposit-paid preorder line quantities for this product across all orders.",
    )
    grove_preorder_cap_effective = fields.Integer(
        string="Effective Preorder Cap",
        compute="_compute_grove_preorder_state",
        help="The cap actually applied: the per-product override if set, else "
        "the global default. 0 means the cap is disabled (never sells out).",
    )
    grove_preorder_cap_reached = fields.Boolean(
        string="Preorder Cap Reached",
        compute="_compute_grove_preorder_state",
        help="True once the preorder count exceeds the effective cap — the "
        "storefront then shows this product as sold out.",
    )

    def _compute_grove_preorder_state(self):
        counts = self._grove_preorder_counts()
        global_cap = self._grove_global_preorder_cap()
        for record in self:
            count = counts.get(record.id, 0)
            override = record.grove_preorder_cap or 0
            cap = override if override > 0 else global_cap
            record.grove_preorder_count = count
            record.grove_preorder_cap_effective = cap
            record.grove_preorder_cap_reached = cap > 0 and count > cap

    @api.model
    def _grove_global_preorder_cap(self):
        """Global default preorder cap from ir.config_parameter, falling back to
        the seed (50) when the parameter is missing or non-numeric. A parameter
        explicitly set to 0 (or negative) is honoured as 'cap disabled' — the
        deliberate off switch for the site-wide cap."""
        # get_param returns the default (here the seed) as an int when the row
        # is absent, and False in older signatures — both must land on the seed,
        # never on int(False)==0, which would silently disable the cap
        # site-wide if the parameter row were ever deleted.
        raw = self.env["ir.config_parameter"].sudo().get_param(PREORDER_CAP_PARAM, PREORDER_CAP_SEED)
        if raw is None or raw is False or str(raw).strip() == "":
            return PREORDER_CAP_SEED
        try:
            return int(raw)
        except (TypeError, ValueError):
            return PREORDER_CAP_SEED

    def _grove_preorder_counts(self):
        """Preorder units per template in ``self`` in a single query.

        Counts sale.order.line where the order is grove_checkout_status =
        'deposit_paid' AND the line's variant is in that order's
        grove_preorder_variant_ids (the deposit variants). Rolled up to the
        template and summed by quantity — the number of trees owed. Cancelled,
        expired, unpaid and refunded_oversell orders are excluded by
        construction (only deposit_paid is matched). Returns {template_id: units}
        with every id in ``self`` present (0 when none).

        v1 limitation (accepted by Josh 2026-09-07): grove_preorder_variant_ids
        is order-scoped, not line-scoped, so a mixed order that both stocks and
        preorders the *same* variant over-counts. Per-line deposit-unit
        persistence is a fast-follow, not v1.
        """
        counts = dict.fromkeys(self.ids, 0)
        variant_to_tmpl = {}
        for tmpl in self:
            for variant in tmpl.product_variant_ids:
                variant_to_tmpl[variant.id] = tmpl.id
        if not variant_to_tmpl:
            return counts
        lines = (
            self.env["sale.order.line"]
            .sudo()
            .search(
                [
                    ("order_id.grove_checkout_status", "=", "deposit_paid"),
                    ("product_id", "in", list(variant_to_tmpl)),
                ]
            )
        )
        for line in lines:
            preorder_ids = _parse_preorder_variant_ids(line.order_id.grove_preorder_variant_ids)
            if line.product_id.id in preorder_ids:
                tmpl_id = variant_to_tmpl.get(line.product_id.id)
                if tmpl_id is not None:
                    counts[tmpl_id] += line.product_uom_qty
        return {tmpl_id: int(units) for tmpl_id, units in counts.items()}

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

    grove_publish_event_ids = fields.One2many("grove.publish.event", "product_tmpl_id", string="Publish Events")
    grove_publish_event_count = fields.Integer(compute="_compute_grove_publish_event_count")

    def _compute_grove_publish_event_count(self):
        Event = self.env["grove.publish.event"]
        for record in self:
            # _origin.id is the real DB id (0/False for an unsaved record in
            # create mode), so the stat button computes without hitting a NewId.
            origin_id = record._origin.id
            record.grove_publish_event_count = (
                Event.search_count([("product_tmpl_id", "=", origin_id)]) if origin_id else 0
            )

    def action_view_grove_publish_events(self):
        """Open the publish-webhook delivery log for this product (stat button)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Publish Events"),
            "res_model": "grove.publish.event",
            "view_mode": "list,form",
            "domain": [("product_tmpl_id", "=", self.id)],
            "context": {"search_default_product_tmpl_id": self.id},
        }

    def action_publish_guide(self):
        """Publish the approved species guide: emit a signed webhook to grove-sites.

        The "Draft-guide" publish action (GOL-985). Fires an HMAC-signed
        `guide.publish` event so the tenant's Next.js storefront revalidates the
        product page, and records the delivery in `grove.publish.event` for
        audit/replay. Gated on the same approval flag the API serializer honours
        (`grove_guide_ready`) — you cannot push an un-reviewed draft live.

        Delivery failures do NOT raise (the event row is kept for retry); we
        surface the outcome as a UI notification instead.
        """
        self.ensure_one()
        if not self.grove_guide_ready:
            raise UserError(
                _("Approve the guide first: tick 'Guide Approved for Storefront' before publishing it to the site.")
            )
        # Audit rows are system-owned: publishing writes the ledger + delivers
        # regardless of the approver's group, same as the Stripe event ledger.
        event = self.env["grove.publish.event"].sudo().publish_guide(self)
        if event.state == "delivered":
            message = _("Guide published — storefront revalidation triggered.")
            level = "success"
        else:
            message = _("Publish webhook failed: %s. Retry from the Publish Events log.") % (
                event.error or _("unknown error")
            )
            level = "warning"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("Publish Guide"), "message": message, "type": level, "sticky": False},
        }

    # Shipping tier drives the per-tree zone rate at checkout
    # (models/shipping_zones.py). Default "potted" = the higher tier, so an
    # untagged product can never be undercharged.
    grove_shipping_tier = fields.Selection(
        [("bareroot", "Bareroot"), ("potted", "Potted")],
        string="Grove Shipping Tier",
        default="potted",
        help="Bareroot ships (per-box zone rates, Box Engine v2); potted is "
        "farm pickup only — it cannot ship. An untagged product defaults to "
        "potted so it can never ship undercharged.",
    )

    # Tree length class (Box Engine v2): the minimum box length in inches this
    # tree's height requires when packed. Both descoped boxes are 24" long, so
    # length is now only a fit GATE (a tree over 24" has no box) — it no longer
    # picks between boxes. The tall 3-5 yr classes (32"/46") left the near-term
    # catalog with their boxes (CEO directive 2026-09-07); a product still tagged
    # over 24" fails safe at checkout (no shippable box) until one is restocked.
    # Values mirror shipping_boxes.LENGTH_CLASSES.
    grove_tree_length = fields.Selection(
        [("16", '16" (small whip)'), ("20", '20"')],
        string="Grove Tree Length Class",
        default="20",
        help='Height class this tree needs. Both current shipping boxes are 24" '
        'long; a tree over 24" has no box and cannot ship until one is restocked.',
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
