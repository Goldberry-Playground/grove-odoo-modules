import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from unittest import mock

_PATH = os.path.join(os.path.dirname(__file__), "..", "rate_check.py")
_spec = importlib.util.spec_from_file_location("rate_check", _PATH)
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

_FX = os.path.join(os.path.dirname(__file__), "..", "fixtures")
# Real 2026-09-09-shape Pirate Ship RatesQuery captures, one per box shape.
SMALL_FIXTURE = os.path.join(_FX, "pirateship_rates_small.json")
LARGE_FIXTURE = os.path.join(_FX, "pirateship_rates_large.json")
P4_FIXTURE = os.path.join(_FX, "pirateship_rates_p24x10x4.json")
P6_FIXTURE = os.path.join(_FX, "pirateship_rates_p24x10x6.json")
# Calculator answered but returned no allowlisted ground rate (lapse / unpriced).
NO_GROUND_FIXTURE = os.path.join(_FX, "pirateship_rates_no_ground.json")

# Fixed probe date the captured fixtures were quoted against (delivery dates in
# them are 9/16, 9/17 — 2 and 3 days out), so transit math is deterministic.
PROBE_DATE = date(2026, 9, 14)


def _rates(path):
    with open(path, encoding="utf-8") as fh:
        return rc.rates_from_response(json.load(fh))


class TestReferenceAddresses(unittest.TestCase):
    def test_reference_zips_carry_a_real_city(self):
        for zone, corners in rc.REFERENCE_ZIPS.items():
            self.assertTrue(corners, f"{zone}: at least one reference corner")
            for entry in corners:
                self.assertEqual(len(entry), 3, f"{zone}: expected (city, state, zip)")
                city, state, zip5 = entry
                self.assertTrue(city and city.strip().lower() != "n/a", f"{zone}: bad city {city!r}")
                self.assertEqual(len(zip5), 5, f"{zone}: bad zip {zip5!r}")


class TestRequestShape(unittest.TestCase):
    def test_probe_posts_pirateship_ratesquery_in_ounces(self):
        captured = {}

        def fake_post(url, json=None, timeout=None, headers=None):
            captured["url"] = url
            captured["body"] = json

            class _R:
                @staticmethod
                def raise_for_status():
                    pass

                @staticmethod
                def json():
                    return {"data": {"rates": []}}

            return _R()

        with mock.patch.object(rc.requests, "post", fake_post):
            rc.quote_zone_box("zone_2", "small", PROBE_DATE)

        self.assertEqual(captured["url"], rc.PIRATESHIP_URL)
        body = captured["body"]
        self.assertEqual(body["operationName"], "RatesQuery")
        v = body["variables"]
        # zone_2 reference is the band's worst-case corner, NYC (GOL-1495).
        self.assertEqual(v["destinationZip"], "10001")
        self.assertEqual(v["originZip"], "26651")
        self.assertTrue(v["isResidential"])
        self.assertEqual(v["destinationCountryCode"], "US")
        self.assertEqual(v["mailClassKeys"], ["03", "93", "GroundAdvantage"])
        self.assertEqual(v["packageTypeKeys"], ["Parcel"])
        # small box: 24x6x4 at 7 lb representative -> weight in OUNCES.
        self.assertEqual(v["weight"], 112.0)
        self.assertEqual((v["dimensionX"], v["dimensionY"], v["dimensionZ"]), (24, 6, 4))

    def test_parcels_come_from_box_catalog(self):
        # GOL-2199: BOTH catalogs publish (bareroot BOXES + potted POTTED_BOXES),
        # each quoted at its own catalog's representative billable weight.
        self.assertEqual(
            set(rc.PARCELS),
            set(rc.shipping_boxes.BOXES) | set(rc.shipping_boxes.POTTED_BOXES),
        )
        for box_id, parcel in rc.PARCELS.items():
            if box_id in rc.shipping_boxes.POTTED_BOXES:
                expected = rc.shipping_boxes.potted_representative_billable_lb(box_id)
            else:
                expected = rc.shipping_boxes.representative_billable_lb(box_id)
            self.assertEqual(parcel["weight_lb"], expected)


class TestTransit(unittest.TestCase):
    def test_parses_delivery_date_to_days(self):
        desc = "Estimated delivery [b]Wednesday 9/16 by 11:00 PM[/b] if shipped today"
        self.assertEqual(rc.parse_transit_days(desc, PROBE_DATE), 2)

    def test_unparsable_date_is_none_not_excluded(self):
        self.assertIsNone(rc.parse_transit_days("Estimated delivery in 1-5 business days", PROBE_DATE))
        self.assertIsNone(rc.parse_transit_days("", PROBE_DATE))
        self.assertIsNone(rc.parse_transit_days(None, PROBE_DATE))

    def test_year_rolls_forward_for_past_month(self):
        # A December probe of an early-January delivery date rolls to next year.
        self.assertEqual(rc.parse_transit_days("delivery 1/3", date(2026, 12, 30)), 4)


class TestWinnerSelection(unittest.TestCase):
    def test_every_captured_fixture_yields_an_allowlisted_winner(self):
        # Each real per-box capture must select an allowlisted (carrier, service)
        # winner at a positive price — proves the captured RatesQuery shape parses.
        for path in (SMALL_FIXTURE, LARGE_FIXTURE, P4_FIXTURE, P6_FIXTURE):
            winner = rc.select_cheapest_ground(_rates(path), PROBE_DATE)
            self.assertIsNotNone(winner, path)
            self.assertIn((winner["carrier"], winner["service"]), rc.GROUND_SERVICE_ALLOWLIST, path)
            self.assertGreater(winner["price"], 0.0, path)

    def test_cheapest_allowlisted_ground_wins(self):
        # small fixture: UPS Ground 03 $9.84 < UPS Ground Saver 93 $11.75 <
        # USPS GroundAdvantage $14.35 -> cheapest UPS Ground wins.
        winner = rc.select_cheapest_ground(_rates(SMALL_FIXTURE), PROBE_DATE)
        self.assertEqual((winner["carrier"], winner["service"]), ("UPS", "03"))
        self.assertEqual(winner["price"], 9.84)
        self.assertEqual(winner["service_title"], "UPS Ground")  # ® stripped

    def test_ground_saver_can_win_on_the_big_box(self):
        # p24x10x6 fixture: Ground Saver 93 $19.85 edges out UPS Ground 03 $19.89
        # — the reason "93" is in the allowlist at all.
        winner = rc.select_cheapest_ground(_rates(P6_FIXTURE), PROBE_DATE)
        self.assertEqual((winner["carrier"], winner["service"]), ("UPS", "93"))
        self.assertEqual(winner["price"], 19.85)

    def test_transit_ceiling_excludes_too_slow_cheapest(self):
        # Cheapest is 10 days out (> ceiling 7); a $2-dearer rate delivers in 3.
        rates = [
            {
                "carrier": {"title": "UPS"},
                "mailClassKey": "93",
                "totalPrice": "8.00",
                "title": "UPS Ground Saver",
                "deliveryDescription": "delivery 9/24",
            },  # 10 days
            {
                "carrier": {"title": "UPS"},
                "mailClassKey": "03",
                "totalPrice": "10.00",
                "title": "UPS Ground",
                "deliveryDescription": "delivery 9/17",
            },  # 3 days
        ]
        winner = rc.select_cheapest_ground(rates, PROBE_DATE)
        self.assertEqual(winner["service"], "03")
        self.assertEqual(winner["price"], 10.00)

    def test_fastest_wins_when_none_within_ceiling(self):
        # No allowlisted rate fits the ceiling -> fastest known wins (ties cheapest),
        # so a slow week never strands an order unshippable (GOL-1906).
        rates = [
            {
                "carrier": {"title": "UPS"},
                "mailClassKey": "03",
                "totalPrice": "20.00",
                "title": "UPS Ground",
                "deliveryDescription": "delivery 9/30",
            },  # 16 days
            {
                "carrier": {"title": "USPS"},
                "mailClassKey": "GroundAdvantage",
                "totalPrice": "25.00",
                "title": "Ground Advantage",
                "deliveryDescription": "delivery 9/25",
            },  # 11 days
        ]
        winner = rc.select_cheapest_ground(rates, PROBE_DATE)
        self.assertEqual(winner["service"], "GroundAdvantage")

    def test_unknown_transit_is_not_excluded(self):
        rates = [
            {
                "carrier": {"title": "UPS"},
                "mailClassKey": "03",
                "totalPrice": "12.00",
                "title": "UPS Ground",
                "deliveryDescription": "delivery in a few days",
            },
        ]
        winner = rc.select_cheapest_ground(rates, PROBE_DATE)
        self.assertEqual(winner["price"], 12.00)
        self.assertIsNone(winner["transit_days"])

    def test_non_allowlisted_service_ignored(self):
        rates = [
            {
                "carrier": {"title": "UPS"},
                "mailClassKey": "01",
                "totalPrice": "5.00",
                "title": "UPS Next Day Air",
                "deliveryDescription": "delivery 9/15",
            },
        ]
        self.assertIsNone(rc.select_cheapest_ground(rates, PROBE_DATE))


class TestRateMath(unittest.TestCase):
    def test_target_formula_ceil(self):
        # 9.84 + 3.50 (small packaging) + 2.00 = 15.34 -> 16
        self.assertEqual(rc.target_rate(9.84, "small"), 16)

    def test_diff_detects_material_drift(self):
        current = {"zone_1": {"bareroot": {"base": 21.0}}}
        proposed = {"zone_1": {"bareroot": {"base": 20.0}}}
        self.assertEqual(rc.compute_drift(current, proposed), [("zone_1", "bareroot", 21.0, 20.0)])

    def test_diff_accepts_bare_number_cells(self):
        current = {"zone_1": {"bareroot": {"base": 21.0}}}
        self.assertEqual(rc.compute_drift(current, {"zone_1": {"bareroot": 20}}), [("zone_1", "bareroot", 21.0, 20)])

    def test_sub_dollar_drift_ignored(self):
        current = {"zone_1": {"bareroot": {"base": 20.4}}}
        self.assertEqual(rc.compute_drift(current, {"zone_1": {"bareroot": {"base": 20.0}}}), [])


class TestCarrierVisibility(unittest.TestCase):
    def test_present_services_reports_all_three_allowlisted(self):
        self.assertEqual(
            rc.present_services(_rates(SMALL_FIXTURE), PROBE_DATE),
            {("UPS", "03"), ("UPS", "93"), ("USPS", "GroundAdvantage")},
        )

    def test_present_services_empty_when_no_ground_returned(self):
        self.assertEqual(rc.present_services(_rates(NO_GROUND_FIXTURE), PROBE_DATE), set())

    def test_visibility_report_flags_absent_service(self):
        report = rc.visibility_report({("USPS", "usps_ground_advantage"): 0, ("UPS", "03"): 5}, 5)
        self.assertIn("UPS 03 (UPS Ground): 5/5", report)
        self.assertIn("NEVER RETURNED", report)

    def test_quote_zone_box_returns_winner_and_present(self):
        def fake_post(url, json=None, timeout=None, headers=None):
            class _R:
                @staticmethod
                def raise_for_status():
                    pass

                @staticmethod
                def json():
                    return {
                        "data": {
                            "rates": [
                                {
                                    "carrier": {"title": "USPS"},
                                    "mailClassKey": "GroundAdvantage",
                                    "totalPrice": "12.74",
                                    "title": "Ground Advantage",
                                    "deliveryDescription": "delivery 9/17",
                                },
                            ]
                        }
                    }

            return _R()

        with mock.patch.object(rc.requests, "post", fake_post):
            winner, present = rc.quote_zone_box("zone_4", "small", PROBE_DATE)
        self.assertEqual(winner["price"], 12.74)
        self.assertEqual((winner["carrier"], winner["service"]), ("USPS", "GroundAdvantage"))
        self.assertEqual(present, {("USPS", "GroundAdvantage")})

    def test_quote_zone_box_publishes_max_across_corners(self):
        # zone_5 has 4 corners; each returns a different UPS Ground price.
        prices = iter(["10.00", "18.00", "12.00", "15.00"])

        def fake_post(url, json=None, timeout=None, headers=None):
            amount = next(prices)

            class _R:
                @staticmethod
                def raise_for_status():
                    pass

                @staticmethod
                def json():
                    return {
                        "data": {
                            "rates": [
                                {
                                    "carrier": {"title": "UPS"},
                                    "mailClassKey": "03",
                                    "totalPrice": amount,
                                    "title": "UPS Ground",
                                    "deliveryDescription": "delivery 9/18",
                                },
                            ]
                        }
                    }

            return _R()

        with mock.patch.object(rc.requests, "post", fake_post):
            winner, _present = rc.quote_zone_box("zone_5", "small", PROBE_DATE)
        # MAX across corners -> $18.00 sets the published (upper-bound) rate.
        self.assertEqual(winner["price"], 18.00)

    def test_quote_zone_box_skips_graphql_error_corner(self):
        # First of zone_5's four corners errors (GraphQL errors[]); the run
        # continues and prices from the remaining corners (max wins).
        def _priced(amount):
            return {
                "data": {
                    "rates": [
                        {
                            "carrier": {"title": "UPS"},
                            "mailClassKey": "03",
                            "totalPrice": amount,
                            "title": "UPS Ground",
                            "deliveryDescription": "delivery 9/18",
                        },
                    ]
                }
            }

        responses = iter(
            [
                {"errors": [{"message": "bad zip"}]},
                _priced("13.00"),
                _priced("11.00"),
                _priced("12.00"),
            ]
        )

        def fake_post(url, json=None, timeout=None, headers=None):
            payload = next(responses)

            class _R:
                @staticmethod
                def raise_for_status():
                    pass

                @staticmethod
                def json():
                    return payload

            return _R()

        with mock.patch.object(rc.requests, "post", fake_post):
            err = io.StringIO()
            with redirect_stderr(err):
                winner, _present = rc.quote_zone_box("zone_5", "small", PROBE_DATE)
        self.assertEqual(winner["price"], 13.00)
        self.assertIn("pirateship error", err.getvalue())


class TestSchemaThreeAndWrite(unittest.TestCase):
    def test_full_run_writes_schema_3_cells(self):
        # Offline full-table dry-run via captured per-box fixtures, written to a
        # temp rates file so the real table is untouched. Proves the schema-3
        # cell shape and that the loader-visible `base` is present.
        empty = {"_comment": "seed", "_schema": 2}  # empty -> everything drifts
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(empty, fh)
            path = fh.name
        out_dir = tempfile.mkdtemp()
        try:
            argv = ["--fixture-dir", _FX]
            with mock.patch.object(rc, "RATES_PATH", path), mock.patch.object(rc, "OUT_DIR", out_dir):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = rc.main(argv)
            self.assertEqual(code, 3)  # rewritten
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["_schema"], 3)
            cell = doc["zone_1"]["small"]
            self.assertEqual(set(cell), {"base", "carrier", "service", "service_title"})
            self.assertIsInstance(cell["base"], float)
            self.assertEqual(cell["carrier"], "UPS")
            self.assertEqual(cell["service"], "03")
            self.assertEqual(cell["service_title"], "UPS Ground")
            # small: ceil(9.84 + 3.50 + 2.00) = 16
            self.assertEqual(cell["base"], 16.0)
        finally:
            os.unlink(path)

    def test_dry_run_does_not_write(self):
        empty = {"_comment": "seed", "_schema": 2}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(empty, fh)
            path = fh.name
        before = open(path, encoding="utf-8").read()
        try:
            argv = ["--dry-run", "--fixture-dir", _FX]
            with mock.patch.object(rc, "RATES_PATH", path):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = rc.main(argv)
            self.assertEqual(code, 0)
            self.assertEqual(open(path, encoding="utf-8").read(), before)
        finally:
            os.unlink(path)


class TestNoGroundHandling(unittest.TestCase):
    def _run_no_ground_against(self, doc):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(doc, fh)
            path = fh.name
        try:
            argv = ["--fixture", NO_GROUND_FIXTURE]
            with mock.patch.object(rc, "RATES_PATH", path):
                err = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    code = rc.main(argv)
            return code, err.getvalue(), open(path, encoding="utf-8").read()
        finally:
            os.unlink(path)

    def test_all_missing_with_real_published_rates_fails(self):
        real = {"_comment": "x", "_schema": 3, "zone_1": {"small": {"base": 18.0}}}
        before = json.dumps(real)
        code, err, after = self._run_no_ground_against(real)
        self.assertEqual(code, 1)
        self.assertIn("Pirate Ship quote source down", err)
        self.assertEqual(after, before)

    def test_all_missing_with_empty_table_skips_cleanly(self):
        code, _err, _after = self._run_no_ground_against({"_comment": "x", "_schema": 3})
        self.assertEqual(code, 0)

    def test_no_ground_against_real_shipped_file_fails(self):
        # The shipped file holds real published rates (no `_provisional`), so an
        # all-missing result is a lapse -> exit 1, file untouched.
        with open(rc.RATES_PATH, encoding="utf-8") as fh:
            rates_before = fh.read()
        with mock.patch("sys.argv", ["rate_check.py", "--fixture", NO_GROUND_FIXTURE]):
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = rc.main()
        self.assertEqual(code, 1)
        self.assertIn("Pirate Ship quote source down", err.getvalue())
        with open(rc.RATES_PATH, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), rates_before)


class TestShippedRatesFile(unittest.TestCase):
    def test_shipped_rates_file_holds_real_published_rates(self):
        with open(rc.RATES_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertNotIn("_provisional", doc)
        zones = sorted(k for k in doc if not k.startswith("_"))
        # zone_6 / zone_7 are GOL-2238's real probe-derived mid/near-plains bands.
        self.assertEqual(zones, ["zone_1", "zone_2", "zone_3", "zone_4", "zone_5", "zone_6", "zone_7"])
        for zone in zones:
            self.assertTrue(doc[zone], f"{zone}: expected per-box rates")


if __name__ == "__main__":
    unittest.main()
