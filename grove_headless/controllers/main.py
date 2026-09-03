import hmac
import html
import json
import logging
import os
import re
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import timezone as _timezone

import psycopg2
import requests
from odoo import http
from odoo.http import Response, request

from ..hooks import WV_GROUP_NAME, WV_MUNI_NAME, WV_STATE_NAME
from ..models import stripe_gateway
from ..models.image_resolution import GROVE_MIN_IMAGE_LONG_EDGE
from ..models.newsletter import newsletter_tag_names
from ..models.order_alerts import format_merchant_email, format_new_order_discord
from ..models.preorder_email import confirmation_deposit_line, preship_balance_line
from ..models.shipment_email import NOTIFY_STATUSES, shipment_notice_copy
from ..models.shipping_boxes import packing_mode
from ..models.shipping_calendar import (
    MODE_PREORDER,
    merge_calendar_override,
    resolve_fulfillment,
    serialize_ship_options,
    ship_options,
    usda_zone_for_zip,
)
from ..models.shipping_zones import (
    canonical_state_code,
    compute_order_shipping,
    rate_feed,
    single_tree_rate,
    unshippable_reason,
    zone_for_state,
)
from ..models.shippo_client import is_valid_tracking
from .product_domain import build_product_domain, slugify, zone_response

# Grove has sales-tax nexus only in West Virginia, so the WV 7% tax (the product
# default set by hooks.setup_wv_sales_tax) legally applies only to a WV-destination
# shipment. Any order shipping elsewhere must have the WV tax stripped — see
# _apply_destination_tax. The set is the WV group tax + its two components so a
# line carrying either the combined group or a bare component is caught.
WV_TAX_NAMES = frozenset({WV_GROUP_NAME, WV_STATE_NAME, WV_MUNI_NAME})
WV_NEXUS_STATE = "WV"

_logger = logging.getLogger(__name__)


# Defense-in-depth caps on contact/address fields. Must mirror the BFF's limits
# (see @grove/odoo-client) — anyone with a valid API key can call this endpoint
# directly, so we never trust the BFF to have already enforced these.
MAX_NAME = 200
MAX_EMAIL = 254
MAX_PHONE = 30
MAX_STREET = 200
MAX_CITY = 100
MAX_STATE = 50
MAX_ZIP = 20
MAX_COUNTRY = 100

# Newsletter opt-in caps. Brand/source/interest values become res.partner.category
# tag names, so bound them to keep the tag table from being flooded by a caller
# with a valid API key posting junk. Interests are also capped in count.
MAX_BRAND = 50
MAX_SOURCE = 100
MAX_INTEREST = 50
MAX_INTERESTS = 20

EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def _check_lengths(values: dict, limits: dict) -> str | None:
    """Return an error message if any value in `values` is not a valid bounded string.

    Non-string non-None values fail the same as too-long strings — anyone with
    a valid API key (auth="bearer") can hit the controller directly with junk
    types like `{"name": 5, "zip": 28801}`, so `len(v)` on a non-string would
    raise TypeError and surface as a 500 with a Werkzeug traceback.
    """
    for key, limit in limits.items():
        v = values.get(key)
        if v is None:
            continue
        if not isinstance(v, str):
            return f"{key} must be a string"
        if len(v) > limit:
            return f"{key} exceeds {limit} characters"
    return None


def _today_utc() -> _date:
    """Calendar date in UTC — the basis the frontend fulfillment resolver uses
    (grove-sites product-view.tsx / fulfillment-mode.ts). The shipping-calendar
    MODE that gates checkout charging (GOL-1309) and the /shipping/options
    preview both resolve on this UTC basis so they agree on the wave boundary; a
    server-local ``_date.today()`` could differ by a day near midnight and land
    a shopper on a different mode than the page they were shown. Legacy rate/box
    helpers (packing_mode, ship_options, single_tree_rate) stay on server-local
    ``_date.today()`` — a distinct axis, out of scope here."""
    return _datetime.now(_timezone.utc).date()


# Fields exposed in the public product list (keep minimal for performance)
PRODUCT_LIST_FIELDS = [
    "id",
    "name",
    "list_price",
    "default_code",
    "website_published",
    "grove_featured",
    "image_128",
    "grove_slug",
    # Purchasability flag. The grid + ?cat= facets now include published-but-
    # not-for-sale "coming soon" placeholders (sale_ok=False, GOL-757/760 —
    # build_product_domain gates on website_published alone), so every card
    # needs sale_ok to render as not-purchasable ("Coming soon") instead of
    # falsely claiming "In stock". The detail page likewise reads it to lock the
    # buy box (no Add-to-Cart, no Bareroot "Reserve" deposit) — without it the
    # frontend derives purchasability from stock alone and a qty-0 Bareroot
    # placeholder leaks a live reservation.
    "sale_ok",
]

PRODUCT_DETAIL_FIELDS = PRODUCT_LIST_FIELDS + [
    "description_sale",
    "grove_seo_description",
    # website_description holds the species "guide" body (agent-drafted, then
    # human-reviewed). It is read here but the value is GATED by
    # _gate_guide_fields on the way out — an un-approved draft is withheld even
    # though the field is in the read set. grove_guide_ready is Wes's approval
    # flag. See GATH-130 (this contract) / GATH-127 (frontend GuideBlock) /
    # GATH-121 (the drafting routine that writes website_description).
    "website_description",
    "grove_guide_ready",
    "categ_id",
    "currency_id",
    "website_url",
    "image_1920",
]

# Fields only present when the `stock` module is installed.
OPTIONAL_STOCK_FIELDS = ["qty_available"]


def _available_fields(model, fields):
    """Filter `fields` to those that actually exist on the model."""
    return [f for f in fields if f in model._fields]


def _json_response(data, status=200):
    """Return a plain JSON HTTP response (not Odoo JSON-RPC)."""
    body = json.dumps(data, default=str)
    return Response(
        body,
        status=status,
        content_type="application/json",
    )


# Each tenant sandbox (nursery/ggg/goldberry) signs its Stripe webhook events
# with a DIFFERENT endpoint secret, yet all tenants POST to the single
# /grove/api/v1/stripe/webhook URL. So verification must try EVERY configured
# tenant secret and accept on any match (mirrors Stripe's own guidance for one
# endpoint serving multiple signing secrets). Names are lowercase to match the
# odoo process-env convention already used for stripe_test_* (see the QA
# compose `environment:` block); Terra wires these per-tenant vars in T2
# (GOL-1016). `stripe_test_webhook_secret` stays for backward-compat with the
# legacy single-tenant env.
STRIPE_WEBHOOK_SECRET_ENV_VARS = (
    "stripe_webhook_secret_nursery",
    "stripe_webhook_secret_ggg",
    "stripe_webhook_secret_goldberry",
    "stripe_test_webhook_secret",
)


def _configured_webhook_secrets():
    """Return the non-empty webhook signing secrets from the environment, in a
    stable order and de-duplicated (a value shared across env vars is tried
    once)."""
    secrets = []
    for name in STRIPE_WEBHOOK_SECRET_ENV_VARS:
        value = os.environ.get(name, "")
        if value and value not in secrets:
            secrets.append(value)
    return secrets


def _verify_stripe_webhook(raw, sig, secrets):
    """Verify a Stripe-Signature header against ANY of the given tenant secrets.

    Returns (True, None) on the first secret that validates, else
    (False, last_error). Every secret is tried WITHOUT an early exit on match:
    the per-secret HMAC is cheap and running them all keeps the response time
    from leaking (via how many secrets were tried) which tenant signed the
    event.
    """
    verified = False
    last_error = None
    for secret in secrets:
        try:
            stripe_gateway.verify_webhook_signature(raw, sig, secret)
            verified = True
        except stripe_gateway.StripeError as exc:
            last_error = exc
    # Honour the documented contract: (True, None) on success. Every secret is
    # still tried (constant-time), but a match must not surface the mismatch
    # error from a later, non-owning tenant's secret (GOL-2014).
    return verified, (None if verified else last_error)


# Each tenant (nursery/ggg/goldberry) is a separate LLC with its OWN Stripe
# account + secret key, so a checkout session and a refund must be created with
# the key belonging to the tenant that owns the order. The webhook-VERIFY path
# above cannot know which tenant signed an event, so it tries every secret;
# here the tenant IS known (from the website/company), so we select exactly one
# key — creating a session with the wrong key would route that LLC's revenue
# into another LLC's account, and a refund whose payment_intent lives in a
# different account fails outright. Names are lowercase to match the odoo
# process-env convention already used for stripe_*_secret (Terra wires the
# per-tenant vars, GOL-973). `stripe_test_secret_key` is the legacy single-key
# fallback: an env that sets only it (and no per-tenant keys) routes every
# tenant to one account — i.e. a single merchant-of-record — with zero code
# change, so the per-tenant-vs-single-account choice stays a config decision.
STRIPE_SECRET_KEY_ENV_PREFIX = "stripe_secret_key_"
STRIPE_SECRET_KEY_LEGACY_ENV = "stripe_test_secret_key"


def _tenant_secret_key(tenant):
    """Return the Stripe secret key to charge/refund `tenant`'s orders with.

    Prefers the per-tenant ``stripe_secret_key_{tenant}`` env var so each LLC's
    money lands in its own account; falls back to the legacy single-tenant
    ``stripe_test_secret_key`` when no per-tenant key is configured (or the
    tenant slug is unknown). Returns ``""`` when neither is set so callers can
    keep emitting the existing "not configured yet" 503.
    """
    if tenant:
        key = os.environ.get(f"{STRIPE_SECRET_KEY_ENV_PREFIX}{tenant}", "")
        if key:
            return key
    return os.environ.get(STRIPE_SECRET_KEY_LEGACY_ENV, "")


def _image_url(model, record, size):
    """Return the ``/web/image`` path for ``record``'s image at ``size``, or None.

    Odoo's ``/web/image/...`` route serves its OWN gray placeholder PNG at HTTP
    200 for records with no image, so the storefront can't tell an imageless
    product apart from a real one (a valid 200 image, so the frontend's onError
    never fires). Gating on the image field's truthiness lets us emit null and
    hand imageless products to the frontend's branded botanical placeholder.

    ``size`` (e.g. ``"image_128"``) doubles as the presence gate: every image
    size derives from ``image_1920``, and the served field is already loaded, so
    checking it avoids reading extra image bytes.
    """
    return f"/web/image/{model}/{record.id}/{size}" if record[size] else None


def _serialize_product(product, fields):
    """Read a product recordset into a plain dict safe for JSON."""
    vals = product.read(fields)
    if not vals:
        return None
    record = vals[0]
    # Replace many2one tuples with {id, name} objects
    for key, value in record.items():
        if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], int):
            record[key] = {"id": value[0], "name": value[1]}
        # bytes (image) -> skip in JSON, use dedicated image URL instead
        if isinstance(value, bytes):
            record[key] = None
    return record


def _serialize_facts(product):
    """Growing-facts block for the detail endpoint (catalog spec 2026-07-13)."""
    return {
        "botanical_name": product.grove_botanical_name or "",
        "zone_min": product.grove_zone_min or None,
        "zone_max": product.grove_zone_max or None,
        "layer": product.grove_layer or "",
        "sun": product.grove_sun or "",
        "mature_size": product.grove_mature_size or "",
        "spacing": product.grove_spacing or "",
        "soil": product.grove_soil or "",
    }


def _gate_guide_fields(product, data):
    """Withhold the species-guide body until Wes has approved it.

    ``website_description`` carries the guide body written by the Paperclip
    guide-drafting routine (GATH-121). Agent-authored HTML is a weaker trust
    story than the human prose it replaces, so it must not reach any client
    until Wes ticks ``grove_guide_ready`` on the product form. The frontend
    GuideBlock (GATH-127) also gates on ``grove_guide_ready`` and sanitizes the
    body, but enforcing the gate here means an un-approved draft can never cross
    the API boundary at all — defense in depth, not just a hidden UI element.

    Mutates ``data`` in place (it is already in the read set) and returns it:
    - ``grove_guide_ready`` -> a plain bool the frontend keys its render on.
    - ``website_description`` -> the body when approved, else ``None``. Also
      ``None`` (never ``False`` / ``""``) when approved-but-empty, so the
      frontend renders its "coming soon" placeholder rather than an empty block.
    """
    ready = bool(product.grove_guide_ready)
    data["grove_guide_ready"] = ready
    data["website_description"] = (product.website_description or None) if ready else None
    return data


def _cultivar_count(product):
    """Distinct **cultivar** count for a template — the storefront "varieties".

    A template's ``product_variant_ids`` is the Cartesian product of its axes
    (Cultivar × Format), so a single-cultivar plant with a Potted/Bareroot Format
    axis has two variants but is still *one* variety (GOL-919). Count distinct
    Cultivar attribute values across the active variants instead, and floor at 1
    so a Format-only product (no Cultivar axis, so the set is empty) reads
    "1 variety" rather than "0".
    """
    cultivars = {
        value.name
        for variant in product.product_variant_ids
        for value in variant.product_template_variant_value_ids
        if value.attribute_id.name == "Cultivar"
    }
    return len(cultivars) or 1


def _template_rootstock(product):
    """Template-wide Rootstock value for products where propagation is metadata,
    not a purchasable axis (GOL-1117).

    Most grafted/seedling plants carry a *single* rootstock across every variant
    (e.g. all apple cultivars grafted on M.111). Modelling that as a variant-
    creating attribute would regenerate the whole Cultivar x Format grid — blowing
    away SKUs and stock. Instead it is a ``no_variant`` attribute line: one value,
    no variant explosion. Surface that value uniformly on each variant so the
    storefront renders a Rootstock metadata pill (GOL-1112).

    Only a single-value no_variant line is treated as template-wide; a multi-value
    line is ambiguous per-variant and reads as no axis ("").
    """
    for line in product.attribute_line_ids:
        if line.attribute_id.name == "Rootstock" and line.attribute_id.create_variant == "no_variant":
            names = line.product_template_value_ids.mapped("name")
            return names[0] if len(names) == 1 else ""
    return ""


def _structure_variant(variant, template_rootstock=""):
    """Structured variant entry: axes parsed into fields, not display-name strings.

    Reads ``product_template_attribute_value_ids`` (the full per-variant
    combination, single-value axes included) rather than
    ``product_template_variant_value_ids`` (only the axes with 2+ values, which
    Odoo treats as variant-*differentiating*). A single-cultivar plant — the
    common shape, where Format is the only multi-value axis — carries its
    Cultivar as a single-value ``create_variant='always'`` line; that value is
    absent from ``product_template_variant_value_ids``, so reading it there
    surfaced a blank ``cultivar`` for every such product (GOL-2014). The
    attribute-value field still excludes ``no_variant`` axes (e.g. a metadata
    Rootstock line), so the ``_template_rootstock`` fallback below is unaffected.
    """
    axis = {v.attribute_id.name: v.name for v in variant.product_template_attribute_value_ids}
    return {
        "id": variant.id,
        "display_name": variant.display_name,
        "sku": variant.default_code or "",
        "cultivar": axis.get("Cultivar", ""),
        "format": axis.get("Format", ""),
        # Propagation axis (GOL-1117): "grafted" vs "seedling" rootstock. Optional —
        # a real per-variant Rootstock axis wins; otherwise fall back to the
        # template-wide no_variant value (see _template_rootstock). Absent -> "",
        # which the storefront reads as "no rootstock pill / selector" (GOL-1112).
        "rootstock": axis.get("Rootstock", "") or template_rootstock,
        "price": variant.lst_price,
        "qty_available": variant.qty_available,
        "shipping_tier": variant.grove_effective_shipping_tier,
        "image_url": _image_url("product.product", variant, "image_128"),
    }


# Deterministic order for the variant list the PDP consumes (GOL-1868).
# product_variant_ids carries no `sequence`, so Odoo's default recordset order is
# a function of per-environment SKU/creation data — this is why prod surfaced
# Potted first while QA surfaced Bareroot first (GOL-1862). Rank shippable /
# preorderable tiers (bareroot) ahead of pickup-only ones (potted); unknown tiers
# sort last but stay deterministic via the id tiebreak, so every consumer
# receives an intentional order. Belt-and-braces only: the frontend must still
# not depend on array order for correctness (GOL-1862 already ensures it doesn't).
_SHIPPING_TIER_RANK = {"bareroot": 0, "potted": 1}


def _variant_sort_key(variant):
    tier = variant.grove_effective_shipping_tier or ""
    return (_SHIPPING_TIER_RANK.get(tier, len(_SHIPPING_TIER_RANK)), variant.id)


def _ordered_variants(product):
    """product_variant_ids in a deterministic, intentional order (GOL-1868)."""
    return product.product_variant_ids.sorted(key=_variant_sort_key)


def _serialize_images(product):
    """Gallery list: template hero first, then eCommerce media images."""
    images = []
    if product.image_1920:
        images.append(
            {
                "id": 0,
                "url": f"/web/image/product.template/{product.id}/image_1024",
                "thumb_url": f"/web/image/product.template/{product.id}/image_256",
            }
        )
    for media in product.product_template_image_ids:
        images.append(
            {
                "id": media.id,
                "url": f"/web/image/product.image/{media.id}/image_1024",
                "thumb_url": f"/web/image/product.image/{media.id}/image_256",
            }
        )
    return images


class GroveHeadlessAPI(http.Controller):
    """Public JSON endpoints for the Grove headless storefronts."""

    # ── Health ───────────────────────────────────────────────────────────

    @http.route(
        "/grove/api/v1/health",
        type="http",
        auth="none",
        methods=["GET"],
        csrf=False,
    )
    def health(self, **_kwargs):
        return _json_response({"status": "ok"})

    # ── Product list ─────────────────────────────────────────────────────

    @http.route(
        "/grove/api/v1/products",
        type="http",
        auth="public",
        website=True,
        methods=["GET"],
        csrf=False,
    )
    def product_list(self, **kwargs):
        website = request.website
        current_company = website.company_id

        # ?cat=<slug> browses by website (public) category — the storefront's
        # plant-type nav. Resolve the slug to public-category ids here (the pure
        # domain builder can't reach the Odoo category table). An unrecognised
        # slug resolves to [] -> the builder returns an empty set, not the whole
        # catalog.
        cat_category_ids = None
        if str(kwargs.get("cat") or "").strip():
            cat_slug = slugify(kwargs.get("cat"))
            categories = request.env["product.public.category"].sudo().search([])
            cat_category_ids = [c.id for c in categories if slugify(c.name) == cat_slug]

        domain = build_product_domain(kwargs, current_company.id, cat_category_ids=cat_category_ids)

        limit = min(int(kwargs.get("limit", 40)), 200)
        offset = int(kwargs.get("offset", 0))

        products = (
            request.env["product.template"]
            .sudo()
            .with_company(current_company)
            .search(domain, limit=limit, offset=offset, order="name asc")
        )
        total = request.env["product.template"].sudo().with_company(current_company).search_count(domain)

        items = []
        for product in products:
            data = _serialize_product(product, PRODUCT_LIST_FIELDS)
            if data:
                data["image_url"] = _image_url("product.template", product, "image_128")
                data["slug"] = data.pop("grove_slug", "") or ""
                data["tags"] = [{"id": t.id, "name": t.name} for t in product.product_tag_ids]
                data["categories"] = [
                    {"id": c.id, "name": c.name, "slug": slugify(c.name)} for c in product.public_categ_ids
                ]
                data["variant_count"] = len(product.product_variant_ids)
                data["cultivar_count"] = _cultivar_count(product)
                data["price_min"] = min(product.product_variant_ids.mapped("lst_price"), default=product.list_price)
                items.append(data)

        return _json_response(
            {
                "count": total,
                "limit": limit,
                "offset": offset,
                "results": items,
            }
        )

    # ── Product detail ───────────────────────────────────────────────────

    @http.route(
        "/grove/api/v1/products/<int:product_id>",
        type="http",
        auth="public",
        website=True,
        methods=["GET"],
        csrf=False,
    )
    def product_detail(self, product_id, **_kwargs):
        website = request.website
        current_company = website.company_id

        product = (
            request.env["product.template"]
            .sudo()
            .with_company(current_company)
            .search(
                [
                    ("id", "=", product_id),
                    ("website_published", "=", True),
                    ("company_id", "in", [current_company.id, False]),
                ],
                limit=1,
            )
        )

        if not product:
            return _json_response({"error": "Product not found"}, status=404)

        detail_fields = PRODUCT_DETAIL_FIELDS + _available_fields(product, OPTIONAL_STOCK_FIELDS)
        data = _serialize_product(product, detail_fields)
        # Gate the agent-drafted guide body: withhold website_description until
        # grove_guide_ready is set (GATH-130). Must run after the raw read.
        _gate_guide_fields(product, data)
        data["image_url"] = _image_url("product.template", product, "image_1920")
        # Stored-photo resolution (GOL-837) so content owners / audit tooling can
        # see which products serve a below-minimum source and need re-shooting.
        data["image"] = {
            "url": data["image_url"],
            "width": product.grove_image_width or None,
            "height": product.grove_image_height or None,
            "low_res": bool(product.grove_image_low_res),
            "min_long_edge": GROVE_MIN_IMAGE_LONG_EDGE,
        }
        template_rootstock = _template_rootstock(product)
        data["variants"] = [_structure_variant(v, template_rootstock) for v in _ordered_variants(product)]
        data["facts"] = _serialize_facts(product)
        data["tags"] = [{"id": t.id, "name": t.name} for t in product.product_tag_ids]
        data["categories"] = [{"id": c.id, "name": c.name, "slug": slugify(c.name)} for c in product.public_categ_ids]
        data["images"] = _serialize_images(product)

        return _json_response(data)

    # ── ZIP → USDA zone ──────────────────────────────────────────────────

    @http.route(
        "/grove/api/v1/zone",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def zone_lookup(self, **kwargs):
        """USDA zone for a ZIP — powers the 'Will this grow for me?' widget."""
        zip_raw = str(kwargs.get("zip", ""))
        body, status = zone_response(zip_raw, usda_zone_for_zip(zip_raw))
        return _json_response(body, status=status)

    # ── Cart ─────────────────────────────────────────────────────────────

    @http.route(
        "/grove/api/v1/cart",
        type="http",
        auth="public",
        website=True,
        methods=["GET"],
        csrf=False,
    )
    def cart_get(self, **_kwargs):
        # request.cart is a lazy proxy injected by website_sale's ir_http override.
        # Resolves to the session's current cart or an empty recordset.
        sale_order = request.cart

        # Cross-company safety: a session cookie originating from another tenant
        # resolves to a sale.order in that tenant's company. Don't render lines
        # from another company's cart — fall through to the empty shape.
        if sale_order and sale_order.company_id != request.website.company_id:
            return _json_response({"lines": [], "amount_total": 0, "currency": None})

        if not sale_order:
            return _json_response({"lines": [], "amount_total": 0, "currency": None})

        lines = []
        for line in sale_order.order_line:
            lines.append(
                {
                    "id": line.id,
                    "product_id": line.product_id.id,
                    "product_name": line.product_id.display_name,
                    "quantity": line.product_uom_qty,
                    "price_unit": line.price_unit,
                    "price_subtotal": line.price_subtotal,
                    "image_url": _image_url("product.product", line.product_id, "image_128"),
                }
            )

        return _json_response(
            {
                "id": sale_order.id,
                "lines": lines,
                "amount_untaxed": sale_order.amount_untaxed,
                "amount_tax": sale_order.amount_tax,
                "amount_total": sale_order.amount_total,
                "currency": {
                    "id": sale_order.currency_id.id,
                    "name": sale_order.currency_id.name,
                },
            }
        )

    @http.route(
        "/grove/api/v1/cart",
        type="http",
        auth="public",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def cart_update(self, **_kwargs):
        try:
            payload = json.loads(request.httprequest.data or "{}")
        except json.JSONDecodeError:
            return _json_response({"error": "Invalid JSON body"}, status=400)

        # Accept either `variant_id` (product.product) or `product_id` (product.template).
        # The frontend currently sends `product_id` from the detail page, so we resolve
        # template → default variant when no explicit variant is given.
        variant_id = payload.get("variant_id")
        template_id = payload.get("product_id")
        quantity = payload.get("quantity", 1)

        if not (variant_id or template_id):
            return _json_response({"error": "Either variant_id or product_id is required"}, status=400)

        try:
            quantity = float(quantity)
            if variant_id is not None:
                variant_id = int(variant_id)
            if template_id is not None:
                template_id = int(template_id)
        except (ValueError, TypeError):
            return _json_response({"error": "Invalid id or quantity"}, status=400)

        # Guard against negative or zero quantities — _cart_add interprets a
        # negative value as a removal, which would let an unauthenticated POST
        # delete arbitrary lines from someone else's session cart. Updates
        # and removals belong in dedicated endpoints, not the add handler.
        if quantity <= 0:
            return _json_response({"error": "quantity must be a positive number"}, status=400)

        current_company = request.website.company_id
        company_domain = [("company_id", "in", [current_company.id, False])]

        if variant_id is not None:
            variant = (
                request.env["product.product"]
                .sudo()
                .with_company(current_company)
                .search(
                    [("id", "=", variant_id), *company_domain],
                    limit=1,
                )
            )
        else:
            template = (
                request.env["product.template"]
                .sudo()
                .with_company(current_company)
                .search(
                    [
                        ("id", "=", template_id),
                        ("website_published", "=", True),
                        *company_domain,
                    ],
                    limit=1,
                )
            )
            variant = template.product_variant_id  # default variant

        if not variant:
            return _json_response({"error": "Product not found"}, status=404)

        # Cross-company safety mirror of cart_get: a session cookie that
        # leaked in from another tenant resolves to a foreign-company cart
        # that we must NOT mutate. Discard it and start a fresh cart scoped
        # to this website's company so the line goes to the right tenant.
        sale_order = request.cart
        if sale_order and sale_order.company_id != request.website.company_id:
            sale_order = request.website._create_cart()
        elif not sale_order:
            sale_order = request.website._create_cart()
        sale_order._cart_add(product_id=variant.id, quantity=quantity)

        return self.cart_get()

    # ── Shipping ─────────────────────────────────────────────────────────

    def _shipping_calendar_override(self):
        """Parsed `grove_headless.shipping_calendar` override, or None.

        The annual shipping calendar (GOL-1172) is admin-editable at runtime
        via this system parameter — a JSON blob deep-merged over the module
        defaults, so nursery ops can restate a zone's ship window once a year
        without a deploy. Malformed JSON is ignored (the feed falls back to
        defaults) rather than 500-ing the storefront.
        """
        return _parse_calendar_override(request.env)

    @http.route(
        "/grove/api/v1/shipping/options",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def shipping_options(self, **kwargs):
        zip_code = kwargs.get("zip", "")
        state = kwargs.get("state", "")
        tier = kwargs.get("tier", "potted")
        is_pickup = (kwargs.get("fulfillment") or "").strip().lower() == "pickup"
        try:
            length_class = int(kwargs.get("length", "20"))
        except ValueError:
            length_class = 20
        today = _date.today()
        # Farm pickup resolves the bareroot window from the FARM's zone, not the
        # shopper's (GOL-1669): a warm-zone buyer collecting at the farm is bound
        # by when we can lift here. A missing/unrecognized customer ZIP no longer
        # blanks the pickup window. Ship (or unspecified) keeps the customer ZIP.
        window_zip = _farm_pickup_zip(request.env, request.website.company_id) if is_pickup else zip_code
        result = serialize_ship_options(ship_options(window_zip, tier, today))
        # Box Engine v2: per_tree_rate = cheapest single-tree shipment in the
        # season's packing mode. Potted (pickup-only) and farm pickup pay no
        # shipping, so no per-tree ship rate is quoted for either.
        mode = packing_mode(today)
        result["packing_mode"] = mode
        quotes_ship = tier == "bareroot" and not is_pickup
        result["per_tree_rate"] = single_tree_rate(state, length_class, mode) if quotes_ship else None
        # GOL-1172: per-USDA-zone fulfillment mode for the three-mode frontend
        # (bareroot-preorder | bareroot-in-window | peat-and-bagged), computed
        # server-side from the same annual calendar the feed serves. Bareroot
        # only — potted is farm pickup, no ship timing.
        if tier == "bareroot":
            calendar = merge_calendar_override(self._shipping_calendar_override())
            # UTC basis (GOL-1309): the fulfillment MODE the shopper is shown
            # here must match the one the checkout gate re-resolves, and the
            # frontend resolves in UTC — see _today_utc. Zone keys off
            # window_zip (farm's ZIP on pickup — GOL-1669), same as the window.
            result["fulfillment"] = resolve_fulfillment(usda_zone_for_zip(window_zip), _today_utc(), calendar)
        else:
            result["fulfillment"] = None
        return _json_response(result)

    @http.route(
        "/grove/api/v1/shipping/rates",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def shipping_rates(self, **_kwargs):
        """Live read-only shipping-rate feed for the storefront estimator (GOL-952).

        Serves the in-memory zone rate table (mirroring ``data/shipping_rates.json``)
        plus the authoritative zone->state green list, so the product-page estimator
        prices against exactly what checkout will charge instead of a bundled copy
        that drifts as the daily rate-checker rewrites the table. Public and cheap:
        no Shippo call in the request path, one cheap DB read (the calendar
        override system parameter). The frontend drops the ``zones`` map into
        ``resolveRateTable()`` and reads ``calendar.resolved[String(usdaZone)]``
        for the shopper's mode/ship_timing verbatim (GOL-1386), resolved
        server-side against ``date.today()`` so it never re-derives the backend
        state machine or disagrees on a timezone boundary day.
        """
        return _json_response(rate_feed(self._shipping_calendar_override(), _date.today()))

    # ── Orders ───────────────────────────────────────────────────────────

    @http.route(
        "/grove/api/v1/orders",
        type="http",
        auth="bearer",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def order_create(self, **_kwargs):
        """Create a draft sale.order from a posted cart payload.

        Auth: requires a valid API key via `Authorization: Bearer <key>` header.
        The Next.js BFF (see @grove/odoo-client) sends this header on every
        request; the key resolves to the Odoo user it was issued for. We use
        `auth="bearer"` (not `auth="user"`) because in Odoo 19 only the
        `bearer` auth method actually parses the Authorization header for an
        API key — `auth="user"` only honours session cookies. This prevents
        unauthenticated POSTs from the public internet creating sale.order
        and res.partner records (and bypassing the BFF / rate limits) against
        any of the three tenant companies. Cart endpoints stay `auth="public"`
        because they rely on website_sale's session-cookie cart proxy
        (`request.cart`), and `order_get` stays `auth="public"` because its
        gate is the per-order portal access_token.

        Body shape:
            {
              "contact": {"name": "...", "email": "...", "phone": "..."},
              "shipping": {"street": "...", "city": "...", "state": "WV",
                           "zip": "...", "country": "US"},
              "billing":  {...} | null,            # null = same as shipping
              "payment_method": "card",            # informational; real payment in later sprint
              "items": [{"variant_id": 2, "quantity": 1}, ...]
            }
        """
        try:
            payload = json.loads(request.httprequest.data or "{}")
        except json.JSONDecodeError:
            return _json_response({"error": "Invalid JSON body"}, status=400)

        order, error = _create_draft_order(request.website, request.env, payload)
        if error is not None:
            return error

        # Ensure a portal access token exists — required to fetch order details
        # later via GET without exposing PII through id-enumeration.
        access_token = order._portal_ensure_token()

        return _json_response(
            {
                "id": order.id,
                "name": order.name,
                "state": order.state,
                "access_token": access_token,
                "amount_untaxed": order.amount_untaxed,
                "amount_tax": order.amount_tax,
                "amount_total": order.amount_total,
                "currency": {
                    "id": order.currency_id.id,
                    "name": order.currency_id.name,
                },
                "line_count": len(order.order_line),
            }
        )

    @http.route(
        "/grove/api/v1/orders/<int:order_id>",
        type="http",
        auth="public",
        website=True,
        methods=["GET"],
        csrf=False,
    )
    def order_get(self, order_id, **kwargs):
        """Return public-safe order details for the confirmation page.

        Requires an `access_token` query param matching the order's token to
        prevent PII leak via incremental id enumeration. The token is returned
        in the order_create response and embedded in the success-page URL.
        """
        access_token = kwargs.get("access_token")
        if not access_token:
            return _json_response({"error": "access_token is required"}, status=403)

        website = request.website
        current_company = website.company_id

        order = (
            request.env["sale.order"]
            .sudo()
            .with_company(current_company)
            .search(
                [
                    ("id", "=", order_id),
                    ("company_id", "=", current_company.id),
                    ("access_token", "=", access_token),
                ],
                limit=1,
            )
        )
        if not order:
            return _json_response({"error": "Order not found"}, status=404)

        lines = [
            {
                "id": line.id,
                "product_name": line.product_id.display_name,
                "quantity": line.product_uom_qty,
                "price_unit": line.price_unit,
                "price_subtotal": line.price_subtotal,
            }
            for line in order.order_line
        ]

        return _json_response(
            {
                "id": order.id,
                "name": order.name,
                "state": order.state,
                "contact": {
                    "name": order.partner_id.name,
                    "email": order.partner_id.email,
                },
                "lines": lines,
                "amount_untaxed": order.amount_untaxed,
                "amount_tax": order.amount_tax,
                "amount_total": order.amount_total,
                "currency": {
                    "id": order.currency_id.id,
                    "name": order.currency_id.name,
                },
            }
        )

    # ── Stripe checkout ──────────────────────────────────────────────────

    @http.route(
        "/grove/api/v1/checkout/session",
        type="http",
        auth="bearer",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def checkout_session(self, **_kwargs):
        """Build a Stripe Checkout Session from a posted cart.

        Body = the /orders shape plus `success_url` + `cancel_url`. We create
        the draft sale.order first so Odoo computes the WV sales tax and the
        tiered shipping charge, then turn its lines into explicit Stripe line
        items (Stripe Tax is OFF — tax rides in as its own line). Charging
        matrix: in-stock lines charge in full; preorder lines charge a flat
        deposit with the balance saved for an off-session capture at ship time.

        Auth mirrors /orders (bearer): creating orders + Stripe sessions must
        not be reachable unauthenticated from the public internet.
        """
        try:
            payload = json.loads(request.httprequest.data or "{}")
        except json.JSONDecodeError:
            return _json_response({"error": "Invalid JSON body"}, status=400)

        # Charge in the account belonging to THIS storefront's tenant so each
        # LLC's revenue lands in its own Stripe account (GOL-1766). The tenant
        # is the website serving the request; a single-account env falls back to
        # the legacy key inside _tenant_secret_key.
        tenant = request.website.grove_tenant_slug()
        secret_key = _tenant_secret_key(tenant)
        if not secret_key:
            # Code ships before keys land (GOL-642): the endpoint is live but
            # inert until Terra applies the Stripe secret key(s) to the droplet
            # env.
            return _json_response({"error": "Checkout is not configured yet"}, status=503)

        success_url = payload.get("success_url")
        cancel_url = payload.get("cancel_url")
        if not success_url or not cancel_url:
            return _json_response({"error": "success_url and cancel_url are required"}, status=400)

        order, error = _create_draft_order(request.website, request.env, payload)
        if error is not None:
            return error

        access_token = order._portal_ensure_token()
        # GOL-1309: re-resolve the destination's shipping-calendar mode and force
        # bareroot lines whose ship wave has not opened onto the deposit path, so
        # a dormant winter order is never charged in full for a tree that cannot
        # ship until spring/fall (mirrors the frontend's pre-checkout resolution).
        calendar_preorder_ids = _calendar_preorder_variant_ids(request.env, order, payload)
        line_items, preorder_ids, charged_cents = _build_stripe_line_items(order, calendar_preorder_ids)
        if not line_items:
            order.unlink()
            return _json_response({"error": "Cart produced no chargeable line items"}, status=400)

        # Stripe substitutes {CHECKOUT_SESSION_ID} at redirect time so the
        # success/cancel pages can look the order up. Preserve any existing query.
        success_url += ("&" if "?" in success_url else "?") + "session_id={CHECKOUT_SESSION_ID}"
        cancel_url += ("&" if "?" in cancel_url else "?") + "session_id={CHECKOUT_SESSION_ID}"

        try:
            session = stripe_gateway.create_checkout_session(
                secret_key,
                line_items=line_items,
                success_url=success_url,
                cancel_url=cancel_url,
                customer_email=order.partner_id.email,
                setup_future_usage=bool(preorder_ids),
                metadata={"order_id": order.id, "order_ref": order.name, "access_token": access_token},
            )
        except stripe_gateway.StripeError as exc:
            # Leave the draft order for staff follow-up; return a clean 502
            # rather than a Werkzeug traceback.
            _logger.error("Stripe checkout session failed for order %s: %s", order.name, exc)
            return _json_response({"error": "Payment provider error creating checkout session"}, status=502)

        order.sudo().write(
            {
                "grove_stripe_session_id": session.get("id"),
                "grove_stripe_payment_intent": session.get("payment_intent") or False,
                "grove_preorder_variant_ids": ",".join(str(i) for i in preorder_ids) or False,
                "grove_checkout_status": "pending",
                # Dollars actually taken today — the base the ship-time settlement
                # subtracts from the (recomputed) order total (GOL-2053).
                "grove_amount_charged_today": round(charged_cents / 100.0, 2),
            }
        )

        # Disclosure for the review page (GOL-2052 constraint 1): a preorder
        # defers shipping + tax to an off-session charge at ship, so we tell the
        # shopper the ESTIMATED shipping/tax and what settles later, plainly. The
        # estimate is the quoted zone rate (kept for display only); the actual
        # amount is recomputed and charged when the box is packed.
        amount_due_today = round(charged_cents / 100.0, 2)
        shipping_deferred = bool(preorder_ids)
        estimated_shipping = round(
            sum(
                ol.price_unit
                for ol in order.order_line
                if ol.product_id and ol.product_id.default_code == SHIPPING_PRODUCT_CODE
            ),
            2,
        )
        # amount_total carries the full goods + shipping + tax; anything not
        # charged today (preorder tree balances, deferred shipping, deferred tax)
        # settles at ship.
        amount_due_at_ship = round(order.amount_total - amount_due_today, 2)

        return _json_response(
            {
                "session_id": session.get("id"),
                "checkout_url": session.get("url"),
                "order_id": order.id,
                "order_ref": order.name,
                "access_token": access_token,
                "has_preorder": bool(preorder_ids),
                "amount_due_today": amount_due_today,
                "amount_total": order.amount_total,
                # GOL-2052: shipping + tax deferral disclosure. When
                # `shipping_deferred` is true the review page must show
                # `estimated_shipping`/`estimated_tax` as ESTIMATES and state the
                # real amount is charged at ship; `amount_due_at_ship` is the
                # deferred remainder (tree balances + shipping + tax).
                "shipping_deferred": shipping_deferred,
                "estimated_shipping": estimated_shipping,
                "estimated_tax": round(order.amount_tax, 2),
                "amount_due_at_ship": amount_due_at_ship,
                "currency": order.currency_id.name,
                # Itemized charged-today breakdown — the SAME array Stripe renders
                # (goods / per-unit deposit / shipping / WV tax), so the review page
                # shows byte-identical math to the card page and the confirmation.
                # `amount_due_today` == sum(unit_amount * quantity) over this list.
                "line_items": [
                    {
                        "name": li["name"],
                        "kind": li.get("kind", "goods"),
                        "unit_amount": round(li["amount_cents"] / 100.0, 2),
                        "quantity": li["quantity"],
                    }
                    for li in line_items
                ],
            }
        )

    @http.route(
        "/grove/api/v1/stripe/webhook",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def stripe_webhook(self, **_kwargs):
        """Receive Stripe Checkout webhooks.

        type="http" (not "json") so Stripe sees real HTTP status codes — a
        "json" route wraps everything in HTTP 200 and would defeat Stripe's
        retry-on-failure. Signature-verified against the raw body, idempotent by
        event id. Handles checkout.session.completed / .expired; on an oversold
        in-stock line it refunds, apologises, and pings ops on Discord.
        """
        raw = request.httprequest.get_data() or b""
        sig = request.httprequest.headers.get("Stripe-Signature", "")
        secrets = _configured_webhook_secrets()
        if not secrets:
            _logger.warning("Stripe webhook rejected: no webhook secret configured")
            return _json_response({"error": "signature verification failed"}, status=400)
        verified, last_error = _verify_stripe_webhook(raw, sig, secrets)
        if not verified:
            _logger.warning("Stripe webhook rejected: %s", last_error)
            return _json_response({"error": "signature verification failed"}, status=400)

        try:
            event = json.loads(raw or b"{}")
        except (json.JSONDecodeError, ValueError):
            return _json_response({"error": "bad json"}, status=400)

        event_id = event.get("id")
        event_type = event.get("type")
        if not event_id:
            return _json_response({"error": "missing event id"}, status=400)

        env = request.env
        Event = env["grove.stripe.event"].sudo()
        # Fast-path dedupe covers the common retry; the UNIQUE constraint below
        # is the real guarantee against a race between concurrent deliveries.
        if Event.search_count([("event_id", "=", event_id)]):
            return _json_response({"ok": True, "duplicate": True})

        # Insert the id under the unique constraint inside a savepoint so a
        # concurrent duplicate collides here (caught → 200) instead of poisoning
        # the transaction.
        try:
            with env.cr.savepoint():
                ledger = Event.create({"event_id": event_id, "event_type": event_type})
                # Force the INSERT now so a unique-constraint collision raises
                # inside this savepoint (where we catch it) rather than later at
                # commit-flush, which would escape the guard.
                env.flush_all()
        except psycopg2.IntegrityError:
            return _json_response({"ok": True, "duplicate": True})

        session = (event.get("data") or {}).get("object") or {}
        try:
            if event_type == "checkout.session.completed":
                result = _handle_session_completed(env, session)
            elif event_type == "checkout.session.expired":
                result = _handle_session_expired(env, session)
            else:
                result = "ignored"
        except Exception:  # noqa: BLE001
            # Roll the whole transaction back — including the ledger insert — so
            # Stripe's retry can reprocess this event cleanly rather than seeing
            # it recorded-but-unhandled.
            _logger.exception("Stripe webhook %s (%s) handler failed", event_id, event_type)
            env.cr.rollback()
            return _json_response({"error": "handler error"}, status=500)

        ledger_vals = {"notes": result}
        # Back-reference the reconciled order on the ledger row so the event
        # trail is queryable order↔event (GOL-711 flag a: order_id was NULL on
        # every row). Cheap re-resolve — webhook volume is tiny.
        order = _find_order_for_session(env, session)
        if order:
            ledger_vals["order_id"] = order.id
        ledger.write(ledger_vals)
        return _json_response({"ok": True, "result": result})

    # ── Newsletter ───────────────────────────────────────────────────────

    @http.route(
        "/grove/api/v1/newsletter/subscribe",
        type="http",
        auth="bearer",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def newsletter_subscribe(self, **_kwargs):
        """Upsert a newsletter opt-in contact and tag it for order attribution.

        Auth: `auth="bearer"` mirrors `order_create` — anyone with a valid API
        key (the Next.js BFF) may call this, but the public internet cannot
        create/tag res.partner records across tenants. The BFF calls this
        best-effort after a successful newsletter capture (grove-sites
        @grove/newsletter); a failure here never blocks the subscription.

        Body shape:
            {
              "email": "a@b.com",          # required
              "name": "Ada",              # optional
              "brand": "goldberry",       # tenant/brand slug
              "interests": ["fruit", ...], # free-form interest slugs
              "source": "homepage_footer", # capture location
              "consent": true,            # required truthy — opt-in proof
              "attribution": {"utm_source": "...", "utm_medium": "...", ...}
            }
        → 200 { partner_id, email, tags: [...], created: bool }

        Behaviour:
          - Resolves the tenant company from X-Grove-Tenant (website routing).
          - Upserts res.partner by email within that company (idempotent). An
            existing partner is reused as-is — we never overwrite name/email
            from a bearer POST (same safety stance as order_create).
          - Tags the partner with `newsletter`, `brand:<brand>`, and
            `interest:<x>` res.partner.category records (additive) so a later
            order carries the capture context for attribution.
          - Records `source` + `attribution` (utm_*) as a chatter note on the
            partner — an audit trail without a schema change.
        """
        try:
            payload = json.loads(request.httprequest.data or "{}")
        except json.JSONDecodeError:
            return _json_response({"error": "Invalid JSON body"}, status=400)
        if not isinstance(payload, dict):
            return _json_response({"error": "Invalid JSON body"}, status=400)

        email = payload.get("email")
        if not email:
            return _json_response({"error": "email is required"}, status=400)
        if not isinstance(email, str) or not EMAIL_RE.fullmatch(email):
            return _json_response({"error": "email is not a valid email address"}, status=400)

        # Consent is the opt-in proof — a subscribe without it must not tag the
        # contact as a newsletter subscriber. Treat any falsy/absent value as
        # missing consent rather than silently subscribing.
        if not payload.get("consent"):
            return _json_response({"error": "consent is required to subscribe"}, status=400)

        err = _check_lengths(
            payload,
            {"email": MAX_EMAIL, "name": MAX_NAME, "brand": MAX_BRAND, "source": MAX_SOURCE},
        )
        if err:
            return _json_response({"error": err}, status=400)

        interests = payload.get("interests") or []
        if not isinstance(interests, list):
            return _json_response({"error": "interests must be a list"}, status=400)
        if len(interests) > MAX_INTERESTS:
            return _json_response({"error": f"interests exceeds {MAX_INTERESTS} entries"}, status=400)
        for interest in interests:
            if not isinstance(interest, str):
                return _json_response({"error": "each interest must be a string"}, status=400)
            if len(interest) > MAX_INTEREST:
                return _json_response({"error": f"interest exceeds {MAX_INTEREST} characters"}, status=400)

        current_company = request.website.company_id
        Partner = request.env["res.partner"].sudo().with_company(current_company)

        partner = Partner.search(
            [
                ("email", "=", email),
                ("company_id", "in", [current_company.id, False]),
            ],
            limit=1,
        )
        created = not partner
        if not partner:
            name = payload.get("name") or email
            partner = Partner.create(
                {
                    "name": name,
                    "email": email,
                    "company_id": current_company.id,
                }
            )

        tag_names = newsletter_tag_names(payload.get("brand"), interests)
        source = payload.get("source")
        if isinstance(source, str) and source.strip():
            tag_names.append(f"source:{source.strip().lower()}")
        category_ids = _get_or_create_partner_categories(request.env, tag_names)
        if category_ids:
            partner.write({"category_id": [(4, cid) for cid in category_ids]})

        _log_newsletter_attribution(partner, source, payload.get("attribution"))

        return _json_response(
            {
                "partner_id": partner.id,
                "email": partner.email,
                "tags": tag_names,
                "created": created,
            }
        )

    # ── Shipping webhook ─────────────────────────────────────────────────

    @http.route(
        "/grove/api/v1/shipping/webhook",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def shipping_webhook(self, **kwargs):
        """Handle Shippo tracking-status webhooks.

        Uses type="http" (not "json") so that Shippo receives the correct HTTP
        status codes: errors return 4xx, success returns 200. With type="json"
        (JSON-RPC dispatcher), all responses — including exceptions — are wrapped
        in HTTP 200, preventing Shippo from detecting failures and retrying.

        Auth: token presented either as the `token` URL query parameter (the
        only mechanism Shippo supports — register the webhook as
        `.../shipping/webhook?token=<GROVE_SHIPPO_WEBHOOK_TOKEN>`) or as an
        `X-Grove-Webhook-Token` request header (non-Shippo callers).
        The token is compared with `hmac.compare_digest` to prevent timing-oracle
        attacks. GROVE_SHIPPO_WEBHOOK_TOKEN must be set in the server environment.
        """
        try:
            payload = json.loads(request.httprequest.get_data() or b"{}")
        except (json.JSONDecodeError, ValueError):
            return _json_response({"error": "bad json"}, status=400)

        # Shippo CANNOT send custom request headers. Its only webhook security
        # options are a self-generated token in the URL query string, an inbound
        # IP allowlist, or HMAC (provisioned by their solutions team on request,
        # up to 10 business days). The header-only check below was therefore
        # unsatisfiable in production and 403'd every real delivery — verified
        # live against prod 2026-08-27. Read ?token= first (the Shippo path) and
        # keep the header for any non-Shippo caller already using it.
        expected = os.environ.get("GROVE_SHIPPO_WEBHOOK_TOKEN", "")
        token = request.httprequest.args.get("token", "") or request.httprequest.headers.get(
            "X-Grove-Webhook-Token", ""
        )
        if not expected or not hmac.compare_digest(token, expected):
            return _json_response({"error": "forbidden"}, status=403)

        data = payload.get("data") or {}
        tracking = data.get("tracking_number")
        status = (data.get("tracking_status") or {}).get("status")
        if not (tracking and status):
            return _json_response({"ok": True, "matched": 0})
        if not is_valid_tracking(tracking):
            return _json_response({"ok": True, "matched": 0})
        orders = request.env["sale.order"].sudo().search([("grove_tracking_numbers", "like", tracking)])
        new_status = status.lower()
        for order in orders:
            _apply_delivery_status(request.env, order, new_status, tracking)
        return _json_response({"ok": True, "matched": len(orders)})


def _get_or_create_partner_categories(env, names):
    """Resolve tag names to res.partner.category ids, creating any missing ones.

    res.partner.category is a global (company-less) taxonomy, so a single tag
    is shared across tenants — the partner it hangs off is still company-scoped.
    Fetches all existing matches in one query, then creates only the gaps.
    """
    unique_names = list(dict.fromkeys(n for n in names if n))
    if not unique_names:
        return []
    Category = env["res.partner.category"].sudo()
    existing = Category.search([("name", "in", unique_names)])
    by_name = {cat.name: cat.id for cat in existing}
    for name in unique_names:
        if name not in by_name:
            by_name[name] = Category.create({"name": name}).id
    return [by_name[name] for name in unique_names]


def _log_newsletter_attribution(partner, source, attribution):
    """Record newsletter capture source + utm attribution as a chatter note.

    A non-destructive audit trail that needs no schema change. Only posts when
    there is something to record, and swallows errors so an attribution log
    failure never fails an otherwise-successful opt-in (best-effort by design).
    """
    if not source and not attribution:
        return
    lines = ["<b>Newsletter opt-in</b>"]
    if source:
        lines.append(f"Source: {source}")
    if isinstance(attribution, dict):
        for key in sorted(attribution):
            value = attribution[key]
            if isinstance(value, (str, int, float)) and str(value).strip():
                lines.append(f"{key}: {value}")
    try:
        partner.sudo().message_post(body="<br/>".join(lines))
    except Exception:  # noqa: BLE001 — audit log is best-effort, never fatal
        _logger.warning("newsletter attribution log failed for partner %s", partner.id, exc_info=True)


def _partner_vals_from_payload(env, contact, address):
    """Build res.partner write/create vals from contact + address dicts."""
    vals = {
        "name": contact.get("name"),
        "email": contact.get("email"),
        "phone": contact.get("phone") or False,
    }
    if not address:
        return vals

    vals.update(
        {
            "street": address.get("street") or False,
            "street2": address.get("street2") or False,
            "city": address.get("city") or False,
            "zip": address.get("zip") or False,
        }
    )

    country_code = (address.get("country") or "").upper()
    if country_code:
        country = env["res.country"].sudo().search([("code", "=", country_code)], limit=1)
        if country:
            vals["country_id"] = country.id
            raw_state = (address.get("state") or "").strip()
            if raw_state:
                # Resolve the ship-to state robustly: a US state given as a full
                # name ("Ohio") or odd case must still bind to res.country.state,
                # or partner.state_id stays empty and downstream billing (shipping
                # zone, WV-nexus tax) + label buying all mis-fire. Canonicalize US
                # input to its 2-letter code; otherwise fall back to a
                # case-insensitive exact match on code or name.
                canonical = canonical_state_code(raw_state) if country_code == "US" else None
                if canonical:
                    domain = [("country_id", "=", country.id), ("code", "=", canonical)]
                else:
                    domain = [
                        ("country_id", "=", country.id),
                        "|",
                        ("code", "=ilike", raw_state),
                        ("name", "=ilike", raw_state),
                    ]
                state = env["res.country.state"].sudo().search(domain, limit=1)
                if state:
                    vals["state_id"] = state.id
    return vals


SHIPPING_PRODUCT_CODE = "GROVE-SHIP"


def _farm_pickup_zip(env, company):
    """Origin ZIP that keys the bareroot mailing window for FARM-PICKUP orders
    (GOL-1669).

    Farm pickup lifts trees on the farm, so the USDA hardiness zone that decides
    when a bareroot line can be filled must come from the FARM's ZIP, never the
    customer's — a warm-zone (say zone 8) buyer collecting at our zone-6 farm is
    bound by when WE can lift, not by their home zone's later window.

    Sourced from the pickup warehouse address (``stock.warehouse.partner_id.zip``)
    so a second farm binds its own origin without a code change; falls back to the
    company partner ZIP and finally the admin-editable ``grove_headless.farm_pickup_zip``
    system parameter (seeded to 26651 / Summersville WV in module data). Returns a
    stripped ZIP string, or None if nothing is configured — callers stay
    conservative on None (``ship_options`` treats an unknown ZIP as not-shippable).
    """
    warehouse = env["stock.warehouse"].sudo()
    wh = warehouse.search([("company_id", "=", company.id)], limit=1) if company else warehouse.browse()
    for candidate in (
        wh.partner_id.zip if wh and wh.partner_id else None,
        company.partner_id.zip if company and company.partner_id else None,
        env["ir.config_parameter"].sudo().get_param("grove_headless.farm_pickup_zip"),
    ):
        candidate = (candidate or "").strip()
        if candidate:
            return candidate
    return None


def _get_shipping_product(env, company):
    """Find (or lazily create) the service product the shipping charge rides on.

    Scoped per-company so each tenant's order carries its own shipping SKU.
    Only ever runs once the zone table is configured — until then
    `_apply_shipping_line` returns before reaching here.
    """
    Product = env["product.product"].sudo().with_company(company)
    product = Product.search(
        [
            ("default_code", "=", SHIPPING_PRODUCT_CODE),
            ("company_id", "in", [company.id, False]),
        ],
        limit=1,
    )
    if not product:
        product = Product.create(
            {
                "name": "Shipping",
                "default_code": SHIPPING_PRODUCT_CODE,
                "type": "service",
                "list_price": 0.0,
                "sale_ok": True,
                "purchase_ok": False,
                "company_id": company.id,
            }
        )
    return product


def _apply_shipping_line(env, order, shipping, company):
    """Add a shipping charge line to `order` from the tiered zone table.

    Returns the applied shipping charge (float) or ``None`` when no line was
    added. Fail-safe: returns None without adding a line when no rate is
    configured for the destination or any line's tier — never a guessed
    charge. The caller enforces the no-$0-shipping circuit breaker (GOL-1036
    defect 2) off this return value. The ship-to state is canonicalized to its
    2-letter code first, so a green-list state supplied as a full name ("Ohio")
    or in odd case is priced instead of silently dropped.
    """
    state = canonical_state_code((shipping or {}).get("state"))
    if not state:
        return None
    items = [
        (
            line.product_id.grove_effective_shipping_tier
            or line.product_id.product_tmpl_id.grove_shipping_tier
            or "potted",
            int(line.product_id.product_tmpl_id.grove_tree_length or "20"),
            line.product_uom_qty,
        )
        for line in order.order_line
        if not line.display_type and line.product_id
    ]
    if not items:
        return None
    charge = compute_order_shipping(state, items, packing_mode(_date.today()))
    if charge is None:
        # A destination outside the 21-state green list legitimately gets no
        # shipping line. But a *green* state that still can't be priced means a
        # rate-table gap is silently under-billing a customer we do ship to —
        # surface that rather than swallow it. The caller's circuit breaker
        # (GOL-1036 defect 2) turns this None into a hard error for a shipped
        # order so it can never reach Stripe with $0 shipping.
        if zone_for_state(state) is not None:
            _logger.warning(
                "grove_headless: shipping line dropped for order %s to green-list state %s "
                "(no rate for one or more line tiers) — customer under-billed for shipping",
                order.name,
                state,
            )
        return None

    product = _get_shipping_product(env, company)
    env["sale.order.line"].sudo().create(
        {
            "order_id": order.id,
            "product_id": product.id,
            "name": f"Shipping ({state})",
            "product_uom_qty": 1.0,
            "price_unit": charge,
        }
    )
    order.invalidate_recordset(["amount_untaxed", "amount_tax", "amount_total"])
    return charge


def _apply_destination_tax(env, order, shipping):
    """Strip the WV sales tax from every line when the order ships out of state.

    The product default (hooks.setup_wv_sales_tax) puts the "WV Sales Tax 7%"
    group on every line, which is only lawful for a WV-destination shipment —
    Grove's sole sales-tax nexus. For any other ship-to state (e.g. Ohio) the WV
    tax is removed so the customer is not wrongly charged WV tax. Called after
    the shipping line is added so that line is de-taxed too when out of state.

    Ship-to state is canonicalized identically to the shipping path. If it can't
    be determined we conservatively leave the default WV tax in place rather than
    guess a zero-tax order.
    """
    dest = canonical_state_code((shipping or {}).get("state"))
    if dest is None or dest == WV_NEXUS_STATE:
        return
    changed = False
    for line in order.order_line:
        if line.display_type or not line.product_id:
            continue
        wv_taxes = line.tax_ids.filtered(lambda t: t.name in WV_TAX_NAMES)
        if wv_taxes:
            line.tax_ids = [(3, tax.id) for tax in wv_taxes]
            changed = True
    if changed:
        order.invalidate_recordset(["amount_untaxed", "amount_tax", "amount_total"])


def _format_payment_note(payment_method):
    """Render the chosen payment method as a human-readable order note.

    Real payment integration lands in a later sprint; for now we just record
    what the customer selected so staff can follow up.
    """
    if not payment_method:
        return False
    return f"Payment method requested: {payment_method}"


def _create_draft_order(website, env, payload):
    """Build a draft sale.order from a posted cart payload.

    Shared by POST /orders and POST /checkout/session. Returns
    (order, None) on success or (None, error_response) on any validation
    failure — the caller returns the error response as-is. Never leaves a
    partial order persisted: every variant is validated before lines are
    written, and the order is unlinked on a late failure.
    """
    contact = payload.get("contact") or {}
    items = payload.get("items") or []

    if not contact.get("email") or not contact.get("name"):
        return None, _json_response({"error": "contact.name and contact.email are required"}, status=400)
    if not isinstance(items, list) or not items:
        return None, _json_response({"error": "items must be a non-empty list"}, status=400)

    # Defense in depth: re-validate email format and field lengths even though
    # the BFF already does. Anyone with a valid API key can POST here directly,
    # so we cannot trust the caller. `isinstance` before `fullmatch` because a
    # non-string email (e.g. int from a misbehaving client) would raise → 500.
    if not isinstance(contact["email"], str) or not EMAIL_RE.fullmatch(contact["email"]):
        return None, _json_response({"error": "contact.email is not a valid email address"}, status=400)

    contact_limits = {"name": MAX_NAME, "email": MAX_EMAIL, "phone": MAX_PHONE}
    err = _check_lengths(contact, contact_limits)
    if err:
        return None, _json_response({"error": f"contact.{err}"}, status=400)

    address_limits = {
        "street": MAX_STREET,
        "street2": MAX_STREET,
        "city": MAX_CITY,
        "state": MAX_STATE,
        "zip": MAX_ZIP,
        "country": MAX_COUNTRY,
    }
    shipping = payload.get("shipping") or {}
    if shipping:
        err = _check_lengths(shipping, address_limits)
        if err:
            return None, _json_response({"error": f"shipping.{err}"}, status=400)
    billing = payload.get("billing") or {}
    if billing:
        err = _check_lengths(billing, address_limits)
        if err:
            return None, _json_response({"error": f"billing.{err}"}, status=400)

    current_company = website.company_id

    # Resolve partner: find an existing partner scoped to this company by email
    # so we don't read/write across tenants. We deliberately do NOT overwrite an
    # existing partner's attributes from a public POST — that would let anyone
    # with a customer's email mutate their record. Reuse as-is, or create fresh.
    Partner = env["res.partner"].sudo().with_company(current_company)
    existing_partner = Partner.search(
        [
            ("email", "=", contact["email"]),
            ("company_id", "in", [current_company.id, False]),
        ],
        limit=1,
    )
    partner_vals = _partner_vals_from_payload(env, contact, payload.get("shipping"))
    if existing_partner:
        partner = existing_partner
    else:
        partner = Partner.create({**partner_vals, "company_id": current_company.id})

    # Resolve billing partner: if a billing address is explicitly provided,
    # always create a child invoice contact. Otherwise reuse main partner.
    billing_partner = partner
    if payload.get("billing"):
        billing_vals = _partner_vals_from_payload(env, contact, payload["billing"])
        billing_partner = Partner.create(
            {**billing_vals, "parent_id": partner.id, "type": "invoice", "company_id": current_company.id}
        )

    # Pick the "Online" sales team if it exists for this company.
    team = (
        env["crm.team"]
        .sudo()
        .search(
            [("name", "=", "Online"), ("company_id", "=", current_company.id)],
            limit=1,
        )
    )

    order_vals = {
        "partner_id": partner.id,
        "partner_invoice_id": billing_partner.id,
        "partner_shipping_id": partner.id,
        "company_id": current_company.id,
        "website_id": website.id,
        "note": _format_payment_note(payload.get("payment_method")),
    }
    if team:
        order_vals["team_id"] = team.id

    SaleOrder = env["sale.order"].sudo().with_company(current_company)
    order = SaleOrder.create(order_vals)

    # Build order lines. Validate every variant up front so partial orders never
    # get persisted. Fetch all referenced variants in a single query rather than
    # one search per item — orders with many lines were doing N round trips.
    parsed_items: list[tuple[int, float]] = []
    for raw_item in items:
        try:
            parsed_items.append((int(raw_item.get("variant_id")), float(raw_item.get("quantity") or 1)))
        except (TypeError, ValueError):
            order.unlink()
            return None, _json_response(
                {"error": "Each item needs numeric variant_id and quantity"},
                status=400,
            )

    if any(qty <= 0 for _, qty in parsed_items):
        order.unlink()
        return None, _json_response({"error": "Each item quantity must be positive"}, status=400)

    wanted_ids = {variant_id for variant_id, _ in parsed_items}
    variants = (
        env["product.product"]
        .sudo()
        .with_company(current_company)
        .search(
            [("id", "in", list(wanted_ids)), ("company_id", "in", [current_company.id, False])],
        )
    )
    found_ids = set(variants.ids)
    missing = wanted_ids - found_ids
    if missing:
        order.unlink()
        return None, _json_response(
            {"error": f"Product variant(s) not found: {sorted(missing)}"},
            status=404,
        )

    line_vals = [
        {
            "order_id": order.id,
            "product_id": variant_id,
            "product_uom_qty": quantity,
        }
        for variant_id, quantity in parsed_items
    ]

    env["sale.order.line"].sudo().create(line_vals)
    order.invalidate_recordset(["amount_untaxed", "amount_tax", "amount_total"])

    # ── Ship-to gate (GOL-1036 defects 1 & 2) ────────────────────────────────
    # A cart with a ship-to state must clear three server-side gates before it
    # can reach payment, or a compliance/revenue defect leaks to Stripe:
    #   (1) state gate — the destination must be on the 21-state green list.
    #       Everything else (FL, and every living-tree-cert / quarantine state)
    #       is rejected here, not just discouraged by product-page copy.
    #   (2) potted gate — potted trees are farm pickup only (Box Engine v2).
    #   (3) $0-shipping circuit breaker — a shippable order MUST end up with a
    #       positive shipping line; if shipping can't be resolved we fail loudly
    #       rather than let a shipped order settle with silent $0 shipping.
    # Fulfillment is explicit (GOL-1057): "pickup" is the ONLY legitimate
    # $0-shipping case — potted-and-pickup carts skip the ship-to gate and add no
    # shipping line. "ship" asserts a shippable order, so a missing ship-to state
    # is a hard error here, not a silent fall-through to $0-shipping pickup (the
    # collision the no-$0-ship breaker exists to catch). Absent `fulfillment`
    # keeps the pre-1057 inference (a ship-to state ⇒ ship) for back-compat with
    # callers that don't send the field yet.
    # Reject any explicit `fulfillment` that isn't exactly ship/pickup (GOL-1303).
    # An unrecognized value (typo, stale client, hand-rolled bearer-API caller)
    # would otherwise fall through to the pre-1057 inference below, which — with a
    # missing state — silently skips the green-list gate, the potted gate and the
    # $0-shipping breaker. Mirror the BFF validator: fail loudly at 400. Absent
    # `fulfillment` (None) still keeps back-compat inference.
    fulfillment = (payload.get("fulfillment") or "").strip().lower() or None
    if fulfillment is not None and fulfillment not in ("ship", "pickup"):
        order.unlink()
        return None, _json_response(
            {"error": "Invalid fulfillment — choose shipping or farm pickup."},
            status=400,
        )
    ship_state = (shipping or {}).get("state") if shipping else None
    is_pickup = fulfillment == "pickup"
    if fulfillment == "ship" and not ship_state:
        order.unlink()
        return None, _json_response(
            {"error": "Choose a ship-to state, or select farm pickup."},
            status=400,
        )
    is_ship_to = not is_pickup and bool(ship_state)
    if is_ship_to:
        dest = canonical_state_code(ship_state)
        if dest is None or zone_for_state(dest) is None:
            # (1) Unsupported / non-green-list destination — reject at the source.
            order.unlink()
            return None, _json_response(
                {
                    "error": (
                        f"We can't ship live trees to {ship_state}. Shipping is limited to "
                        "our 21-state region for plant-health compliance — choose a supported "
                        "ship-to state or farm pickup."
                    )
                },
                status=400,
            )
        ship_items = [
            (
                line.product_id.grove_effective_shipping_tier
                or line.product_id.product_tmpl_id.grove_shipping_tier
                or "potted",
                int(line.product_id.product_tmpl_id.grove_tree_length or "20"),
                line.product_uom_qty,
            )
            for line in order.order_line
            if not line.display_type and line.product_id and line.product_id.product_tmpl_id.type != "service"
        ]
        # (2) Potted (or otherwise non-shippable) lines block the ship-to order.
        reason = unshippable_reason(ship_items)
        if reason:
            order.unlink()
            return None, _json_response({"error": reason}, status=400)

    # Apply the per-box 21-state shipping charge (Box Engine v2). Rates load
    # from data/shipping_rates.json (models/shipping_zones.py) and are
    # maintained by the daily rate-checker. Fail-safe: no rate → no line added.
    # Farm pickup never gets a shipping line even if the buyer left an address on
    # the form — pickup is the one legitimate $0-shipping fulfillment (GOL-1057).
    shipping_charge = None if is_pickup else _apply_shipping_line(env, order, shipping, current_company)

    # (3) No-$0-shipping circuit breaker: a ship-to order with shippable goods
    # that produced no positive shipping line is a rate-table gap — never let it
    # reach Stripe under-billed. (unshippable_reason above already cleared potted
    # carts, so reaching here with shippable items and no charge is a real gap.)
    if is_ship_to:
        has_shippable = any(qty > 0 for _tier, _length, qty in ship_items)
        if has_shippable and (shipping_charge is None or shipping_charge <= 0):
            order.unlink()
            return None, _json_response(
                {
                    "error": (
                        "We couldn't calculate shipping for this order right now. "
                        "No payment was taken — please try again shortly or contact us."
                    )
                },
                status=409,
            )

    # WV sales tax is destination-based for SHIPPED orders (GOL-1021): keep it
    # only for WV-bound orders, strip it from every line (incl. shipping) for any
    # other ship-to state. Runs after the shipping line so it is de-taxed too when
    # out of state. Farm pickup always transfers at the WV farm, so WV tax applies
    # regardless of any (possibly stale out-of-state) address the buyer left in the
    # payload before toggling to pickup — skip destination de-taxing entirely so
    # the collected tax matches the pickup UI's "West Virginia sales tax applies"
    # promise (GOL-1303).
    if not is_pickup:
        _apply_destination_tax(env, order, shipping)

    # Persist the resolved fulfilment intent (GOL-1933) for the post-purchase
    # chain. A non-ship, non-pickup order (absent `fulfillment` with no ship-to
    # state → the pre-1057 $0-shipping local case) carries no shipping line and
    # is never labelled, so it collapses to "pickup" for alerting/label-gating.
    order.grove_fulfillment = "ship" if is_ship_to else "pickup"

    return order, None


# ── Stripe checkout helpers ──────────────────────────────────────────────────


def _bareroot_tier(product) -> bool:
    """Is this product a shippable bareroot tier (the tier the ship calendar
    governs)? Mirrors the tier resolution used by the ship-to gate and the
    shipping-charge builder: per-variant effective tier first, then the template
    tier, defaulting to potted (non-shippable)."""
    tier = product.grove_effective_shipping_tier or product.product_tmpl_id.grove_shipping_tier or "potted"
    return tier == "bareroot"


def _calendar_preorder_variant_ids(env, order, payload, today=None):
    """Variant ids whose bareroot ship wave has not opened yet — force them to
    the preorder deposit path regardless of stock (GOL-1309).

    The checkout path had zero shipping-calendar awareness: an in-stock bareroot
    tree ordered in the shopper's dormant winter (its ship wave months away) was
    charged in FULL for immediate fulfillment, even though it physically cannot
    leave the nursery until spring/fall. This re-resolves the destination USDA
    zone + today to exactly one mode via the rev-2 calendar
    (shipping_calendar.resolve_fulfillment) and returns the bareroot variants to
    reprice when that mode is ``bareroot-preorder``: those units become a flat
    per-unit deposit now, with the balance captured off-session at ship time
    (the same deposit machinery short-stock preorders already use).

    Only ``bareroot-preorder`` changes charging. ``bareroot-in-window`` and
    ``peat-and-bagged`` both ship now, so they keep stock-based charging.

    Scope / conservative fallbacks:
      * SHIP orders only — farm pickup transfers at the WV farm, off the ship
        calendar, so it is never calendar-repriced.
      * An unresolvable destination zone (zip absent from the ~15k-row PHZM
        matrix, or a zone the calendar does not configure → mode None) is left
        on stock-based charging: we neither block the order nor reseason it off
        a zone we cannot place. The frontend already surfaced the mode
        pre-checkout; this backend gate is defense-in-depth against a stale or
        hand-rolled client, not the primary UX.
      * The rev-2 model has no hard "frozen / cannot ship" mode for bareroot —
        every dormant date is a chargeable preorder, so there is no reject path
        here (unlike the older ship_options freeze model). See the PR notes.

    Date basis is UTC (``_today_utc``) to match the frontend and the
    /shipping/options preview; ``today`` is injectable for tests.
    """
    fulfillment = (payload.get("fulfillment") or "").strip().lower() or None
    shipping = payload.get("shipping") or {}
    ship_state = shipping.get("state")
    is_pickup = fulfillment == "pickup"
    is_ship_to = not is_pickup and bool(ship_state)
    if not is_ship_to:
        return frozenset()
    zone = usda_zone_for_zip(shipping.get("zip"))
    if zone is None:
        return frozenset()
    calendar = merge_calendar_override(_parse_calendar_override(env))
    mode = resolve_fulfillment(zone, today or _today_utc(), calendar).get("mode")
    if mode != MODE_PREORDER:
        return frozenset()
    return frozenset(
        line.product_id.id
        for line in order.order_line
        if not line.display_type and line.product_id and _bareroot_tier(line.product_id)
    )


def _build_stripe_line_items(order, calendar_preorder_ids=frozenset()):
    """Turn a draft order's lines into Stripe Checkout line items.

    Returns (line_items, preorder_variant_ids, charged_cents). Applies the
    charging matrix per product line (in-stock units = full price; short-stock
    units = a per-unit flat deposit).

    GOL-2052 (CEO directive 2026-09-03): when an order contains ANY preorder
    unit, ONLY the per-unit deposit(s) — and any in-stock goods that bill in
    full — are charged today; SHIPPING and TAX are deferred and collected
    off-session at ship time, at *actual* cost (see ``_settle_at_ship``). A
    stale quoted rate can then never be the charge, and tax is recomputed on the
    settled total (WV tax applies to the shipping line, whose real amount is not
    known until the box is packed). A fully-in-stock / farm-pickup order has no
    preorder unit, ships now, and is unchanged: shipping + WV tax ride the
    today-charge as one explicit line each (Stripe Tax OFF).

    ``calendar_preorder_ids`` (GOL-1309): variant ids whose bareroot ship wave
    is not yet open per the shipping calendar (see
    _calendar_preorder_variant_ids). Those lines charge entirely as deposits
    regardless of on-hand stock — an in-stock dormant tree cannot ship until its
    wave, so it is a preorder even though the shelf shows it available. Modeled
    by treating free stock as zero for the line so line_charge routes every unit
    to the deposit side.
    """
    line_items = []
    preorder_variant_ids = []
    tax_today = 0.0
    # Shipping is captured here and only committed to the today-charge AFTER the
    # loop, once we know whether the cart contains a preorder — a preorder defers
    # shipping (and all tax) to the ship-time settlement (GOL-2052).
    shipping_item = None
    shipping_tax = 0.0
    # Calendar-window preorders (GOL-1666) apply to bareroot regardless of
    # fulfillment: a bareroot line that can't be filled now charges the flat
    # deposit even when in stock, matching the product page. The zone that keys
    # that window depends on WHERE the trees are handed over — a shipped order
    # (carries a GROVE-SHIP line) resolves from the DESTINATION ZIP; a farm-pickup
    # order (no shipping line) resolves from the FARM's own ZIP (GOL-1669), since
    # a warm-zone buyer collecting here is bound by when we can lift on the farm,
    # not by their home zone's later window.
    today = _date.today()
    is_ship_order = any(
        ol.product_id and ol.product_id.default_code == SHIPPING_PRODUCT_CODE for ol in order.order_line
    )
    dest_partner = order.partner_shipping_id or order.partner_id
    dest_zip = dest_partner.zip if dest_partner else None
    farm_zip = _farm_pickup_zip(order.env, order.company_id)
    for line in order.order_line:
        if line.display_type or not line.product_id:
            continue
        product = line.product_id
        name = product.display_name
        if product.default_code == SHIPPING_PRODUCT_CODE:
            amount = stripe_gateway.to_cents(line.price_unit)
            if amount <= 0:
                continue
            # Held, not appended: whether this rides today or defers to ship is
            # decided after the loop from has_preorder (GOL-2052).
            shipping_item = {"name": name, "kind": "shipping", "amount_cents": amount, "quantity": 1}
            shipping_tax = line.price_tax
            continue
        # free_qty (on-hand minus reserved), not qty_available: a unit another
        # order already reserved is not sellable now and must fall to preorder
        # (GOL-1036 defect 4), or the same tree is billed to two customers.
        # GOL-1309: a variant whose ship wave is not yet open is a preorder even
        # if in stock — treat its free stock as zero so every unit deposits.
        ordered_qty = line.product_uom_qty
        # Two independent deposit-forcing signals; either one moves the line to
        # the deposit path (deposit is the safe direction — never a full charge
        # for a tree that cannot ship):
        #   * calendar_preorder_ids (GOL-1309): destination-zone calendar MODE is
        #     bareroot-preorder (rev-2 resolver, UTC basis) — free stock counts 0.
        #   * ships_now (GOL-1666 §2 / GOL-1669): the wave window from
        #     ship_options, keyed off the destination ZIP for shipped orders and
        #     the FARM's ZIP for pickup.
        free_qty = 0 if product.id in calendar_preorder_ids else product.free_qty
        # Only bareroot honors the mailing-window calendar; potted is pickup-only
        # and its sold-out handling is the GOL-1666 §2 bareroot steer, not here.
        tier = product.grove_effective_shipping_tier or product.product_tmpl_id.grove_shipping_tier or "potted"
        line_ships_now = True
        if tier == "bareroot":
            # Ship orders resolve the window from the customer's zone; farm-pickup
            # orders from the farm's own zone (GOL-1669).
            window_zip = dest_zip if is_ship_order else farm_zip
            line_ships_now = ship_options(window_zip, tier, today).get("ships_now", True)
        for amount, qty, is_preorder in stripe_gateway.line_charge(
            line.price_unit, ordered_qty, free_qty, ships_now=line_ships_now
        ):
            if is_preorder:
                preorder_variant_ids.append(product.id)
                line_items.append(
                    {"name": f"Deposit — {name}", "kind": "deposit", "amount_cents": amount, "quantity": qty}
                )
            else:
                line_items.append({"name": name, "kind": "goods", "amount_cents": amount, "quantity": qty})
                # Prorate the line's tax to the units billed today.
                tax_today += line.price_tax * (qty / ordered_qty) if ordered_qty else 0.0
    # GOL-2052: a cart with any preorder unit collects ONLY deposits (+ any
    # in-stock goods) today; its shipping and ALL tax are settled off-session at
    # ship on actual cost, so neither the quoted shipping line nor a tax line is
    # charged now. A non-preorder cart ships now and keeps the prior behaviour:
    # shipping + WV tax ride today's charge.
    has_preorder = bool(preorder_variant_ids)
    if not has_preorder:
        if shipping_item is not None:
            line_items.append(shipping_item)
            tax_today += shipping_tax
        if tax_today > 0:
            line_items.append(
                {
                    "name": "Sales tax (WV)",
                    "kind": "tax",
                    "amount_cents": stripe_gateway.to_cents(tax_today),
                    "quantity": 1,
                }
            )
    charged_cents = sum(li["amount_cents"] * li["quantity"] for li in line_items)
    return line_items, preorder_variant_ids, charged_cents


def _find_order_for_session(env, session):
    """Reconcile a Stripe session back to its sale.order by stored session id,
    falling back to the order_id carried in session metadata."""
    SaleOrder = env["sale.order"].sudo()
    session_id = session.get("id")
    if session_id:
        order = SaleOrder.search([("grove_stripe_session_id", "=", session_id)], limit=1)
        if order:
            return order
    meta_order_id = (session.get("metadata") or {}).get("order_id")
    if meta_order_id:
        try:
            return SaleOrder.browse(int(meta_order_id)).exists()
        except (TypeError, ValueError):
            return SaleOrder
    return SaleOrder


def _oversold_lines(order):
    """Product lines that were charged in full but can no longer be fulfilled.

    Excludes the shipping line and any variant recorded as a preorder deposit
    at session time — a preorder is legitimately short on stock, only a line we
    took full payment for and now cannot ship is an oversell.

    Availability is read as ``free_qty`` (on-hand minus stock already reserved
    for other orders) in the *order's own company* — GOL-711. The session-build
    path evaluates stock via ``with_company(current_company)`` (the branch that
    owns the nursery warehouse), but this runs from the public webhook with no
    company in context, so a branch-warehouse quant is invisible in the ambient
    company and ``qty_available`` read 0 → a full-stock line (24 on-hand, 0
    reserved) was refunded as oversold. Pinning the company makes the quant
    visible; ``free_qty`` is the correct "can we still ship this" measure and,
    because draft/sent carts never reserve, accumulated test orders can't drive
    it falsely negative.
    """
    preorder_ids = set()
    for raw in (order.grove_preorder_variant_ids or "").split(","):
        raw = raw.strip()
        if raw.isdigit():
            preorder_ids.add(int(raw))
    oversold = []
    for line in order.order_line:
        if line.display_type or not line.product_id:
            continue
        product = line.product_id
        if product.default_code == SHIPPING_PRODUCT_CODE or product.id in preorder_ids:
            continue
        available = product.with_company(order.company_id).free_qty
        if available < line.product_uom_qty:
            oversold.append(line)
    return oversold


# ── Ship-time settlement (GOL-2053) ─────────────────────────────────────────
#
# A deposit-only preorder takes ONLY grove_amount_charged_today at checkout; the
# balance — tree prices + ACTUAL shipping (the labels we bought) + WV tax
# recomputed on that real cost — is captured off-session when the box ships.
#
# Product knobs (CEO-tunable via ir.config_parameter, no code change):
#   grove_headless.settlement_max_retries — automatic retries of a declined card
#     before it drops to manual-only (default 3; the retry cron enforces it).
# Ratified retry/dunning policy (CEO ruling GOL-2054): a declined ship-time
# charge flags the order settlement_failed, duns the customer (hosted Stripe
# pay-link email) + alerts ops on Discord, then AUTO-RETRIES the saved card
# DAILY up to settlement_max_retries. After the final decline the order HOLDS
# in settlement_failed for a human (Josh/Wesley) — it is never auto-cancelled.


def _settlement_shipping_line(order):
    """The GROVE-SHIP line whose price the settlement rewrites to ACTUAL cost,
    or an empty recordset for a pickup order that never carried one."""
    return order.order_line.filtered(lambda ol: ol.product_id and ol.product_id.default_code == SHIPPING_PRODUCT_CODE)[
        :1
    ]


def _recompute_ship_total(env, order):
    """Rebuild the order total on the ACTUAL packed shipping + destination-aware
    WV tax, so settlement bills what really shipped, not the checkout estimate.

    The quoted GROVE-SHIP line price is replaced with grove_actual_shipping_cost;
    Odoo then recomputes amount_tax from each line's existing (destination-
    correct) taxes. For a SHIP order we re-run _apply_destination_tax against the
    current ship-to state as well, so an address edited between checkout and ship
    still taxes correctly; PICKUP orders always transfer at the WV farm and keep
    WV tax (mirroring the draft path, which skips destination de-taxing)."""
    ship_line = _settlement_shipping_line(order)
    if ship_line:
        ship_line.price_unit = order.grove_actual_shipping_cost or 0.0
    if order.grove_fulfillment == "ship":
        state = order.partner_shipping_id.state_id.code or None
        _apply_destination_tax(env, order, {"state": state})
    order.invalidate_recordset(["amount_untaxed", "amount_tax", "amount_total"])


def _resolve_saved_card(secret_key, order):
    """(customer, payment_method) for the off-session charge.

    Prefers the ids already persisted on the order; otherwise reads them back
    from the DEPOSIT intent (setup_future_usage attached the method to the
    customer) and caches them so a retry does not re-hit Stripe. Returns
    (None, None) when neither the order nor a retrievable intent yields a card —
    the caller then duns for a manual payment instead of charging."""
    customer = order.grove_stripe_customer or None
    payment_method = order.grove_stripe_payment_method or None
    if customer and payment_method:
        return customer, payment_method
    pi_id = order.grove_stripe_payment_intent
    if not pi_id:
        return customer, payment_method
    try:
        intent = stripe_gateway.retrieve_payment_intent(secret_key, pi_id)
    except stripe_gateway.StripeError as exc:
        _logger.error("Could not retrieve deposit intent %s for %s: %s", pi_id, order.name, exc)
        return customer, payment_method
    customer = customer or intent.get("customer")
    payment_method = payment_method or intent.get("payment_method")
    vals = {}
    if customer and not order.grove_stripe_customer:
        vals["grove_stripe_customer"] = customer
    if payment_method and not order.grove_stripe_payment_method:
        vals["grove_stripe_payment_method"] = payment_method
    if vals:
        order.write(vals)
    return customer, payment_method


def _settlement_pay_link(env, order, secret_key, amount_cents):
    """A hosted Stripe Checkout URL for the customer to pay the balance manually
    after an off-session decline (GOL-2053 acceptance 4). Best-effort: returns
    None if the session can't be created, so the dunning email still sends with
    a 'contact us' fallback. The session is tagged purpose=settlement so its
    completion webhook settles the order without re-confirming it."""
    base = (env["ir.config_parameter"].sudo().get_param("web.base.url") or "").rstrip("/")
    try:
        session = stripe_gateway.create_checkout_session(
            secret_key,
            line_items=[{"name": f"Balance due — order {order.name}", "amount_cents": amount_cents, "quantity": 1}],
            success_url=f"{base}/shop/confirmation?order={order.id}",
            cancel_url=f"{base}/shop/cart",
            customer_email=order.partner_id.email,
            metadata={"order_id": order.id, "order_ref": order.name, "purpose": "settlement"},
        )
    except stripe_gateway.StripeError as exc:
        _logger.error("Dunning pay-link creation failed for %s: %s", order.name, exc)
        return None
    return session.get("url")


def _send_dunning_email(env, order, amount_due, pay_url):
    """Best-effort dunning email after a ship-time decline. Copy is deliberately
    plain and honest (the plant already shipped); the ratified final wording is
    a CEO decision on GOL-2052 — this is the functional default."""
    email = order.partner_id.email
    if not email:
        return
    if pay_url:
        cta = f'<p><a href="{pay_url}">Pay your balance securely here</a>.</p>'
    else:
        cta = "<p>Please reply to this email and we'll send you a secure payment link.</p>"
    body = (
        f"<p>Hi {order.partner_id.name or 'there'},</p>"
        f"<p>Your order {order.name} has shipped! We tried to collect the remaining "
        f"balance of ${amount_due:.2f} (your tree total plus actual shipping and tax) "
        f"on the card you used at checkout, but it didn't go through.</p>"
        f"{cta}"
        f"<p>Thank you — Goldberry Grove Nursery</p>"
    )
    try:
        env["mail.mail"].sudo().create(
            {
                "subject": f"Payment needed for your shipped order {order.name}",
                "email_to": email,
                "body_html": body,
                "auto_delete": True,
            }
        ).send()
    except Exception:  # noqa: BLE001 — dunning email is best-effort
        _logger.warning("Dunning email failed for %s", order.name, exc_info=True)


def _mark_settlement_failed(env, order, secret_key, amount_cents, *, reason):
    """Record a shipped-but-unsettled order and start the dunning path: flag the
    status, post chatter, alert ops on Discord, and email the customer a hosted
    payment link. The order STAYS shipped — the decline never rolls back the
    fulfilment (GOL-2053 acceptance 4)."""
    balance = round(amount_cents / 100.0, 2)
    order.write({"grove_checkout_status": "settlement_failed"})
    note = (
        f"Ship-time settlement of ${balance:.2f} failed ({reason}). Order stays "
        f"SHIPPED; customer has been emailed a payment link. Attempt "
        f"{order.grove_settlement_attempts}."
    )
    order.message_post(body=note)
    _notify_discord(
        f":rotating_light: Settlement FAILED on {order.name} — ${balance:.2f} unpaid "
        f"({reason}). Shipped but unsettled; customer dunned. Attempt "
        f"{order.grove_settlement_attempts}."
    )
    pay_url = _settlement_pay_link(env, order, secret_key, amount_cents)
    _send_dunning_email(env, order, balance, pay_url)


def settle_order_at_ship(env, order):
    """Capture a preorder's deferred balance off-session at ship (GOL-2053).

    Idempotent and safe to call from either ship trigger (label purchase or the
    operator mark-shipped path): an order already ``settled`` is a no-op, and the
    order-scoped Idempotency-Key means a replayed charge returns the original
    intent instead of double-billing. Never raises — a decline or gateway error
    is recorded on the order (the plant has shipped), never propagated.

    Returns a short status string for the caller/tests:
      settled | already_settled | not_applicable | nothing_due | no_key |
      settlement_failed | settlement_error
    """
    order.ensure_one()
    status = order.grove_checkout_status
    if status == "settled":
        return "already_settled"
    # Only a deposit-only order (or one whose earlier settlement failed) has a
    # deferred balance. A fully-in-stock order already collected shipping+tax at
    # checkout, and a non-checkout order has nothing to settle.
    if status not in ("deposit_paid", "settlement_failed"):
        return "not_applicable"

    _recompute_ship_total(env, order)
    balance = round((order.amount_total or 0.0) - (order.grove_amount_charged_today or 0.0), 2)
    if balance <= 0:
        order.write({"grove_checkout_status": "settled"})
        order.message_post(body=f"Ship-time settlement: nothing further due (balance ${balance:.2f}).")
        return "nothing_due"
    amount_cents = stripe_gateway.to_cents(balance)

    tenant = order.website_id.grove_tenant_slug() if order.website_id else None
    secret_key = _tenant_secret_key(tenant)
    if not secret_key:
        _logger.error("Ship-time settlement: no Stripe key for %s (tenant %s)", order.name, tenant)
        order.message_post(body="Ship-time settlement could not run: Stripe key is not configured.")
        return "no_key"

    attempts = (order.grove_settlement_attempts or 0) + 1
    customer, payment_method = _resolve_saved_card(secret_key, order)
    if not customer or not payment_method:
        order.write({"grove_settlement_attempts": attempts})
        _mark_settlement_failed(env, order, secret_key, amount_cents, reason="no saved card on file")
        return "settlement_failed"

    # Order-scoped so a retried ship never double-charges (GOL-2053 acceptance 3).
    idem = f"grove-settle-{order.id}-{order.grove_stripe_session_id or order.grove_stripe_payment_intent or order.id}"
    try:
        intent = stripe_gateway.create_payment_intent(
            secret_key,
            amount_cents=amount_cents,
            customer=customer,
            payment_method=payment_method,
            metadata={"order_ref": order.name, "purpose": "ship_settlement"},
            idempotency_key=idem,
            description=f"Ship-time balance for {order.name}",
        )
    except stripe_gateway.StripeCardError as exc:
        order.write(
            {
                "grove_settlement_attempts": attempts,
                "grove_settlement_payment_intent": exc.payment_intent or order.grove_settlement_payment_intent,
            }
        )
        _mark_settlement_failed(
            env, order, secret_key, amount_cents, reason=f"card declined ({exc.decline_code or exc.code or 'declined'})"
        )
        return "settlement_failed"
    except stripe_gateway.StripeError as exc:
        # Transport/config error (not a decline) — retryable. Keep the order in
        # its current status so the retry cron / manual re-trigger tries again.
        order.write({"grove_settlement_attempts": attempts})
        _logger.error("Ship-time settlement gateway error for %s: %s", order.name, exc)
        order.message_post(body=f"Ship-time settlement could not reach Stripe (will retry): {exc}")
        _notify_discord(f":warning: Settlement gateway error on {order.name} (${balance:.2f}) — will retry: {exc}")
        return "settlement_error"

    order.write(
        {
            "grove_checkout_status": "settled",
            "grove_settlement_payment_intent": intent.get("id") or order.grove_settlement_payment_intent,
            "grove_settlement_attempts": attempts,
        }
    )
    order.message_post(
        body=(
            f"Ship-time settlement captured ${balance:.2f} off-session — actual shipping "
            f"${order.grove_actual_shipping_cost or 0.0:.2f}, recomputed tax ${order.amount_tax:.2f}."
        )
    )
    return "settled"


def _handle_settlement_paid(env, order, session):
    """A customer paid the dunning link (purpose=settlement): mark the order
    settled and record the intent, WITHOUT re-running the oversell / confirm /
    receipt path a first-time checkout does."""
    if order.grove_checkout_status != "settled":
        order.write(
            {
                "grove_checkout_status": "settled",
                "grove_settlement_payment_intent": session.get("payment_intent")
                or order.grove_settlement_payment_intent,
            }
        )
        order.message_post(body="Ship-time balance paid by the customer via the payment link.")
    return "settled"


def _handle_session_completed(env, session):
    """checkout.session.completed: record the payment intent, then either
    refund an oversell or mark the order paid/deposit-paid and confirm it."""
    order = _find_order_for_session(env, session)
    if not order:
        return "order_not_found"

    # A dunning payment (customer clearing a failed ship-time settlement) settles
    # the order directly — it must not re-run oversell/confirm/receipt (GOL-2053).
    if (session.get("metadata") or {}).get("purpose") == "settlement":
        return _handle_settlement_paid(env, order, session)

    payment_intent = session.get("payment_intent")
    vals = {}
    if payment_intent:
        vals["grove_stripe_payment_intent"] = payment_intent
    # Persist the saved-card handle for the ship-time off-session settlement
    # (GOL-2053): setup_future_usage=off_session attaches the payment method to a
    # Customer, whose id the completed session carries. The payment_method id is
    # resolved from the deposit intent at settlement (it is not on the session).
    customer = session.get("customer")
    if customer:
        vals["grove_stripe_customer"] = customer

    oversold = _oversold_lines(order)
    if oversold:
        names = ", ".join(line.product_id.display_name for line in oversold)
        refunded = False
        if payment_intent:
            # The payment_intent lives in the account that CHARGED it, so the
            # refund must use that same tenant's key (GOL-1766). The order
            # carries its originating website, so resolve the tenant from it
            # rather than the ambient env (this runs from the public webhook
            # with no website in context).
            tenant = order.website_id.grove_tenant_slug() if order.website_id else None
            secret_key = _tenant_secret_key(tenant)
            try:
                stripe_gateway.create_refund(
                    secret_key,
                    payment_intent,
                    reason="requested_by_customer",
                    metadata={"order_ref": order.name, "reason": "oversold"},
                )
                refunded = True
            except stripe_gateway.StripeError as exc:
                _logger.error("Oversell refund failed for %s: %s", order.name, exc)
        vals["grove_checkout_status"] = "refunded_oversell"
        order.write(vals)
        note = (
            f"Oversold: on-hand stock can no longer fulfil {names}. Payment has been "
            f"{'refunded' if refunded else 'flagged for a MANUAL refund'} with our apologies."
        )
        order.message_post(body=note)
        _notify_customer_apology(env, order, names, refunded)
        _notify_discord(
            f":warning: Oversold order {order.name}: {names} — "
            f"refund {'issued' if refunded else 'FAILED, needs manual action'}."
        )
        return "refunded_oversell" if refunded else "oversell_refund_failed"

    has_preorder = bool((order.grove_preorder_variant_ids or "").strip())
    vals["grove_checkout_status"] = "deposit_paid" if has_preorder else "paid"
    order.write(vals)
    try:
        if order.state in ("draft", "sent"):
            order.action_confirm()
    except Exception:  # noqa: BLE001 — payment is already recorded; don't fail the webhook
        _logger.exception("action_confirm failed for %s (payment recorded, confirm deferred)", order.name)
    _send_order_confirmation_email(env, order)
    # Post-purchase ops chain (GOL-1933): alert staff on EVERY new paid order —
    # both fulfilment types, deposit or full. Pickup orders get no label but must
    # still notify (Josh). Both are best-effort and must never fail the webhook.
    _notify_new_order(env, order, has_preorder)
    _notify_merchant_email(env, order, has_preorder)
    if has_preorder:
        # Deposit explainer alongside the branded receipt (GOL-1666): the
        # standard sale template lists the charged-today totals; this line
        # spells out the deposit/balance arrangement in the ratified voice.
        _notify_preorder_deposit(env, order)
    return vals["grove_checkout_status"]


def _handle_session_expired(env, session):
    """checkout.session.expired: mark the draft order's checkout as expired."""
    order = _find_order_for_session(env, session)
    if not order:
        return "order_not_found"
    order.write({"grove_checkout_status": "expired"})
    return "expired"


def _parse_calendar_override(env):
    """Parsed `grove_headless.shipping_calendar` override dict, or None.

    Module-level twin of the controller's `_shipping_calendar_override` so the
    webhook/notify path (which has `env` but no `request`) can resolve the same
    admin-editable calendar. Malformed JSON is ignored (fall back to defaults).
    """
    raw = env["ir.config_parameter"].sudo().get_param("grove_headless.shipping_calendar")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        _logger.warning("grove_headless.shipping_calendar is not valid JSON; using calendar defaults")
        return None


def _preorder_ship_season(env, order):
    """Best-effort ship season ('spring' | 'fall') for the order's destination,
    or None when the shipping ZIP/zone is unknown. Used only to word the
    preorder-deposit emails ("...ships this spring"); a None just drops the
    season word, never blocks the email."""
    partner = order.partner_shipping_id or order.partner_id
    zip_code = partner.zip if partner else None
    zone = usda_zone_for_zip(zip_code)
    if zone is None:
        return None
    calendar = merge_calendar_override(_parse_calendar_override(env))
    return resolve_fulfillment(zone, _date.today(), calendar).get("season")


def _notify_preorder_deposit(env, order):
    """Best-effort preorder-deposit explainer emailed alongside the standard
    order-confirmation receipt (GOL-1666). Only sent for orders that took a
    deposit (grove_preorder_variant_ids set). Never fatal: the payment and the
    branded receipt already stand on their own if outgoing mail is unconfigured.
    """
    email = order.partner_id.email
    if not email:
        return
    season = _preorder_ship_season(env, order)
    body = (
        f"<p>Hi {order.partner_id.name or 'there'},</p>"
        f"<p>Thanks for reserving with us! Your order <strong>{order.name}</strong> "
        f"includes preorder trees.</p>"
        f"<p>{confirmation_deposit_line(season)}</p>"
        f"<p>We'll email you again when your trees ship.</p>"
        f"<p>Goldberry Grove Nursery</p>"
    )
    try:
        env["mail.mail"].sudo().create(
            {
                "subject": f"Your preorder deposit for {order.name}",
                "email_to": email,
                "body_html": body,
                "auto_delete": True,
            }
        ).send()
    except Exception:  # noqa: BLE001 — deposit explainer is best-effort
        _logger.warning("Preorder deposit email failed for %s", order.name, exc_info=True)


def _notify_customer_apology(env, order, product_names, refunded):
    """Best-effort customer apology email for an oversell. Never fatal — the
    refund + chatter note stand on their own if outgoing mail is unconfigured."""
    email = order.partner_id.email
    if not email:
        return
    body = (
        f"<p>Hi {order.partner_id.name or 'there'},</p>"
        f"<p>We're very sorry — we sold out of {product_names} before your order "
        f"{order.name} could be fulfilled, so we've "
        f"{'refunded your payment in full' if refunded else 'begun refunding your payment'}. "
        f"Please reach out and we'll help you find an alternative.</p>"
        f"<p>— Goldberry Grove Nursery</p>"
    )
    try:
        env["mail.mail"].sudo().create(
            {
                "subject": f"About your order {order.name}",
                "email_to": email,
                "body_html": body,
                "auto_delete": True,
            }
        ).send()
    except Exception:  # noqa: BLE001 — apology email is best-effort
        _logger.warning("Oversell apology email failed for %s", order.name, exc_info=True)


def _notify_discord(message):
    """Best-effort ops ping. DISCORD_OPS_WEBHOOK_URL is optional; a missing URL
    or a failed POST never breaks webhook processing."""
    url = os.environ.get("DISCORD_OPS_WEBHOOK_URL", "")
    if not url:
        return
    try:
        # allowed_mentions parse:[] disarms every mention — a customer named
        # "@everyone"/"@here" reaching the ops channel via order alerts (GOL-1933)
        # must not ping staff.
        requests.post(
            url,
            json={"content": message, "allowed_mentions": {"parse": []}},
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        _logger.warning("Discord ops notify failed", exc_info=True)


def _order_alert_context(order):
    """Primitive values the pure alert formatters need, read off the order once
    (GOL-1933). Kept together so the Discord ping and the merchant email describe
    the same order identically. Carrier is read defensively so the alert lights
    up automatically once GOL-1906 persists it — no field reference that would
    break on origin/main where it does not yet exist."""
    lines = [
        (line.product_id.display_name, line.product_uom_qty)
        for line in order.order_line
        if not line.display_type and line.product_id and line.product_id.product_tmpl_id.type != "service"
    ]
    tracking = (order.grove_tracking_numbers or "").strip().replace("\n", ", ") or None
    carrier = getattr(order, "grove_carrier", False) or None
    return {
        "order_ref": order.name,
        "customer": order.partner_id.name,
        "customer_email": order.partner_id.email,
        "fulfillment": order.grove_fulfillment,
        "lines": lines,
        "total": order.amount_total,
        "currency": order.currency_id.name or "USD",
        "carrier": carrier,
        "tracking": tracking,
    }


def _shipping_address_text(order):
    """One-line ship-to for the merchant email, or None for pickup/no address.

    Each partner field is HTML-escaped before the ``<br/>`` join so untrusted
    address input can't inject markup into the staff email (GOL-1933 review);
    the caller inserts the result as pre-escaped HTML and must not re-escape it."""
    if order.grove_fulfillment != "ship":
        return None
    p = order.partner_shipping_id or order.partner_id
    if not p:
        return None
    bits = [p.name, p.street, p.street2, p.city]
    tail = " ".join(x for x in [p.state_id.code, p.zip] if x)
    if tail:
        bits.append(tail)
    return "<br/>".join(html.escape(x) for x in bits if x) or None


def _notify_new_order(env, order, is_deposit):
    """Best-effort Discord ops ping on a new paid order (GOL-1933). Fires for
    both fulfilment types; a missing DISCORD_OPS_WEBHOOK_URL or a failed POST is
    swallowed by _notify_discord so it never breaks webhook processing."""
    try:
        message = format_new_order_discord(is_deposit=is_deposit, **_order_alert_context(order))
        _notify_discord(message)
    except Exception:  # noqa: BLE001 — ops alert is best-effort
        _logger.warning("New-order Discord alert failed for %s", order.name, exc_info=True)


def _notify_merchant_email(env, order, is_deposit):
    """Best-effort internal merchant notification to order.company_id.email
    (GOL-1933), separate from the customer receipt. Never fatal — the payment is
    already recorded. A missing company email is logged loudly (it means staff
    get no alert) rather than silently dropped."""
    recipient = order.company_id.email
    if not recipient:
        _logger.warning(
            "No company email on %s; merchant order alert not sent for %s",
            order.company_id.display_name,
            order.name,
        )
        return
    try:
        subject, body_html = format_merchant_email(
            is_deposit=is_deposit,
            shipping_address=_shipping_address_text(order),
            **_order_alert_context(order),
        )
        env["mail.mail"].sudo().create(
            {
                "subject": subject,
                "email_to": recipient,
                "body_html": body_html,
                "auto_delete": True,
            }
        ).send()
    except Exception:  # noqa: BLE001 — merchant alert is best-effort
        _logger.warning("Merchant order alert failed for %s", order.name, exc_info=True)


def _send_order_confirmation_email(env, order):
    """Best-effort order-confirmation receipt once payment is recorded (GOL-988).

    Uses the standard Odoo sale confirmation template so the receipt is branded
    and lists the order lines + totals, and so Odoo's outgoing mail server
    (Mailgun SMTP in QA/prod) applies the configured From/from_filter. Never
    fatal: the payment is already recorded and the chatter/webhook stand on
    their own if outgoing mail is unconfigured."""
    if not order.partner_id.email:
        return
    template = env.ref("sale.mail_template_sale_confirmation", raise_if_not_found=False)
    if not template:
        _logger.warning("sale confirmation template missing; skipping receipt for %s", order.name)
        return
    try:
        template.sudo().send_mail(order.id, force_send=True)
    except Exception:  # noqa: BLE001 — receipt is best-effort, never fails the webhook
        _logger.warning("Order confirmation email failed for %s", order.name, exc_info=True)


def _apply_delivery_status(env, order, new_status, tracking):
    """Record the new Shippo delivery status and, on a *transition* into a
    notify-worthy state, email the customer. Idempotent: a repeated webhook for
    a status the order already has sends no second email, so a customer gets one
    "shipped" and one "delivered" notice even when both the operator signal
    (Phase 2) and the Shippo transit scan fire. Returns True when the status
    changed."""
    if new_status == order.grove_delivery_status:
        return False
    order.grove_delivery_status = new_status
    _notify_shipping_status(env, order, new_status, tracking)
    return True


def _order_shipments(order, fallback_tracking=None):
    """(carrier, tracking) pairs for the branded shipment notice, read from the
    per-box fields Shippo persists (grove_shipping_carriers index-aligned with
    grove_tracking_numbers, GOL-1906). Falls back to the single webhook tracking
    number when the order carries no persisted boxes."""
    trackings = (order.grove_tracking_numbers or "").splitlines()
    carriers = (order.grove_shipping_carriers or "").splitlines()
    pairs = []
    for i, number in enumerate(trackings):
        number = number.strip()
        if not number:
            continue
        carrier = carriers[i].strip() if i < len(carriers) else ""
        pairs.append((carrier, number))
    if not pairs and fallback_tracking:
        pairs.append(("", fallback_tracking))
    return pairs


def _notify_shipping_status(env, order, status, tracking):
    """Best-effort branded shipment-notification email (GOL-988 / GOL-1979).
    Never fatal — the delivery-status write has already landed, and a mail
    failure must not make Shippo retry the webhook. Only notify-worthy statuses
    (NOTIFY_STATUSES) produce an email; everything else is a silent status
    update. Renders the carrier and a clickable per-carrier tracking link for
    each packed box."""
    if status not in NOTIFY_STATUSES or not order.partner_id.email:
        return
    # Pre-ship balance reminder (GOL-1666): only on the "shipped"/transit notice
    # and only for orders that took a preorder deposit, so a full-charge order
    # never sees a balance line it does not owe.
    balance_line = None
    if status == "transit" and (order.grove_preorder_variant_ids or "").strip():
        balance_line = preship_balance_line(_preorder_ship_season(env, order))
    subject, body = shipment_notice_copy(
        status=status,
        order_name=order.name,
        customer_name=order.partner_id.name,
        shipments=_order_shipments(order, fallback_tracking=tracking),
        balance_line=balance_line,
    )
    # Reply-To to the selling company's formatted address so a customer reply
    # lands with the operator, not the no-reply envelope sender.
    reply_to = getattr(order.company_id, "email_formatted", False) or order.company_id.email or None
    try:
        env["mail.mail"].sudo().create(
            {
                "subject": subject,
                "email_to": order.partner_id.email,
                "reply_to": reply_to,
                "body_html": body,
                "auto_delete": True,
            }
        ).send()
    except Exception:  # noqa: BLE001 — shipping notice is best-effort
        _logger.warning("Shipping notification email failed for %s", order.name, exc_info=True)
