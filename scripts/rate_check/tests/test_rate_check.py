import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

_PATH = os.path.join(os.path.dirname(__file__), "..", "rate_check.py")
_spec = importlib.util.spec_from_file_location("rate_check", _PATH)
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "shippo_rates_response.json")
NO_UPS_FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "shippo_rates_no_ups.json")


class TestReferenceAddresses(unittest.TestCase):
    def test_reference_zips_carry_a_real_city(self):
        # Each reference destination must be (city, state, zip). A placeholder
        # city ("n/a") makes UPS hard-reject the probe once a real carrier is
        # connected ("111539 Invalid Destination Postal Code and City"),
        # dropping the UPS Ground rate and failing rate-check (GOL-1446).
        for zone, entry in rc.REFERENCE_ZIPS.items():
            self.assertEqual(len(entry), 3, f"{zone}: expected (city, state, zip)")
            city, state, zip5 = entry
            self.assertTrue(city and city.strip().lower() != "n/a", f"{zone}: bad city {city!r}")
            self.assertEqual(len(zip5), 5, f"{zone}: bad zip {zip5!r}")

    def test_probe_sends_the_zone_city_not_a_placeholder(self):
        captured = {}

        def fake_post(url, json=None, timeout=None, headers=None):
            captured["city"] = json["address_to"]["city"]

            class _R:
                @staticmethod
                def raise_for_status():
                    pass

                @staticmethod
                def json():
                    return {"rates": []}

            return _R()

        with mock.patch.object(rc.requests, "post", fake_post):
            rc.quote_zone_box("k", "zone_2", next(iter(rc.PARCELS)))
        # zone_2 reference is the band's worst-case (priciest) corner, NYC,
        # not a mid-band representative like Columbus (GOL-1495).
        self.assertEqual(captured["city"], "New York")


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
        # probe returns no UPS Ground rate. Because the shipped rates file is
        # still the provisional placeholder (`_provisional: true`), there are
        # no real published rates to protect, so the checker must skip cleanly
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

    def test_shipped_rates_file_is_marked_provisional(self):
        # The clean-skip above is only correct while the shipped table is the
        # launch-hypothesis placeholder. Once the checker publishes real rates
        # it drops the marker, and all-missing becomes a lapse failure.
        with open(rc.RATES_PATH, encoding="utf-8") as fh:
            self.assertTrue(json.load(fh).get("_provisional"))

    def _run_no_ups_against(self, doc):
        """Run the all-missing path against a rates file containing `doc`.

        Returns (exit_code, stderr, rates_after). Uses a temp RATES_PATH so
        the real shipped table is never touched.
        """
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(doc, fh)
            path = fh.name
        try:
            argv = ["rate_check.py", "--fixture", NO_UPS_FIXTURE]
            with mock.patch.object(rc, "RATES_PATH", path), mock.patch("sys.argv", argv):
                err = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    code = rc.main()
            with open(path, encoding="utf-8") as fh:
                after = fh.read()
            return code, err.getvalue(), after
        finally:
            os.unlink(path)

    def test_all_missing_with_real_published_rates_fails(self):
        # Real published rates exist (no `_provisional` marker) but Shippo now
        # returns zero UPS Ground rates for every probe: the carrier link has
        # lapsed. The checker must FAIL (exit 1) so the fossilized table gets
        # investigated, and must not rewrite the file (GOL-1312).
        real = {
            "_comment": "Maintained by scripts/rate_check",
            "_schema": 2,
            "zone_1": {"br16": {"base": 18.0}},
        }
        before = json.dumps(real)
        code, err, after = self._run_no_ups_against(real)
        self.assertEqual(code, 1)
        self.assertIn("UPS connection lost", err)
        self.assertEqual(after, before)

    def test_all_missing_with_empty_table_skips_cleanly(self):
        # An empty (or fully placeholder-stripped) table has no real rates to
        # protect, so all-missing is still the not-ready state — skip cleanly.
        code, _err, _after = self._run_no_ups_against({"_comment": "x", "_schema": 2})
        self.assertEqual(code, 0)
