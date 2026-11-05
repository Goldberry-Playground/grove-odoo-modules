"""Pure tests for the Schedule F account and line map (no Odoo).

Loaded by file path so the module under test never imports the Odoo package.
"""

import importlib.util
import os
import re

_PATH = os.path.join(os.path.dirname(__file__), "..", "schedule_f_lines.py")
_spec = importlib.util.spec_from_file_location("schedule_f_lines", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_every_schedule_f_line_has_at_least_one_account():
    for line in mod.LINE_ORDER:
        assert mod.accounts_for_line(line), line


def test_line_order_is_form_order_without_duplicates():
    assert mod.LINE_ORDER[0] == "1a"
    assert mod.LINE_ORDER[-1] == "32"
    assert len(mod.LINE_ORDER) == len(set(mod.LINE_ORDER))
    assert mod.LINE_ORDER == sorted(mod.LINE_ORDER, key=mod.sort_key)


def test_codes_are_unique_and_four_digits():
    codes = [a["code"] for a in mod.ACCOUNTS]
    assert len(codes) == len(set(codes))
    assert all(len(c) == 4 and c.isdigit() for c in codes)


def test_every_account_line_is_a_known_schedule_f_line_or_none():
    for a in mod.ACCOUNTS:
        assert a["line"] is None or a["line"] in mod.LINE_ORDER, a


def test_income_lines_use_income_types_and_expenses_use_expense_types():
    for a in mod.ACCOUNTS:
        if a["kind"] == "income":
            assert a["account_type"] in {"income", "income_other"}, a
        if a["kind"] == "expense":
            assert a["account_type"] in {"expense", "expense_direct_cost", "expense_depreciation"}, a
        if a["kind"] == "balance":
            assert a["line"] is None, a


def test_no_account_has_a_line_number_inside_its_name():
    for a in mod.ACCOUNTS:
        assert not re.search(r"\bline\b", a["name"], re.IGNORECASE), a["name"]
        assert "—" not in a["name"], a["name"]


def test_sort_key_orders_sub_lines():
    assert mod.sort_key("21b") == (21, "b")
    assert mod.sort_key("8") == (8, "")
    assert mod.sort_key(None) == (999, "")
    assert sorted(["24b", "8", "1a", "24a"], key=mod.sort_key) == ["1a", "8", "24a", "24b"]


def test_accounts_for_line_returns_only_that_line():
    assert [a["code"] for a in mod.accounts_for_line("21a")] == ["6210"]
    assert {a["code"] for a in mod.accounts_for_line("32")} == {"6320", "6321"}
    assert mod.accounts_for_line("9") == []


def test_specific_codes():
    assert mod.account_by_code("4020")["line"] == "2"
    assert mod.account_by_code("6280")["line"] == "28"
    assert mod.account_by_code("6321")["line"] == "32"
    assert mod.account_by_code("3100")["kind"] == "balance"
    assert mod.account_by_code("1350")["account_type"] == "asset_current"
    assert mod.account_by_code("2900")["account_type"] == "liability_credit_card"
    assert mod.account_by_code("4015")["kind"] == "expense"
    assert mod.account_by_code("4015")["account_type"] == "expense_direct_cost"
    assert mod.account_by_code("4015")["line"] == "1b"


def test_unknown_code_raises_key_error():
    try:
        mod.account_by_code("9999")
    except KeyError:
        return
    raise AssertionError("expected KeyError for unknown code")


def test_module_does_not_import_odoo():
    with open(_PATH, encoding="utf-8") as fh:
        source = fh.read()
    assert "import odoo" not in source
    assert "from odoo" not in source
