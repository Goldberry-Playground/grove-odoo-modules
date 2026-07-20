import hmac
import json
import logging
import os
import re
from datetime import date as _date

import psycopg2
import requests
from odoo import http
from odoo.http import Response, request

from ..models import stripe_gateway
from ..models.newsletter import newsletter_tag_names
from ..models.shipping_calendar import serialize_ship_options, ship_options, usda_zone_for_zip
from ..models.shipping_zones import compute_order_shipping, compute_shipping_rate
from ..models.shippo_client import is_valid_tracking
from .product_domain import build_product_domain, slugify, zone_response

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
]

PRODUCT_DETAIL_FIELDS = PRODUCT_LIST_FIELDS + [
    "description_sale",
    "grove_seo_description",
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


def _structure_variant(variant):
    """Structured variant entry: axes parsed into fields, not display-name strings."""
    axis = {v.attribute_id.name: v.name for v in variant.product_template_variant_value_ids}
    return {
        "id": variant.id,
        "display_name": variant.display_name,
        "sku": variant.default_code or "",
        "cultivar": axis.get("Cultivar", ""),
        "format": axis.get("Format", ""),
        "price": variant.lst_price,
        "qty_available": variant.qty_available,
        "shipping_tier": variant.grove_effective_shipping_tier,
        "image_url": f"/web/image/product.product/{variant.id}/image_128",
    }


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
                data["image_url"] = f"/web/image/product.template/{product.id}/image_128"
                data["slug"] = data.pop("grove_slug", "") or ""
                data["tags"] = [{"id": t.id, "name": t.name} for t in product.product_tag_ids]
                data["categories"] = [
                    {"id": c.id, "name": c.name, "slug": slugify(c.name)} for c in product.public_categ_ids
                ]
                data["variant_count"] = len(product.product_variant_ids)
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
        data["image_url"] = f"/web/image/product.template/{product.id}/image_1920"
        data["variants"] = [_structure_variant(v) for v in product.product_variant_ids]
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
                    "image_url": f"/web/image/product.product/{line.product_id.id}/image_128",
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
        result = serialize_ship_options(ship_options(zip_code, tier, _date.today()))
        result["per_tree_rate"] = compute_shipping_rate(state, tier=tier)
        return _json_response(result)

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

        secret_key = os.environ.get("stripe_test_secret_key", "")
        if not secret_key:
            # Code ships before keys land (GOL-642): the endpoint is live but
            # inert until Terra applies stripe_test_* to the QA droplet env.
            return _json_response({"error": "Checkout is not configured yet"}, status=503)

        success_url = payload.get("success_url")
        cancel_url = payload.get("cancel_url")
        if not success_url or not cancel_url:
            return _json_response({"error": "success_url and cancel_url are required"}, status=400)

        order, error = _create_draft_order(request.website, request.env, payload)
        if error is not None:
            return error

        access_token = order._portal_ensure_token()
        line_items, preorder_ids, charged_cents = _build_stripe_line_items(order)
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
            }
        )

        return _json_response(
            {
                "session_id": session.get("id"),
                "checkout_url": session.get("url"),
                "order_id": order.id,
                "order_ref": order.name,
                "access_token": access_token,
                "has_preorder": bool(preorder_ids),
                "amount_due_today": round(charged_cents / 100.0, 2),
                "amount_total": order.amount_total,
                "currency": order.currency_id.name,
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
        secret = os.environ.get("stripe_test_webhook_secret", "")
        sig = request.httprequest.headers.get("Stripe-Signature", "")
        try:
            stripe_gateway.verify_webhook_signature(raw, sig, secret)
        except stripe_gateway.StripeError as exc:
            _logger.warning("Stripe webhook rejected: %s", exc)
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

        ledger.write({"notes": result})
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

        Auth: token sent via `X-Grove-Webhook-Token` request header (configure
        this custom header in the Shippo webhook settings URL for this endpoint).
        The token is compared with `hmac.compare_digest` to prevent timing-oracle
        attacks. GROVE_SHIPPO_WEBHOOK_TOKEN must be set in the server environment.
        """
        try:
            payload = json.loads(request.httprequest.get_data() or b"{}")
        except (json.JSONDecodeError, ValueError):
            return _json_response({"error": "bad json"}, status=400)

        expected = os.environ.get("GROVE_SHIPPO_WEBHOOK_TOKEN", "")
        token = request.httprequest.headers.get("X-Grove-Webhook-Token", "")
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
        orders.write({"grove_delivery_status": status.lower()})
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
            state_code = (address.get("state") or "").upper()
            if state_code:
                state = (
                    env["res.country.state"]
                    .sudo()
                    .search(
                        [("code", "=", state_code), ("country_id", "=", country.id)],
                        limit=1,
                    )
                )
                if state:
                    vals["state_id"] = state.id
    return vals


SHIPPING_PRODUCT_CODE = "GROVE-SHIP"


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

    Fail-safe: returns without adding a line when no rate is configured for
    the destination or any line's tier — never a guessed charge.
    """
    state = (shipping or {}).get("state")
    if not state:
        return
    items = [
        (
            line.product_id.product_tmpl_id.grove_shipping_tier or "potted",
            line.product_uom_qty,
        )
        for line in order.order_line
        if not line.display_type and line.product_id
    ]
    if not items:
        return
    charge = compute_order_shipping(state, items)
    if charge is None:
        return

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

    # Apply the tiered 21-state shipping charge (GOL-15). Rates load from
    # data/shipping_rates.json (models/shipping_zones.py) and are maintained by
    # the daily rate-checker. Fail-safe: no rate configured → no line added.
    _apply_shipping_line(env, order, shipping, current_company)

    return order, None


# ── Stripe checkout helpers ──────────────────────────────────────────────────


def _build_stripe_line_items(order):
    """Turn a draft order's lines into Stripe Checkout line items.

    Returns (line_items, preorder_variant_ids, charged_cents). Applies the
    charging matrix per product line (in-stock = full price; short stock =
    a flat deposit line at qty 1) and adds the WV sales tax as ONE explicit
    line (Stripe Tax OFF) covering only what is charged today — preorder lines
    contribute a deposit and no tax now; their goods + tax settle off-session
    when they ship.
    """
    line_items = []
    preorder_variant_ids = []
    tax_today = 0.0
    for line in order.order_line:
        if line.display_type or not line.product_id:
            continue
        product = line.product_id
        name = product.display_name
        if product.default_code == SHIPPING_PRODUCT_CODE:
            amount = stripe_gateway.to_cents(line.price_unit)
            if amount <= 0:
                continue
            line_items.append({"name": name, "amount_cents": amount, "quantity": 1})
            tax_today += line.price_tax
            continue
        amount, qty, is_preorder = stripe_gateway.line_charge(
            line.price_unit, line.product_uom_qty, product.qty_available
        )
        if is_preorder:
            preorder_variant_ids.append(product.id)
            line_items.append({"name": f"Deposit — {name}", "amount_cents": amount, "quantity": qty})
        else:
            line_items.append({"name": name, "amount_cents": amount, "quantity": qty})
            tax_today += line.price_tax
    if tax_today > 0:
        line_items.append({"name": "Sales tax (WV)", "amount_cents": stripe_gateway.to_cents(tax_today), "quantity": 1})
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
        if product.qty_available < line.product_uom_qty:
            oversold.append(line)
    return oversold


def _handle_session_completed(env, session):
    """checkout.session.completed: record the payment intent, then either
    refund an oversell or mark the order paid/deposit-paid and confirm it."""
    order = _find_order_for_session(env, session)
    if not order:
        return "order_not_found"

    payment_intent = session.get("payment_intent")
    vals = {}
    if payment_intent:
        vals["grove_stripe_payment_intent"] = payment_intent

    oversold = _oversold_lines(order)
    if oversold:
        names = ", ".join(line.product_id.display_name for line in oversold)
        refunded = False
        if payment_intent:
            secret_key = os.environ.get("stripe_test_secret_key", "")
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
    return vals["grove_checkout_status"]


def _handle_session_expired(env, session):
    """checkout.session.expired: mark the draft order's checkout as expired."""
    order = _find_order_for_session(env, session)
    if not order:
        return "order_not_found"
    order.write({"grove_checkout_status": "expired"})
    return "expired"


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
        requests.post(url, json={"content": message}, timeout=10)
    except Exception:  # noqa: BLE001
        _logger.warning("Discord ops notify failed", exc_info=True)
