"""Pure test for the sweat-equity rate (no Odoo runtime).

Loaded by file path via pytest; not imported through grove_ops.tests (which is
stubbed in the root conftest). The setup_ops script carries the single source of
truth for the rate; this pins it from the module's side.
"""

import importlib.util
import os

_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "setup_ops.py")
_spec = importlib.util.spec_from_file_location("setup_ops_for_grove_ops", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_sweat_equity_rate_is_21():
    assert _mod.SWEAT_EQUITY_RATE == 21.00


def test_rate_has_no_gl_account_in_employee_specs():
    users = [{"name": "Pat", "login": "pat@grove.test", "company_keys": ["farm"]}]
    (spec,) = _mod.employee_specs(users)
    assert spec["hourly_cost"] == 21.00
    assert "account_id" not in spec
