import json
import logging

from odoo.http import Response, request

from odoo import http

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
    "qty_available",
    "website_url",
    "image_1920",
]


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

        data = _serialize_product(product, PRODUCT_DETAIL_FIELDS)
        data["image_url"] = f"/web/image/product.template/{product.id}/image_1920"
        data["variants"] = []

        for variant in product.product_variant_ids:
            variant_vals = variant.read(["id", "name", "default_code", "lst_price", "qty_available"])[0]
            variant_vals["image_url"] = f"/web/image/product.product/{variant.id}/image_128"
            data["variants"].append(variant_vals)

        return _json_response(data)

    # ── Cart ─────────────────────────────────────────────────────────────

    @http.route(
        "/grove/api/v1/cart",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def cart_get(self, **_kwargs):
        website = request.website
        sale_order = website.sale_get_order()

        if not sale_order:
            return _json_response({"lines": [], "amount_total": 0, "currency": None})

        lines = []
        for line in sale_order.order_line:
            lines.append(
                {
                    "id": line.id,
                    "product_id": line.product_id.id,
                    "product_name": line.product_id.name,
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
        methods=["POST"],
        csrf=False,
    )
    def cart_update(self, **_kwargs):
        try:
            payload = json.loads(request.httprequest.data or "{}")
        except json.JSONDecodeError:
            return _json_response({"error": "Invalid JSON body"}, status=400)

        product_id = payload.get("product_id")
        quantity = payload.get("quantity", 1)

        if not product_id:
            return _json_response({"error": "product_id is required"}, status=400)

        try:
            product_id = int(product_id)
            quantity = float(quantity)
        except (ValueError, TypeError):
            return _json_response({"error": "Invalid product_id or quantity"}, status=400)

        website = request.website
        current_company = website.company_id

        # Verify product exists, is published, and belongs to current company
        product = (
            request.env["product.product"]
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

        sale_order = website.sale_get_order(force_create=True)
        sale_order._cart_update(
            product_id=product.id,
            set_qty=quantity,
        )

        # Return the updated cart
        return self.cart_get()
