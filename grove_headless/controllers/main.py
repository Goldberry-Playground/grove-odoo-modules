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
