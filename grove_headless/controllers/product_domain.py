"""Pure filter-domain builder for the public product_list endpoint.

No Odoo imports on purpose: this module tests standalone under pytest
(repo convention) and is consumed by controllers/main.py at runtime.
"""

import re


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text) -> str:
    """Lowercase-hyphenate a category name into a stable URL slug.

    Mirrors the storefront slug convention so ``?cat=<slug>`` lines up with the
    website category names the nursery maintains in Odoo ("Stone Fruit" ->
    "stone-fruit", "Trees" -> "trees"). Kept pure/here so the controller and the
    unit tests share one definition.
    """
    return _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")


# Valid `grove_layer` / `grove_sun` Selection values (models/product_template.py).
# A facet value outside these sets is ignored rather than pushed into the domain,
# mirroring the _to_int guard on the numeric filters.
LAYER_VALUES = {"canopy", "understory", "shrub", "ground", "vine"}
SUN_VALUES = {"full", "partial", "shade"}


def build_product_domain(kwargs: dict, company_id: int, cat_category_ids=None) -> list:
    # Visibility is gated by `website_published` alone — NOT `sale_ok`. A
    # published-but-not-for-sale "coming soon" placeholder (sale_ok=False,
    # GOL-757/760) must appear in the /shop grid and ?cat= facets so shoppers
    # can browse to it; the storefront reads the serialized `sale_ok` to render
    # the card + detail buy box as not-purchasable. (Odoo semantics:
    # website_published = "show it", sale_ok = "can it be sold".)
    domain = [
        ("website_published", "=", True),
        ("company_id", "in", [company_id, False]),
    ]
    if kwargs.get("featured"):
        domain.append(("grove_featured", "=", True))
    category_id = _to_int(kwargs.get("category_id"))
    if category_id is not None:
        domain.append(("public_categ_ids", "in", [category_id]))
    # ?cat=<slug> — the storefront's SEO-friendly twin of ?category_id. The
    # controller resolves the slug to website-category ids (slugify(name)==slug)
    # and passes them in; the pure builder stays Odoo-free. An unknown slug
    # resolves to [] here -> [-1] so it returns an empty set (empty state),
    # never the whole catalog (which a bare `("public_categ_ids","in",[])` would).
    if str(kwargs.get("cat") or "").strip():
        domain.append(("public_categ_ids", "in", cat_category_ids or [-1]))
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
    layer = str(kwargs.get("layer") or "").strip().lower()
    if layer in LAYER_VALUES:
        domain.append(("grove_layer", "=", layer))
    sun = str(kwargs.get("sun") or "").strip().lower()
    if sun in SUN_VALUES:
        domain.append(("grove_sun", "=", sun))
    return domain


def zone_response(zip_raw, zone):
    """(body, status) for the ZIP->USDA-zone lookup endpoint."""
    if zone is None:
        return {"error": "unknown zip"}, 404
    return {"zip": str(zip_raw or "")[:5], "zone": zone}, 200
