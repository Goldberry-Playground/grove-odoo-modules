"""Pure tests for the product_list filter-domain builder (no Odoo runtime)."""

import importlib.util
import pathlib

_path = pathlib.Path(__file__).parent.parent / "controllers" / "product_domain.py"
_spec = importlib.util.spec_from_file_location("product_domain", _path)
product_domain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(product_domain)

BASE = [
    ("website_published", "=", True),
    ("sale_ok", "=", True),
    ("company_id", "in", [7, False]),
]


def test_base_domain_no_filters():
    assert product_domain.build_product_domain({}, 7) == BASE


def test_tag_filter():
    dom = product_domain.build_product_domain({"tag_id": "3"}, 7)
    assert ("product_tag_ids", "in", [3]) in dom


def test_zone_filter_brackets_range():
    dom = product_domain.build_product_domain({"zone": "6"}, 7)
    assert ("grove_zone_min", "<=", 6) in dom
    assert ("grove_zone_max", ">=", 6) in dom


def test_layer_filter():
    dom = product_domain.build_product_domain({"layer": "canopy"}, 7)
    assert ("grove_layer", "=", "canopy") in dom


def test_sun_filter_normalizes_case_and_whitespace():
    dom = product_domain.build_product_domain({"sun": " Full "}, 7)
    assert ("grove_sun", "=", "full") in dom


def test_layer_sun_combine_with_zone():
    dom = product_domain.build_product_domain({"zone": "6", "layer": "shrub", "sun": "partial"}, 7)
    assert ("grove_zone_min", "<=", 6) in dom
    assert ("grove_layer", "=", "shrub") in dom
    assert ("grove_sun", "=", "partial") in dom


def test_unknown_layer_sun_ignored():
    assert product_domain.build_product_domain({"layer": "bogus", "sun": "midnight"}, 7) == BASE


def test_bad_values_ignored():
    assert product_domain.build_product_domain({"tag_id": "x", "zone": "?"}, 7) == BASE


def test_existing_filters_still_work():
    dom = product_domain.build_product_domain({"featured": "1", "category_id": "12", "slug": " Pear "}, 7)
    assert ("grove_featured", "=", True) in dom
    assert ("public_categ_ids", "in", [12]) in dom
    assert ("grove_slug", "=", "pear") in dom


def test_slugify():
    assert product_domain.slugify("Trees") == "trees"
    assert product_domain.slugify("Stone Fruit") == "stone-fruit"
    assert product_domain.slugify("  Nuts & Hardwood  ") == "nuts-hardwood"
    assert product_domain.slugify("") == ""
    assert product_domain.slugify(None) == ""


def test_cat_filter_with_resolved_ids():
    # The controller resolves the slug to website-category ids and passes them in.
    dom = product_domain.build_product_domain({"cat": "trees"}, 7, cat_category_ids=[2, 5])
    assert ("public_categ_ids", "in", [2, 5]) in dom


def test_cat_unknown_slug_returns_empty_set_not_whole_catalog():
    # An unrecognised slug resolves to [] -> [-1] so nothing matches (empty
    # state), rather than a bare `in []` which Odoo treats as "no constraint".
    dom = product_domain.build_product_domain({"cat": "bogus"}, 7, cat_category_ids=[])
    assert ("public_categ_ids", "in", [-1]) in dom


def test_no_cat_kwarg_adds_no_category_leaf():
    dom = product_domain.build_product_domain({}, 7, cat_category_ids=[2])
    assert not any(leaf[0] == "public_categ_ids" for leaf in dom)


def test_cat_combines_with_zone_and_layer():
    dom = product_domain.build_product_domain(
        {"cat": "vines", "zone": "6", "layer": "vine"}, 7, cat_category_ids=[3]
    )
    assert ("public_categ_ids", "in", [3]) in dom
    assert ("grove_zone_min", "<=", 6) in dom
    assert ("grove_layer", "=", "vine") in dom
