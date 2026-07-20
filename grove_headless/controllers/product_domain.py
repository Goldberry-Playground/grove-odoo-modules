"""Pure filter-domain builder for the public product_list endpoint.

No Odoo imports on purpose: this module tests standalone under pytest
(repo convention) and is consumed by controllers/main.py at runtime.
"""


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_product_domain(kwargs: dict, company_id: int) -> list:
    domain = [
        ("website_published", "=", True),
        ("sale_ok", "=", True),
        ("company_id", "in", [company_id, False]),
    ]
    if kwargs.get("featured"):
        domain.append(("grove_featured", "=", True))
    category_id = _to_int(kwargs.get("category_id"))
    if category_id is not None:
        domain.append(("public_categ_ids", "in", [category_id]))
    slug = str(kwargs.get("slug") or "").strip().lower()
    if slug:
        domain.append(("grove_slug", "=", slug))
    tag_id = _to_int(kwargs.get("tag_id"))
    if tag_id is not None:
        domain.append(("product_tag_ids", "in", [tag_id]))
    zone = _to_int(kwargs.get("zone"))
    if zone is not None:
        domain.append(("grove_zone_min", "<=", zone))
        domain.append(("grove_zone_max", ">=", zone))
    return domain


def zone_response(zip_raw, zone):
    """(body, status) for the ZIP->USDA-zone lookup endpoint."""
    if zone is None:
        return {"error": "unknown zip"}, 404
    return {"zip": str(zip_raw or "")[:5], "zone": zone}, 200
