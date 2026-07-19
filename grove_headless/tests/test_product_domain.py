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


def test_bad_values_ignored():
    assert product_domain.build_product_domain({"tag_id": "x", "zone": "?"}, 7) == BASE


def test_existing_filters_still_work():
    dom = product_domain.build_product_domain({"featured": "1", "category_id": "12", "slug": " Pear "}, 7)
    assert ("grove_featured", "=", True) in dom
    assert ("public_categ_ids", "in", [12]) in dom
    assert ("grove_slug", "=", "pear") in dom
