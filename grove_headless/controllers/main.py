import json
import logging

from odoo import http
from odoo.http import Response, request

_logger = logging.getLogger(__name__)

# Fields exposed in the public product list (keep minimal for performance)
PRODUCT_LIST_FIELDS = [
    "id",
    "name",
    "list_price",
    "default_code",
    "website_published",
    "grove_featured",
    "image_128",
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

        domain = [
            ("website_published", "=", True),
            ("sale_ok", "=", True),
            ("company_id", "in", [current_company.id, False]),
        ]

        # Optional filters
        if kwargs.get("featured"):
            domain.append(("grove_featured", "=", True))

        if kwargs.get("category_id"):
            try:
                domain.append(("public_categ_ids", "in", [int(kwargs["category_id"])]))
            except (ValueError, TypeError):
                pass

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
        data["variants"] = []

        variant_fields = ["id", "display_name", "default_code", "lst_price"] + _available_fields(
            request.env["product.product"], OPTIONAL_STOCK_FIELDS
        )
        for variant in product.product_variant_ids:
            variant_vals = variant.read(variant_fields)[0]
            variant_vals["image_url"] = f"/web/image/product.product/{variant.id}/image_128"
            data["variants"].append(variant_vals)

        return _json_response(data)

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

        sale_order = request.cart or request.website._create_cart()
        sale_order._cart_add(product_id=variant.id, quantity=quantity)

        return self.cart_get()

    # ── Orders ───────────────────────────────────────────────────────────

    @http.route(
        "/grove/api/v1/orders",
        type="http",
        auth="public",
        website=True,
        methods=["POST"],
        csrf=False,
    )
    def order_create(self, **_kwargs):
        """Create a draft sale.order from a posted cart payload.

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

        contact = payload.get("contact") or {}
        items = payload.get("items") or []

        if not contact.get("email") or not contact.get("name"):
            return _json_response({"error": "contact.name and contact.email are required"}, status=400)
        if not isinstance(items, list) or not items:
            return _json_response({"error": "items must be a non-empty list"}, status=400)

        website = request.website
        current_company = website.company_id
        env = request.env

        # Resolve partner: find an existing partner scoped to this company by
        # email so we don't read/write across tenants. We deliberately do NOT
        # overwrite an existing partner's attributes from a public POST — that
        # would let anyone with a customer's email mutate their record. Instead
        # we either reuse the existing partner as-is, or create a fresh one.
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

        # Build order lines. Validate every variant up front so partial orders
        # never get persisted.
        line_vals = []
        for raw_item in items:
            try:
                variant_id = int(raw_item.get("variant_id"))
                quantity = float(raw_item.get("quantity") or 1)
            except (TypeError, ValueError):
                order.unlink()
                return _json_response(
                    {"error": "Each item needs numeric variant_id and quantity"},
                    status=400,
                )

            variant = (
                env["product.product"]
                .sudo()
                .with_company(current_company)
                .search(
                    [("id", "=", variant_id), ("company_id", "in", [current_company.id, False])],
                    limit=1,
                )
            )
            if not variant:
                order.unlink()
                return _json_response({"error": f"Product variant {variant_id} not found"}, status=404)

            line_vals.append(
                {
                    "order_id": order.id,
                    "product_id": variant.id,
                    "product_uom_qty": quantity,
                }
            )

        env["sale.order.line"].sudo().create(line_vals)
        order.invalidate_recordset(["amount_untaxed", "amount_tax", "amount_total"])

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


def _format_payment_note(payment_method):
    """Render the chosen payment method as a human-readable order note.

    Real payment integration lands in a later sprint; for now we just record
    what the customer selected so staff can follow up.
    """
    if not payment_method:
        return False
    return f"Payment method requested: {payment_method}"
