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
        # One reference parcel per catalog box across BOTH catalogs (bareroot
        # BOXES + potted POTTED_BOXES, GOL-2031), each quoted at its own
        # representative billable weight (never undercharge).
        self.assertEqual(set(rc.PARCELS), set(rc.shipping_boxes.BOXES) | set(rc.shipping_boxes.POTTED_BOXES))
        for box_id, parcel in rc.PARCELS.items():
            if box_id in rc.shipping_boxes.POTTED_BOXES:
                expected = rc.shipping_boxes.potted_representative_billable_lb(box_id)
            else:
                expected = rc.shipping_boxes.representative_billable_lb(box_id)
            self.assertEqual(float(parcel["weight"]), expected)

    def test_potted_boxes_probe_at_true_dims_and_calibrated_weight(self):
        # GOL-2031: potted boxes reach the probe at their real 24" length (so the
        # USPS nonstandard-length surcharge lands in the quote) and at the
        # bench-calibrated weight (p24x6 full = 8 lb per Josh's 2026-09-05 datum).
        self.assertEqual(rc.PARCELS["p24x6"]["length"], "24")
        self.assertEqual(rc.PARCELS["p24x9"]["length"], "24")
        self.assertEqual(float(rc.PARCELS["p24x6"]["weight"]), 8.0)

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
