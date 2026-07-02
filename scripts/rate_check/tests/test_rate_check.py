import importlib.util
import json
import os
import unittest

_PATH = os.path.join(os.path.dirname(__file__), "..", "rate_check.py")
_spec = importlib.util.spec_from_file_location("rate_check", _PATH)
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "shippo_rates_response.json")


class TestRateMath(unittest.TestCase):
    def test_ups_ground_rate_selected(self):
        with open(FIXTURE) as fh:
            data = json.load(fh)
        self.assertEqual(rc.pick_ups_ground(data), 14.23)

    def test_target_formula_ceil(self):
        # 14.23 + 3.50 + 2.00 = 19.73 -> 20
        self.assertEqual(rc.target_rate(14.23), 20)

    def test_diff_detects_material_drift(self):
        current = {"zone_1": {"bareroot": {"base": 21.0}}}
        proposed = {"zone_1": {"bareroot": 20}}
        drift = rc.compute_drift(current, proposed)
        self.assertEqual(drift, [("zone_1", "bareroot", 21.0, 20)])

    def test_sub_dollar_drift_ignored(self):
        current = {"zone_1": {"bareroot": {"base": 20.4}}}
        drift = rc.compute_drift(current, {"zone_1": {"bareroot": 20}})
        self.assertEqual(drift, [])
