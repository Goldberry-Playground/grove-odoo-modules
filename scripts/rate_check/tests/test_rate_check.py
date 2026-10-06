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
# GOL-2605: the verbatim body Pirate Ship's public endpoint returns (HTTP 200)
# for every ad-hoc query since it switched to persisted-queries-only.
PERSISTED_ONLY_FIXTURE = os.path.join(_FX, "pirateship_rates_persisted_only.json")

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
        # GOL-2923 (Josh 2026-10-06): the published cell is the RAW CARRIER cost
        # rounded up to the whole dollar — NO handling folded in (handling is one
        # flat fee added once per order by the app, not per box). 9.84 -> 10.
        self.assertEqual(rc.target_rate(9.84), 10)
        self.assertEqual(rc.target_rate(10.0), 10)  # already whole -> unchanged
        self.assertEqual(rc.target_rate(11.01), 12)

    def test_target_cell_carries_no_handling(self):
        # The table must NOT bake the S&H fee into a per-box cell (that would
        # double-charge a 2-box order). target_rate adds nothing beyond ceil, so
        # it is exactly the carrier quote rounded up — regardless of the fee.
        fee = rc.shipping_boxes.SHIPPING_HANDLING_FEE
        self.assertEqual(fee, 5.00)  # the one flat fee still lives in the catalog
        self.assertEqual(rc.target_rate(12.00), 12)  # 12.00 carrier, no + fee
        self.assertEqual(rc.target_rate(12.01), 13)  # round-up only

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
        # zone_5 has 9 corners (GOL-2238 folded AR/MO/IA back in; GOL-2235 added
        # FL's Miami + Key West); each returns a different UPS Ground price.
        prices = iter(["10.00", "18.00", "12.00", "15.00", "9.00", "11.00", "14.00", "13.00", "16.00"])

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
        # First of zone_5's nine corners errors (GraphQL errors[]); the run
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
                _priced("10.00"),
                _priced("9.00"),
                _priced("8.00"),
                _priced("7.50"),
                _priced("7.00"),
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
            argv = ["--fixture-dir", _FX, "--probe-date", PROBE_DATE.isoformat()]
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
            # small: ceil(9.84) = 10 — raw carrier cost only, no handling in the
            # cell (GOL-2923, Josh 2026-10-06: handling is added once per order).
            self.assertEqual(cell["base"], 10.0)
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


class TestQuoteSourceRefused(unittest.TestCase):
    """GOL-2605 — "the endpoint refused our query" vs "the carrier went quiet".

    Pirate Ship turned on Apollo persisted-queries-only on 2026-09-29, so every
    probe corner raises and the run lands in the same all-missing branch a real
    carrier lapse would. These pin the two apart: the refusal must be NAMED (no
    retry or workflow change can fix it), and it must NOT invent an alarm while
    the table is still the provisional placeholder."""

    def test_is_source_closed_recognizes_persisted_query_refusals(self):
        self.assertTrue(rc.is_source_closed("This server only executes persisted queries."))
        self.assertTrue(rc.is_source_closed("PersistedQueryNotFound"))
        self.assertTrue(rc.is_source_closed("PersistedQueryNotSupported"))

    def test_is_source_closed_ignores_ordinary_quote_errors(self):
        # A carrier/validation error is NOT a refusal — it must keep the
        # existing "quote source down?" wording so the two never blur.
        self.assertFalse(rc.is_source_closed("destinationZip is invalid"))
        self.assertFalse(rc.is_source_closed("502 Bad Gateway"))
        self.assertFalse(rc.is_source_closed(None))

    def test_quote_zone_box_records_probe_errors_in_diagnostics(self):
        def fake_post(url, json=None, timeout=None, headers=None):
            class _R:
                @staticmethod
                def raise_for_status():
                    pass

                @staticmethod
                def json():
                    return {"errors": [{"message": "This server only executes persisted queries."}]}

            return _R()

        diagnostics = []
        with mock.patch.object(rc.requests, "post", fake_post):
            err = io.StringIO()
            with redirect_stderr(err):
                winner, present = rc.quote_zone_box("zone_2", "small", PROBE_DATE, diagnostics=diagnostics)
        self.assertIsNone(winner)
        self.assertEqual(present, set())
        self.assertTrue(diagnostics, "the refusal text must reach the caller")
        self.assertTrue(all(rc.is_source_closed(d) for d in diagnostics))

    def _run_persisted_only_against(self, doc):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(doc, fh)
            path = fh.name
        try:
            argv = ["--fixture", PERSISTED_ONLY_FIXTURE]
            with mock.patch.object(rc, "RATES_PATH", path):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = rc.main(argv)
            return code, out.getvalue(), err.getvalue(), open(path, encoding="utf-8").read()
        finally:
            os.unlink(path)

    def test_refusal_against_real_rates_names_the_closed_source(self):
        real = {"_comment": "x", "_schema": 3, "zone_1": {"small": {"base": 18.0}}}
        before = json.dumps(real)
        code, out, err, after = self._run_persisted_only_against(real)
        self.assertEqual(code, 1)
        # Annotated so the diagnosis is visible in the Actions run summary even
        # with no Discord webhook provisioned.
        self.assertIn("::error::", out)
        self.assertIn("REFUSED", out)
        self.assertIn("persisted", out)
        self.assertIn("quote source closed, not lapsed", err)
        # Never the misleading lapse wording, which would send triage at the
        # Pirate Ship *account* instead of at the missing quote source.
        self.assertNotIn("Pirate Ship quote source down", err)
        # The money file is untouched — a refusal must never publish or zero.
        self.assertEqual(after, before)

    def test_refusal_against_provisional_table_still_skips_cleanly(self):
        # No real published rates to protect => the pre-launch "not ready" state
        # wins over the refusal alarm (GOL-1312 clean-skip preserved).
        code, _out, _err, _after = self._run_persisted_only_against(
            {"_comment": "x", "_schema": 3, "_provisional": True, "zone_1": {"small": {"base": 1.0}}}
        )
        self.assertEqual(code, 0)


def _manual_doc(quoted_on="2026-09-29", **overrides):
    """A COMPLETE hand-quote file: every zone x box cell, ascending by box size
    so the monotonicity guard passes."""
    order = {"small": 9.0, "large": 15.0, "p24x10x4": 12.0, "p24x10x6": 20.0}
    doc = {"_quoted_on": quoted_on}
    for i, zone in enumerate(rc.REFERENCE_ZIPS):
        doc[zone] = {
            box_id: {
                "quote": order[box_id] + i,
                "carrier": "UPS",
                "service": "03",
                "service_title": "UPS Ground",
            }
            for box_id in rc.PARCELS
        }
    doc.update(overrides)
    return doc


def _write_json(doc):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(doc, fh)
        return fh.name


class TestManualQuoteLoader(unittest.TestCase):
    """GOL-2641: the no-network refresh path. It must reject anything it cannot
    publish honestly rather than guess — a hand refresh writes money."""

    def _load(self, doc):
        path = _write_json(doc)
        try:
            return rc.load_manual_quotes(path)
        finally:
            os.unlink(path)

    def test_loads_raw_quotes_and_the_quote_date(self):
        quotes, quoted_on = self._load(_manual_doc())
        self.assertEqual(quoted_on, date(2026, 9, 29))
        cell = quotes["zone_1"]["small"]
        self.assertEqual(cell["price"], 9.0)
        self.assertEqual(cell["carrier"], "UPS")
        self.assertEqual(cell["service_title"], "UPS Ground")

    def test_quoted_on_is_required(self):
        doc = _manual_doc()
        del doc["_quoted_on"]
        with self.assertRaises(ValueError) as cm:
            self._load(doc)
        self.assertIn("_quoted_on", str(cm.exception))

    def test_unparsable_quoted_on_raises(self):
        with self.assertRaises(ValueError):
            self._load(_manual_doc(_quoted_on="09/29/2026"))

    def test_unknown_zone_raises(self):
        with self.assertRaises(ValueError) as cm:
            self._load(_manual_doc(zone_99={"small": {"quote": 1.0}}))
        self.assertIn("zone_99", str(cm.exception))

    def test_unknown_box_id_raises(self):
        doc = _manual_doc()
        doc["zone_1"]["enormous"] = {"quote": 5.0, "carrier": "UPS", "service": "03", "service_title": "t"}
        with self.assertRaises(ValueError) as cm:
            self._load(doc)
        self.assertIn("enormous", str(cm.exception))

    def test_carrier_provenance_is_required_per_cell(self):
        # schema 3 exists so "which carrier set this rate" is always answerable,
        # and rate_feed shows service_title in storefront copy.
        for key in ("carrier", "service", "service_title"):
            doc = _manual_doc()
            del doc["zone_1"]["small"][key]
            with self.assertRaises(ValueError) as cm:
                self._load(doc)
            self.assertIn(key, str(cm.exception))

    def test_bare_number_cell_is_rejected(self):
        doc = _manual_doc()
        doc["zone_1"]["small"] = 12.0
        with self.assertRaises(ValueError):
            self._load(doc)

    def test_non_positive_quote_is_rejected(self):
        for bad in (0, -3.0):
            doc = _manual_doc()
            doc["zone_1"]["small"]["quote"] = bad
            with self.assertRaises(ValueError) as cm:
                self._load(doc)
            self.assertIn("positive", str(cm.exception))


class TestManualRefreshRun(unittest.TestCase):
    def _run(self, doc, extra_argv=(), current=None):
        manual_path = _write_json(doc)
        rates_path = _write_json(current if current is not None else {"_comment": "seed", "_schema": 3})
        out_dir = tempfile.mkdtemp()
        try:
            argv = ["--manual-quotes", manual_path] + list(extra_argv)
            with mock.patch.object(rc, "RATES_PATH", rates_path), mock.patch.object(rc, "OUT_DIR", out_dir):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = rc.main(argv)
            with open(rates_path, encoding="utf-8") as fh:
                written = fh.read()
            return code, out.getvalue(), err.getvalue(), written
        finally:
            os.unlink(manual_path)
            os.unlink(rates_path)

    def test_hand_refresh_writes_the_table_with_provenance(self):
        code, _, err, written = self._run(_manual_doc())
        self.assertEqual(code, 3)  # rewritten
        doc = json.loads(written)
        self.assertEqual(doc["_rates_verified_on"], "2026-09-29")
        self.assertEqual(doc["_rates_source"], "manual")
        self.assertEqual(doc["_schema"], 3)
        # No quote source was contacted, and the log says so.
        self.assertIn("HAND-QUOTED", err)
        self.assertNotIn("Service visibility", err)

    def test_hand_quotes_go_through_the_same_target_formula(self):
        # small: ceil(9.00) = 9 — the published cell is the carrier quote rounded
        # up, no handling (GOL-2923, Josh 2026-10-06). Hand quotes go through the
        # same target_rate; hand-editing shipping_rates.json is what skips it.
        _, _, _, written = self._run(_manual_doc())
        cell = json.loads(written)["zone_1"]["small"]
        self.assertEqual(cell["base"], float(rc.target_rate(9.0)))
        self.assertEqual(cell["base"], 9.0)
        self.assertEqual(set(cell), {"base", "carrier", "service", "service_title"})

    def test_incomplete_hand_refresh_is_refused(self):
        # A dropped cell falls back in the Odoo loader -> under-charge. Exit 2,
        # and the table must be untouched.
        doc = _manual_doc()
        del doc["zone_1"]["small"]
        real = {"_comment": "x", "_schema": 3, "zone_1": {"small": {"base": 18.0}}}
        before = json.dumps(real)
        code, _, err, written = self._run(doc, current=real)
        self.assertEqual(code, 2)
        self.assertIn("zone_1/small", err)
        self.assertIn("incomplete", err)
        self.assertEqual(json.loads(written), json.loads(before))

    def test_manual_still_honours_the_monotonicity_guard(self):
        # A bigger box cheaper than a smaller one inside a zone is cart-gaming;
        # the hand path must not be a way around the guard (exit 4).
        doc = _manual_doc()
        doc["zone_1"]["large"]["quote"] = 1.0
        code, _, err, _ = self._run(doc)
        self.assertEqual(code, 4)
        self.assertIn("monotonicity", err)

    def test_manual_dry_run_does_not_write(self):
        real = {"_comment": "x", "_schema": 3, "zone_1": {"small": {"base": 18.0}}}
        before = json.dumps(real)
        code, _, _, written = self._run(_manual_doc(), extra_argv=["--dry-run"], current=real)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(written), json.loads(before))

    def test_manual_reports_no_drift_when_the_table_already_matches(self):
        # Re-confirming unchanged rates is the common case; it must be a clean
        # exit 0, not a spurious rewrite.
        _, _, _, written = self._run(_manual_doc())
        current = json.loads(written)
        code, out, _, again = self._run(_manual_doc(), current=current)
        self.assertEqual(code, 0)
        self.assertIn("no material drift", out)

    def test_malformed_file_is_an_exit_2_message_not_a_traceback(self):
        doc = _manual_doc(_quoted_on="YYYY-MM-DD")
        code, _, err, _ = self._run(doc)
        self.assertEqual(code, 2)
        self.assertIn("--manual-quotes rejected", err)
        self.assertNotIn("Traceback", err)

    def test_shipped_template_is_complete_but_not_applyable(self):
        # The template must cover every cell (so it is a usable starting point)
        # while being IMPOSSIBLE to apply verbatim — otherwise it would be a way
        # to stamp the table "fresh" with no real quotes behind it.
        path = os.path.join(os.path.dirname(__file__), "..", "manual_quotes.example.json")
        with open(path, encoding="utf-8") as fh:
            tmpl = json.load(fh)
        for zone in rc.REFERENCE_ZIPS:
            self.assertEqual(sorted(tmpl[zone]), sorted(rc.PARCELS), f"{zone}: template must cover every box")
        code, _, err, _ = self._run(tmpl)
        self.assertEqual(code, 2, "the template must never be applyable as-is")
        self.assertIn("--manual-quotes rejected", err)

    def test_manual_quotes_refuses_to_combine_with_a_fixture(self):
        code, _, err, _ = self._run(_manual_doc(), extra_argv=["--fixture", NO_GROUND_FIXTURE])
        self.assertEqual(code, 2)
        self.assertIn("cannot be combined", err)


class TestProvenanceStampOnAutomatedRuns(unittest.TestCase):
    def test_probe_run_stamps_the_probe_date_and_pirateship_source(self):
        path = _write_json({"_comment": "seed", "_schema": 2})
        out_dir = tempfile.mkdtemp()
        try:
            argv = ["--fixture-dir", _FX, "--probe-date", PROBE_DATE.isoformat()]
            with mock.patch.object(rc, "RATES_PATH", path), mock.patch.object(rc, "OUT_DIR", out_dir):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = rc.main(argv)
            self.assertEqual(code, 3)
            doc = json.load(open(path, encoding="utf-8"))
            self.assertEqual(doc["_rates_verified_on"], PROBE_DATE.isoformat())
            self.assertEqual(doc["_rates_source"], "pirateship")
        finally:
            os.unlink(path)


class TestShippedRatesFile(unittest.TestCase):
    def test_shipped_rates_file_holds_real_published_rates(self):
        with open(rc.RATES_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertNotIn("_provisional", doc)
        zones = sorted(k for k in doc if not k.startswith("_"))
        # GOL-2238 (2026-09-14): the 2026-09-08 zone_6/zone_7 split was retired
        # (TN -> zone_1, AR/MO/IA -> zone_5), back to the 5-zone distance table.
        self.assertEqual(zones, ["zone_1", "zone_2", "zone_3", "zone_4", "zone_5"])
        for zone in zones:
            self.assertTrue(doc[zone], f"{zone}: expected per-box rates")


if __name__ == "__main__":
    unittest.main()
