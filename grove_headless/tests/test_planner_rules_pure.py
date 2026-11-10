"""Pure-Python unit tests for the planner-learning helpers (GOL-3549 §B).

No Odoo runtime: loads grove_headless/services/planner/rules.py by file path
(the importlib pattern the other pure grove_headless tests use) and exercises
the rule defaults and every public-route body validator. The strict-400
contract of the routes lives entirely in these validators, so this is where it
is proven cheaply.
"""

import importlib.util
import os

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "services", "planner", "rules.py")
_spec = importlib.util.spec_from_file_location("grove_planner_rules", _MODULE_PATH)
rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rules)


def test_rule_defaults_are_the_shipped_constants():
    # Order and values the order-builder engine shipped with (spec B.1).
    assert tuple(rules.PLANNER_RULE_DEFAULTS[k] for k in rules.PLANNER_RULE_KEYS) == (
        2.0,
        1.5,
        40.0,
        4.0,
        10.0,
        10.0,
        3.0,
        2.0,
    )
    # Every key has a default; nothing extra.
    assert set(rules.PLANNER_RULE_DEFAULTS) == set(rules.PLANNER_RULE_KEYS)


def test_zone_band_for_zone():
    assert rules.zone_band_for_zone(3) == "3-5"
    assert rules.zone_band_for_zone(5) == "3-5"
    assert rules.zone_band_for_zone(6) == "6-7"
    assert rules.zone_band_for_zone(9) == "8-9"
    assert rules.zone_band_for_zone(2) is None
    assert rules.zone_band_for_zone(10) is None
    assert rules.zone_band_for_zone(None) is None


def test_validate_signal_requires_kind_and_browser_id():
    clean, err = rules.validate_signal({"kind": "removed", "product_tmpl_id": 7})
    assert err == "browser_id is required"
    clean, err = rules.validate_signal({"kind": "nope", "browser_id": "b1"})
    assert err and err.startswith("kind must be one of")


def test_validate_signal_product_required_except_built_and_saved():
    clean, err = rules.validate_signal({"kind": "removed", "browser_id": "b1"})
    assert err == "product_tmpl_id is required for kind removed"
    # built/saved are plan-level and need no product.
    clean, err = rules.validate_signal({"kind": "built", "browser_id": "b1"})
    assert err is None and clean["product_tmpl_id"] is None


def test_validate_signal_derives_band_from_zone():
    clean, err = rules.validate_signal({"kind": "added", "product_tmpl_id": 7, "browser_id": "b1", "zone": 7})
    assert err is None
    assert clean["zone_band"] == "6-7"
    assert clean["kind"] == "added"


def test_validate_signal_rejects_bad_band_and_bad_ints():
    _, err = rules.validate_signal({"kind": "added", "product_tmpl_id": 7, "browser_id": "b1", "zone_band": "nope"})
    assert err and err.startswith("zone_band must be one of")
    _, err = rules.validate_signal({"kind": "added", "product_tmpl_id": "x", "browser_id": "b1"})
    assert err == "product_tmpl_id must be an integer"


def test_validate_suggestion():
    _, err = rules.validate_suggestion({"stratum": "overstory", "browser_id": "b1"})
    assert err == "name is required"
    _, err = rules.validate_suggestion({"name": "Pawpaw", "stratum": "bogus", "browser_id": "b1"})
    assert err and err.startswith("stratum must be one of")
    clean, err = rules.validate_suggestion({"name": "Pawpaw", "stratum": "overstory", "browser_id": "b1"})
    assert err is None and clean["name"] == "Pawpaw" and clean["stratum"] == "overstory"


def test_validate_save_lines():
    _, err = rules.validate_save({"name": "My Grove", "browser_id": "b1", "lines": []})
    assert err == "lines must be a non-empty array"
    clean, err = rules.validate_save(
        {
            "name": "My Grove",
            "browser_id": "b1",
            "zone": 6,
            "lines": [{"product_tmpl_id": 7, "qty": 2, "role": "anchor", "stratum": "overstory"}],
        }
    )
    assert err is None
    assert clean["lines"][0] == {"product_tmpl_id": 7, "qty": 2.0, "role": "anchor", "stratum": "overstory"}
    # a non-positive qty is rejected
    _, err = rules.validate_save({"name": "x", "browser_id": "b1", "lines": [{"product_tmpl_id": 7, "qty": 0}]})
    assert err == "line qty must be positive"


def test_validate_dispute_and_propose():
    _, err = rules.validate_dispute({"fact": "zone_min"})
    assert err == "product_tmpl_id is required"
    clean, err = rules.validate_dispute({"product_tmpl_id": 7, "fact": "zone_min"})
    assert err is None and clean["contradicts"] is True
    _, err = rules.validate_dispute({"product_tmpl_id": 7, "fact": "bogus"})
    assert err and err.startswith("fact must be one of")

    _, err = rules.validate_propose({"key": "priority_weight"})
    assert err == "value must be a number"
    clean, err = rules.validate_propose({"key": "priority_weight", "value": "1.0"})
    assert err is None and clean["value"] == 1.0
    _, err = rules.validate_propose({"key": "nope", "value": 1})
    assert err and err.startswith("key must be one of")
