import logging
import os
import re

import requests
from markupsafe import Markup, escape
from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# Plant-fact enrichment providers (GOL-2383/B). The pure mapping + the USDA
# provider are wrapped by the Fetch-facts handler below; Perenual is never
# called from this file — it is enqueued as a grove.enrich.job and drained by
# the budgeted cron (models/grove_enrich_job.py).
from ..services.plant_data import mapping as plant_mapping
from ..services.plant_data.usda import USDAProvider

_logger = logging.getLogger(__name__)

# ── Preorder cap (GOL-2171) ──────────────────────────────────────────────
# The global default preorder cap lives in ir.config_parameter under this key
# (seeded to 50 by data/grove_config_params.xml, noupdate so an admin edit
# survives upgrades). A per-template override (grove_preorder_cap) wins when
# set; 0/unset means "inherit this global". Kept as a named constant so the
# controller and tests reference the same key.
PREORDER_CAP_PARAM = "grove_headless.preorder_cap_default"
PREORDER_CAP_SEED = 50

# ── Listing-content gate (GOL-2382, spec 2026-09-21) ─────────────────────
# The 12 growing facts every plant listing must carry before it can be
# published, in the order the missing-items banner and the form field group use.
# kind drives the "is set" test: ints count when > 0, chars/selections when
# non-blank after strip. The other three completeness requirements (storefront
# description, approved care guide, facts reviewed) are checked separately with
# custom labels so the banner reads the way Josh signed off on ("Plant Spacing,
# Chill Hours, Care guide approval, Facts reviewed").
_GROVE_REQUIRED_FACTS = [
    ("grove_botanical_name", "char"),
    ("grove_zone_min", "int"),
    ("grove_zone_max", "int"),
    ("grove_layer", "char"),
    ("grove_sun", "char"),
    ("grove_mature_size", "char"),
    ("grove_mature_spread", "char"),
    ("grove_spacing", "char"),
    ("grove_soil", "char"),
    ("grove_pollination", "char"),
    ("grove_years_to_fruit", "char"),
    ("grove_chill_hours", "char"),
]

# Storefront-facing content fields. Every one gets tracking=True (chatter shows
# who/what changed it) and a machine write to any of them — one that stamps
# grove_facts_provenance in the same vals, i.e. the enrichment (B) or drafter (C)
# path — clears the human "Facts reviewed" sign-off, per the field's contract.
_GROVE_CONTENT_FACT_FIELDS = frozenset(name for name, _kind in _GROVE_REQUIRED_FACTS) | frozenset(
    {
        "grove_growth_rate",
        "grove_watering",
        "grove_bloom_season",
        "grove_harvest_season",
        "grove_wildlife",
    }
)

# Human labels for the three non-fact completeness requirements. Kept explicit
# (not derived from field.string) so the banner text matches the spec exactly.
_GROVE_LABEL_DESCRIPTION = "Description"
_GROVE_LABEL_GUIDE = "Care guide approval"
_GROVE_LABEL_REVIEWED = "Facts reviewed"

# Dependencies shared by the two listing-status computes. grove_listing_complete
# (stored, the publish gate) and grove_listing_missing (non-stored banner text)
# both derive from _grove_missing_items(), but they use SEPARATE compute methods
# so that *reading* the non-stored banner never triggers a write to the stored
# gate flag (GOL-2471). Kept as one tuple so the two @api.depends can't drift.
_GROVE_LISTING_STATUS_DEPENDS = (
    "grove_botanical_name",
    "grove_zone_min",
    "grove_zone_max",
    "grove_layer",
    "grove_sun",
    "grove_mature_size",
    "grove_mature_spread",
    "grove_spacing",
    "grove_soil",
    "grove_pollination",
    "grove_years_to_fruit",
    "grove_chill_hours",
    "description_ecommerce",
    "website_description",
    "grove_guide_ready",
    "grove_facts_reviewed",
)


def _html_is_blank(value):
    """True when an HTML field has no visible text after stripping tags.

    Odoo stores an "empty" rich-text field as markup like ``<p><br></p>`` or
    ``<p>\xa0</p>`` rather than a falsy value, so a bare truthiness check would
    wrongly count those as filled. Strip tags and non-breaking spaces, then look
    for any remaining non-whitespace text.
    """
    if not value:
        return True
    text = re.sub(r"<[^>]+>", " ", value)
    text = text.replace("\xa0", " ").replace("&nbsp;", " ")
    return not text.strip()


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

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # A create that publishes a gated plant must still pass the gate.
        for record in records:
            if record.website_published:
                record._grove_check_publish_gate()
        return records

    def write(self, vals):
        # An enrichment/agent write stamps grove_facts_provenance alongside the
        # fact it wrote; that invalidates the human "Facts reviewed" sign-off
        # unless the write is itself (re)setting the flag. Human form edits never
        # touch provenance, so their sign-off survives.
        if (
            "grove_facts_reviewed" not in vals
            and "grove_facts_provenance" in vals
            and _GROVE_CONTENT_FACT_FIELDS.intersection(vals)
        ):
            vals = dict(vals, grove_facts_reviewed=False)
        if self._GROVE_AVAILABILITY_FIELDS.intersection(vals):
            self.env["grove.publish.event"].sudo().note_availability_candidates(self)
        # Snapshot which records are crossing the publish transition BEFORE the
        # write, so an already-published incomplete product editing one field is
        # never re-gated. `website_published` is the stored related of
        # `is_published`; a direct `is_published=True` write (data import,
        # XML-RPC, list-view toggle, server action) must be gated too.
        publishing = vals.get("website_published") or vals.get("is_published")
        transitioning = self.filtered(lambda r: not r.website_published) if publishing else self.browse()
        res = super().write(vals)
        for record in transitioning:
            record._grove_check_publish_gate()
        return res

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
    grove_botanical_name = fields.Char(string="Botanical Name", tracking=True)
    grove_zone_min = fields.Integer(string="USDA Zone Min", tracking=True)
    grove_zone_max = fields.Integer(string="USDA Zone Max", tracking=True)
    grove_layer = fields.Selection(
        [
            ("canopy", "Canopy"),
            ("understory", "Understory"),
            ("shrub", "Shrub"),
            ("ground", "Ground cover"),
            ("vine", "Vine"),
        ],
        string="Food Forest Layer",
        tracking=True,
    )
    grove_sun = fields.Selection(
        [("full", "Full sun"), ("partial", "Partial sun"), ("shade", "Shade")],
        string="Sun Requirement",
        tracking=True,
    )
    grove_mature_size = fields.Char(string="Mature Size", tracking=True)
    grove_spacing = fields.Char(string="Plant Spacing", tracking=True)
    grove_soil = fields.Char(string="Soil", tracking=True)

    # ── Listing content gate: new required + optional facts (GOL-2382) ──
    # Required chars joining the 12-fact required set. "Not applicable" is a
    # valid non-blank value for non-fruiting/ornamental plants (chill hours,
    # pollination, years to fruit).
    grove_mature_spread = fields.Char(string="Mature Spread", tracking=True, help='Display-only, e.g. "6–8 ft".')
    grove_chill_hours = fields.Char(
        string="Chill Hours",
        tracking=True,
        help='e.g. "450–550"; enter "Not applicable" for non-fruiting plants.',
    )
    grove_pollination = fields.Char(
        string="Pollination",
        tracking=True,
        help='e.g. "Self-fertile" / "Needs a second variety".',
    )
    grove_years_to_fruit = fields.Char(
        string="Years to Fruit",
        tracking=True,
        help='e.g. "2–4 years"; "Not applicable" for ornamentals.',
    )
    # Optional facts (auto-filled by enrichment in GOL-2383/B); not gated.
    grove_growth_rate = fields.Selection(
        [("slow", "Slow"), ("moderate", "Moderate"), ("fast", "Fast")],
        string="Growth Rate",
        tracking=True,
    )
    grove_bloom_season = fields.Char(string="Bloom Season", tracking=True, help='e.g. "Late spring".')
    grove_harvest_season = fields.Char(string="Harvest Season", tracking=True, help='e.g. "Summer–winter".')
    grove_watering = fields.Selection(
        [("low", "Low"), ("moderate", "Moderate"), ("high", "High")],
        string="Watering",
        tracking=True,
    )
    grove_wildlife = fields.Char(string="Wildlife", tracking=True, help='e.g. "Attracts bees, birds".')

    # eCommerce marketing description + care guide are content fields too, so
    # extend the inherited definitions to track changes in chatter. The
    # storefront description becomes description_ecommerce (the PDP renders it as
    # description_html); description_sale reverts to its Odoo quotation/invoice
    # role and is no longer the storefront copy. website_description carries the
    # care guide, gated on the storefront by grove_guide_ready as before.
    description_ecommerce = fields.Html(tracking=True)
    website_description = fields.Html(tracking=True)

    # ── Provenance and workflow (GOL-2382) ──────────────────────────────
    grove_facts_provenance = fields.Json(
        string="Facts Provenance",
        help="Per-field {source, ref, at} record of each auto-fill/draft write, e.g. "
        '{"grove_soil": {"source": "perenual", "ref": "1234", "at": "2026-09-21T..."}}. '
        "Writing this (an enrichment/agent write) clears the Facts Reviewed sign-off.",
    )
    grove_usda_symbol = fields.Char(string="USDA PLANTS Symbol", help='Resolved PLANTS symbol, e.g. "DIVI5"; editable.')
    grove_perenual_id = fields.Integer(string="Perenual Species Id", help="Resolved Perenual species id; editable.")
    grove_facts_reviewed = fields.Boolean(
        string="Facts reviewed for storefront",
        default=False,
        tracking=True,
        help="Tick once the growing facts have been reviewed for the storefront. "
        "Cleared automatically by any enrichment or agent write to a fact.",
    )
    grove_gate_exempt = fields.Boolean(
        string="Exempt from listing-content gate",
        default=False,
        tracking=True,
        help="Bundles, gift cards and supplies are not plant listings — tick to "
        "let them publish without the growing-facts / description / guide gate.",
    )
    grove_draft_state = fields.Selection(
        [("none", "None"), ("requested", "Draft requested"), ("drafted", "Draft ready")],
        string="Content Draft State",
        default="none",
        help="Tracks the Paperclip content-drafter workflow (GOL-2384/C).",
    )
    grove_listing_complete = fields.Boolean(
        string="Listing complete",
        compute="_compute_grove_listing_complete",
        store=True,
        compute_sudo=True,
        help="True when every required fact, the storefront description, the "
        "approved care guide and the Facts Reviewed sign-off are present.",
    )
    grove_listing_missing = fields.Char(
        string="Missing for storefront",
        compute="_compute_grove_listing_missing",
        compute_sudo=False,
        help="Human-readable list of the items still needed before this plant can "
        "be published; empty when the listing is complete.",
    )

    @api.depends(*_GROVE_LISTING_STATUS_DEPENDS)
    def _compute_grove_listing_complete(self):
        # Stored publish-gate flag. Kept in a compute method of its own so that
        # reading the sibling grove_listing_missing banner cannot write it
        # (GOL-2471). compute_sudo=True: the gate is evaluated with full access
        # regardless of who triggers the recompute.
        for record in self:
            record.grove_listing_complete = not record._grove_missing_items()

    @api.depends(*_GROVE_LISTING_STATUS_DEPENDS)
    def _compute_grove_listing_missing(self):
        # Non-stored banner text. Read-only side-effect-free view of the same
        # requirements — it must never touch the stored gate flag.
        for record in self:
            record.grove_listing_missing = ", ".join(record._grove_missing_items())

    def _grove_missing_items(self):
        """Ordered list of human labels for every unmet completeness requirement.

        Empty means the listing is complete. Order follows the spec so the banner
        reads facts first, then description, care guide, facts reviewed.
        """
        self.ensure_one()
        missing = []
        for name, kind in _GROVE_REQUIRED_FACTS:
            value = self[name]
            if kind == "int":
                is_set = bool(value) and value > 0
            else:
                is_set = bool(str(value or "").strip())
            if not is_set:
                missing.append(self._fields[name].string)
        if _html_is_blank(self.description_ecommerce):
            missing.append(_GROVE_LABEL_DESCRIPTION)
        if _html_is_blank(self.website_description) or not self.grove_guide_ready:
            missing.append(_GROVE_LABEL_GUIDE)
        if not self.grove_facts_reviewed:
            missing.append(_GROVE_LABEL_REVIEWED)
        return missing

    # ── Fetch facts: USDA sync + enqueue Perenual (GOL-2391/B) ───────────
    def action_fetch_facts(self):
        """Form button: fill empty growing facts from USDA now, queue Perenual.

        USDA (free, no key) runs synchronously and writes only the empty fields
        it is authoritative-first for; its zone/spacing hints are posted to
        chatter, never written. Perenual owns the rest but is rate-limited, so it
        is enqueued as a grove.enrich.job and drained by the budgeted cron.
        """
        for record in self:
            record._grove_fetch_facts_sync()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Fetch facts"),
                "message": _(
                    "USDA fields applied where empty (see the chatter). "
                    "Perenual enrichment queued — it runs on the budgeted schedule."
                ),
                "type": "success",
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def _grove_fetch_facts_sync(self):
        self.ensure_one()
        provider = USDAProvider()
        facts = provider.lookup(self.grove_botanical_name, cached_id=self.grove_usda_symbol or None)
        if facts.resolved_id and not self.grove_usda_symbol:
            self.grove_usda_symbol = facts.resolved_id
        self._grove_apply_facts(facts, "usda")
        self._grove_enqueue_perenual()

    def _grove_enqueue_perenual(self):
        """Queue one Perenual enrich job for this product, unless one is pending."""
        self.ensure_one()
        Job = self.env["grove.enrich.job"].sudo()
        existing = Job.search(
            [
                ("product_tmpl_id", "=", self.id),
                ("provider", "=", "perenual"),
                ("state", "in", ("queued", "running")),
            ],
            limit=1,
        )
        if not existing:
            Job.create({"product_tmpl_id": self.id, "provider": "perenual"})

    def _grove_field_empty(self, name):
        """True when a growing-fact field holds no usable value.

        Integers (zones) count as empty at 0/False; chars and selections count
        as empty when blank after strip.
        """
        value = self[name]
        if self._fields[name].type == "integer":
            return not value
        return not str(value or "").strip()

    def _grove_apply_facts(self, facts, provider_name):
        """Apply a provider's PlantFacts to this template, conservatively.

        A field is written only when (a) this provider is authoritative-FIRST for
        it in FIELD_PRECEDENCE and (b) the field is currently empty. Every write
        is recorded in grove_facts_provenance and echoed to chatter one line per
        field; hints/candidates are chatter-only. Writing provenance alongside a
        content field clears the human "Facts reviewed" sign-off (see write()).
        Returns True when at least one field was filled.
        """
        self.ensure_one()
        writes = {}
        lines = []
        provenance = dict(self.grove_facts_provenance or {})
        now_iso = fields.Datetime.now().isoformat()
        for name, fv in facts.fields.items():
            order = plant_mapping.FIELD_PRECEDENCE.get(name, ())
            if not order or order[0] != provider_name:
                continue  # this provider is not authoritative-first for the field
            if not self._grove_field_empty(name):
                continue  # never overwrite an existing value
            writes[name] = fv.value
            provenance[name] = {"source": fv.source, "ref": fv.ref, "at": now_iso}
            lines.append(f"{escape(self._fields[name].string)}: {escape(str(fv.value))} (source: {escape(fv.source)})")

        # Hints and candidates are chatter-only — never written to a field.
        if facts.hints:
            self.message_post(
                body=Markup("<b>{}</b> notes:<br/>{}").format(
                    provider_name.upper(),
                    Markup("<br/>").join(escape(h) for h in facts.hints),
                )
            )
        if facts.candidates:
            self.message_post(
                body=Markup("<b>{}</b> found no exact match. Candidates:<br/>{}").format(
                    provider_name.upper(),
                    Markup("<br/>").join(escape(c) for c in facts.candidates),
                )
            )
        if not writes:
            return False

        writes["grove_facts_provenance"] = provenance
        self.write(writes)
        self.message_post(
            body=Markup("Auto-filled {} field(s) from <b>{}</b>:<br/>{}").format(
                len(lines), provider_name.upper(), Markup("<br/>").join(Markup(line) for line in lines)
            )
        )
        return True

    # ── Request content draft (GOL-2384/C) ──────────────────────────────
    def action_request_draft(self):
        """Form button: hand this listing to the Paperclip content-drafter.

        Flips ``grove_draft_state`` to ``requested`` and posts a chatter note.
        The AgenticOS routine ``grove-content-drafter`` polls Odoo over XML-RPC
        for ``[('grove_draft_state','=','requested')]`` and writes the storefront
        description + care guide from the recorded facts, one product per run.

        Guard: a botanical name AND at least one fact fetched into provenance are
        required, so the agent never drafts from nothing — run **Fetch facts**
        first. A gate-exempt product (bundle/gift card/supply) is not a plant
        listing and cannot request a draft.
        """
        for record in self:
            record._grove_request_draft_one()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Request content draft"),
                "message": _(
                    "Draft requested. The content-drafter routine will pick this "
                    "up on its next run (every 15 min) and write the storefront "
                    "description and care guide from the recorded facts."
                ),
                "type": "success",
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def _grove_request_draft_one(self):
        self.ensure_one()
        if self.grove_gate_exempt:
            raise UserError(
                _("This product is exempt from the listing-content gate, so it does not take a content draft.")
            )
        if not (self.grove_botanical_name or "").strip():
            raise UserError(_("Set the Botanical Name before requesting a content draft."))
        if not self.grove_facts_provenance:
            raise UserError(
                _(
                    "Run Fetch facts first: the drafter needs at least one fetched fact "
                    "(with its source) recorded in provenance so it never drafts from nothing."
                )
            )
        self.grove_draft_state = "requested"
        self.message_post(
            body=Markup("<b>Content draft requested.</b> Queued for the grove-content-drafter routine (GOL-2384/C).")
        )

    # ── Publish gate (GOL-2382) ─────────────────────────────────────────
    def _grove_is_gated(self):
        """True when this template must pass the listing-content gate to publish.

        Gated == a consumable plant: type 'consu', not exempt, and categorised
        under the Plants root from data/grove_product_categories.xml. Services,
        supplies, bundles (exempt) and anything outside the Plants tree publish
        freely.
        """
        self.ensure_one()
        if self.grove_gate_exempt or self.type != "consu":
            return False
        plants_root = self.env.ref("grove_headless.categ_plants", raise_if_not_found=False)
        categ = self.categ_id
        if not plants_root or not categ:
            return False
        # parent_path is the materialised root→node id path (e.g. "1/7/"); a
        # descendant's path is prefixed by its ancestor's, and the root's own
        # path is a prefix of itself, so this covers "under Plants, inclusive".
        return bool(
            categ.parent_path and plants_root.parent_path and categ.parent_path.startswith(plants_root.parent_path)
        )

    def _grove_check_publish_gate(self):
        """Raise UserError if a gated template is being published while incomplete.

        Called only for records that just transitioned website_published False →
        True, so an already-published incomplete product keeps selling and can be
        edited field by field (the nightly audit, GOL-2385/D, chases it instead).
        """
        self.ensure_one()
        if not self.website_published or not self._grove_is_gated():
            return
        missing = self._grove_missing_items()
        if missing:
            raise UserError(
                _(
                    "Cannot publish %(name)s: missing %(items)s",
                    name=self.display_name,
                    items=", ".join(missing),
                )
            )

    # ── Nightly listing-content audit (GOL-2385/D, spec 2026-09-21) ──────
    @api.model
    def cron_audit_listing_content(self):
        """Daily 06:00 America/New_York: chase published-but-incomplete plants.

        Selects every gated (plant) template that is published yet still missing
        a required fact, description, care guide or sign-off, posts one Discord
        summary to the ops webhook and schedules one open "To Do" activity (due
        today) per product on its responsible user — skipping any product that
        already carries an open audit activity so nightly runs never pile up.

        This never unpublishes anything: an already-selling incomplete plant
        keeps selling (the publish gate only blocks the False→True transition);
        the audit is the follow-up that gets a human to finish the listing.
        """
        incomplete = self._grove_audit_incomplete_listings()
        if not incomplete:
            _logger.info("listing-content audit: no published plant listing is incomplete")
            return
        self._grove_audit_post_discord(incomplete)
        scheduled = self._grove_audit_schedule_activities(incomplete)
        _logger.info(
            "listing-content audit: %d incomplete published listing(s), %d new activity(ies) scheduled",
            len(incomplete),
            scheduled,
        )

    @api.model
    def _grove_audit_incomplete_listings(self):
        """Published, gated, incomplete plant templates, ordered by name.

        grove_listing_complete is stored, so the completeness half is a plain
        domain; gating (under the Plants category, consu, not exempt) needs the
        parent_path prefix check, so it is filtered in Python — the published +
        incomplete pre-filter keeps that set tiny.
        """
        candidates = self.sudo().search(
            [("website_published", "=", True), ("grove_listing_complete", "=", False)],
            order="name",
        )
        return candidates.filtered(lambda t: t._grove_is_gated())

    @api.model
    def _grove_audit_discord_text(self, products):
        """Build the ops-channel summary line (kept pure for payload-shape tests).

        Mirrors the spec example: "3 published listings incomplete: Apple —
        Plant Spacing, Soil, Description, Care guide; ...".
        """
        parts = [f"{p.display_name} — {p.grove_listing_missing}" for p in products]
        return f"{len(products)} published listing(s) incomplete: " + "; ".join(parts)

    @api.model
    def _grove_audit_post_discord(self, products):
        """Post the audit summary to the ops Discord webhook (best-effort).

        Reuses the same env-var mechanism as grove.order.rollup._discord_digest;
        this is a data-quality/ops report so it targets DISCORD_OPS_WEBHOOK_URL
        (not the orders channel). A missing webhook logs and no-ops rather than
        raising — the activities below are the durable half of the audit.
        """
        url = os.environ.get("DISCORD_OPS_WEBHOOK_URL", "")
        if not url:
            _logger.info("listing-content audit: DISCORD_OPS_WEBHOOK_URL unset — skipping Discord post")
            return
        text = self._grove_audit_discord_text(products)
        try:
            requests.post(url, json={"content": text[:2000]}, timeout=10)
        except Exception:
            _logger.warning("listing-content audit: Discord notify failed", exc_info=True)

    @api.model
    def _grove_audit_schedule_activities(self, products):
        """One open To-Do per incomplete product, deduped; returns count created.

        Assigned to the product's responsible user (falling back to the admin
        user, then the current user). A product that already carries an open
        activity of this type is skipped, so re-running the audit is idempotent.
        """
        todo_type = self.env.ref("mail.mail_activity_data_todo")
        model_id = self.env["ir.model"]._get_id("product.template")
        deadline = fields.Date.context_today(self)
        fallback = self.env.ref("base.user_admin", raise_if_not_found=False) or self.env.user
        Activity = self.env["mail.activity"].sudo()
        created = 0
        for product in products:
            existing = Activity.search_count(
                [
                    ("res_model_id", "=", model_id),
                    ("res_id", "=", product.id),
                    ("activity_type_id", "=", todo_type.id),
                ]
            )
            if existing:
                continue
            user = product.responsible_id or fallback
            Activity.create(
                {
                    "res_model_id": model_id,
                    "res_id": product.id,
                    "activity_type_id": todo_type.id,
                    "summary": _("Complete storefront listing"),
                    "note": Markup("<p>{}</p>").format(
                        _("Missing before this plant is a complete listing: %s") % product.grove_listing_missing
                    ),
                    "date_deadline": deadline,
                    "user_id": user.id,
                }
            )
            created += 1
        return created

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
