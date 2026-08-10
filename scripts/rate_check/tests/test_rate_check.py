import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

_PATH = os.path.join(os.path.dirname(__file__), "..", "rate_check.py")
_spec = importlib.util.spec_from_file_location("rate_check", _PATH)
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "shippo_rates_response.json")
NO_UPS_FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "shippo_rates_no_ups.json")


class TestRateMath(unittest.TestCase):
    def test_ups_ground_rate_selected(self):
        with open(FIXTURE) as fh:
            data = json.load(fh)
        self.assertEqual(rc.pick_ups_ground(data), 14.23)

    def test_target_formula_ceil(self):
        # 14.23 + 4.50 (s20 packaging) + 2.00 = 20.73 -> 21
        self.assertEqual(rc.target_rate(14.23, "s20"), 21)

    def test_parcels_come_from_box_catalog(self):
        # One reference parcel per catalog box, quoted at representative
        # billable weight (never undercharge).
        self.assertEqual(set(rc.PARCELS), set(rc.shipping_boxes.BOXES))
        for box_id, parcel in rc.PARCELS.items():
            self.assertEqual(float(parcel["weight"]), rc.shipping_boxes.representative_billable_lb(box_id))

    def test_diff_detects_material_drift(self):
        current = {"zone_1": {"bareroot": {"base": 21.0}}}
        proposed = {"zone_1": {"bareroot": 20}}
        drift = rc.compute_drift(current, proposed)
        self.assertEqual(drift, [("zone_1", "bareroot", 21.0, 20)])

    def test_sub_dollar_drift_ignored(self):
        current = {"zone_1": {"bareroot": {"base": 20.4}}}
        drift = rc.compute_drift(current, {"zone_1": {"bareroot": 20}})
        self.assertEqual(drift, [])


class TestNoUpsRatesSkips(unittest.TestCase):
    def test_no_ups_rates_anywhere_skips_cleanly(self):
        # Shippo answers HTTP 200 but has no UPS carrier connected: every
        # probe returns no UPS Ground rate. The checker must skip cleanly
        # (exit 0) rather than fail — the daily job stays green until UPS is
        # connected (GOL-1296), and must not rewrite the rates file.
        with open(rc.RATES_PATH, encoding="utf-8") as fh:
            rates_before = fh.read()
        with mock.patch("sys.argv", ["rate_check.py", "--fixture", NO_UPS_FIXTURE]):
            out = io.StringIO()
            with redirect_stdout(out):
                code = rc.main()
        self.assertEqual(code, 0)
        self.assertIn("no UPS Ground rate for any probe", out.getvalue())
        with open(rc.RATES_PATH, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), rates_before)
