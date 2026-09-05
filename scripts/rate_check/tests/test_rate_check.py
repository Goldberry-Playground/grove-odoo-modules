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
# No allowlisted ground rate at all (only a non-ground service present) — the
# "carrier not connected / lapsed" state under least-cost selection (GOL-1906).
NO_GROUND_FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "shippo_rates_no_ground.json")


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
    def test_cheapest_ground_rate_selected(self):
        # Fixture: UPS Ground 14.23 vs USPS Ground Advantage 15.80 -> cheaper
        # UPS wins the least-cost race (GOL-1906).
        with open(FIXTURE) as fh:
            data = json.load(fh)
        self.assertEqual(rc.pick_cheapest_ground(data), 14.23)

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


class TestCarrierVisibility(unittest.TestCase):
    def test_present_carriers_reports_both_allowlisted_carriers(self):
        # Fixture carries UPS Ground + USPS Ground Advantage (and a non-ground
        # UPS 3-Day, which must be ignored). Visibility is independent of who
        # wins on price (GOL-1906).
        with open(FIXTURE) as fh:
            data = json.load(fh)
        self.assertEqual(
            rc.present_carriers(data),
            {("UPS", "ups_ground"), ("USPS", "usps_ground_advantage")},
        )

    def test_present_carriers_empty_when_no_ground_returned(self):
        with open(NO_GROUND_FIXTURE) as fh:
            data = json.load(fh)
        self.assertEqual(rc.present_carriers(data), set())

    def test_visibility_report_flags_absent_carrier(self):
        # USPS returned on every probe, UPS on none -> UPS flagged as never
        # returned. This is the readout that proves whether a carrier reaches
        # the automation's token (GOL-1906 CEO ruling step (b)).
        report = rc.visibility_report({("USPS", "usps_ground_advantage"): 5}, 5)
        self.assertIn("USPS usps_ground_advantage: 5/5", report)
        self.assertIn("UPS ups_ground: 0/5", report)
        self.assertIn("NEVER RETURNED", report)

    def test_quote_zone_box_returns_quote_and_present_carriers(self):
        def fake_post(url, json=None, timeout=None, headers=None):
            class _R:
                @staticmethod
                def raise_for_status():
                    pass

                @staticmethod
                def json():
                    return {
                        "rates": [
                            {"provider": "USPS", "servicelevel": {"token": "usps_ground_advantage"}, "amount": "12.74"},
                        ]
                    }

            return _R()

        with mock.patch.object(rc.requests, "post", fake_post):
            quote, present = rc.quote_zone_box("k", "zone_4", next(iter(rc.PARCELS)))
        self.assertEqual(quote, 12.74)
        self.assertEqual(present, {("USPS", "usps_ground_advantage")})


class TestNoUpsRatesSkips(unittest.TestCase):
    def test_no_ups_against_real_shipped_file_fails(self):
        # GOL-1495 published the real UPS Ground table, so the shipped file no
        # longer carries the `_provisional` placeholder marker. An all-missing
        # Shippo result against that real file is therefore a carrier lapse,
        # not the not-ready state: the checker must FAIL (exit 1) so a fossilized
        # table gets investigated, and must not rewrite the file (GOL-1312).
        with open(rc.RATES_PATH, encoding="utf-8") as fh:
            rates_before = fh.read()
        with mock.patch("sys.argv", ["rate_check.py", "--fixture", NO_GROUND_FIXTURE]):
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = rc.main()
        self.assertEqual(code, 1)
        self.assertIn("ground carrier connection lost", err.getvalue())
        with open(rc.RATES_PATH, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), rates_before)

    def test_shipped_rates_file_holds_real_published_rates(self):
        # GOL-1495 published the real UPS Ground table: the launch-hypothesis
        # `_provisional` marker is gone and every zone carries per-box rates.
        # Dropping that marker is what flips the all-missing path from a clean
        # skip to the lapse failure asserted above.
        with open(rc.RATES_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertNotIn("_provisional", doc)
        zones = sorted(k for k in doc if not k.startswith("_"))
        self.assertEqual(zones, ["zone_1", "zone_2", "zone_3", "zone_4", "zone_5"])
        for zone in zones:
            self.assertTrue(doc[zone], f"{zone}: expected per-box rates")

    def _run_no_ups_against(self, doc):
        """Run the all-missing path against a rates file containing `doc`.

        Returns (exit_code, stderr, rates_after). Uses a temp RATES_PATH so
        the real shipped table is never touched.
        """
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(doc, fh)
            path = fh.name
        try:
            argv = ["rate_check.py", "--fixture", NO_GROUND_FIXTURE]
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
        # returns zero ground rates for every probe: the carrier link(s) have
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
        self.assertIn("ground carrier connection lost", err)
        self.assertEqual(after, before)

    def test_all_missing_with_empty_table_skips_cleanly(self):
        # An empty (or fully placeholder-stripped) table has no real rates to
        # protect, so all-missing is still the not-ready state — skip cleanly.
        code, _err, _after = self._run_no_ups_against({"_comment": "x", "_schema": 2})
        self.assertEqual(code, 0)
