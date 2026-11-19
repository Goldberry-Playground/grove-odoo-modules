"""Tests for the keyless Pirate Ship live-quote client (GOL-3927, spec §1).

The client in ``models/pirateship_rates.py`` is pure Python, so these are plain
``unittest`` cases with no DB — they run under Odoo's ``--test-enable`` runner and
standalone (``python3 -m pytest``). The module is loaded by file path so
importing it never drags in the Odoo addon package.

Coverage mirrors the spec Testing section: parse recorded real responses (26651
with the $3.45 Delivery Area surcharge, 28401 without, a QueryRetired error, a
403 challenge, a timeout); cheapest-within-ceiling selection; ounces conversion;
the persisted-query (keyless) request shape.
"""

import importlib.util
import json
import os
import unittest
from datetime import date

import requests

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "pirateship_rates.py")
_spec = importlib.util.spec_from_file_location("grove_pirateship_rates", _MODULE_PATH)
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "pirateship")
# All fixtures are anchored to a quote taken on this date (their M/D delivery
# descriptions resolve to a few days out); pin it so transit-day math is stable.
TODAY = date(2026, 11, 19)


def _load(name):
    with open(os.path.join(_FIXTURES, f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


class FakeResponse:
    """Minimal ``requests``-like response for the injected ``post``."""

    def __init__(self, payload=None, status_code=200, raise_json=False):
        self._payload = payload
        self.status_code = status_code
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not JSON (challenge page)")
        return self._payload


def _post_returning(payload=None, status_code=200, raise_json=False, capture=None):
    """A fake ``post`` that records its call into ``capture`` and returns a
    ``FakeResponse``."""

    def _post(url, json=None, timeout=None, headers=None):  # noqa: A002 - mirror requests
        if capture is not None:
            capture.update({"url": url, "json": json, "timeout": timeout, "headers": headers})
        return FakeResponse(payload=payload, status_code=status_code, raise_json=raise_json)

    return _post


class TestRequestShape(unittest.TestCase):
    def test_persisted_query_keyless_no_query_document(self):
        capture = {}
        post = _post_returning(payload=_load("rates_26651"), capture=capture)
        pr.quote("26651", "p24x10x4", 6.0, post=post, today=TODAY)
        body = capture["json"]
        # Persisted-queries-only mode: send the hash, never a query document.
        self.assertEqual(body["operationName"], "RatesQuery")
        self.assertNotIn("query", body)
        pq = body["extensions"]["persistedQuery"]
        self.assertEqual(pq["version"], 1)
        self.assertEqual(pq["sha256Hash"], pr.PERSISTED_QUERY_HASH)

    def test_variables_origin_residential_mailclasses(self):
        capture = {}
        post = _post_returning(payload=_load("rates_26651"), capture=capture)
        pr.quote("28401", "p24x10x4", 6.0, post=post, today=TODAY)
        v = capture["json"]["variables"]
        self.assertEqual(v["originZip"], "26651")
        self.assertEqual(v["destinationZip"], "28401")
        self.assertIs(v["isResidential"], True)
        self.assertEqual(v["mailClassKeys"], ["03", "93", "GroundAdvantage"])
        self.assertEqual(v["packageTypeKeys"], ["Parcel"])

    def test_weight_sent_in_ounces(self):
        capture = {}
        post = _post_returning(payload=_load("rates_26651"), capture=capture)
        pr.quote("26651", "p24x10x4", 6.0, post=post, today=TODAY)
        v = capture["json"]["variables"]
        self.assertEqual(v["weight"], 96.0)  # 6 lb * 16 oz/lb
        # Dimensions come from the box catalog, not the caller.
        self.assertEqual((v["dimensionX"], v["dimensionY"], v["dimensionZ"]), (24, 10, 4))

    def test_three_second_timeout(self):
        capture = {}
        post = _post_returning(payload=_load("rates_26651"), capture=capture)
        pr.quote("26651", "p24x10x4", 6.0, post=post, today=TODAY)
        self.assertEqual(capture["timeout"], 3.0)


class TestParseRecorded(unittest.TestCase):
    def test_26651_winner_and_delivery_area_surcharge(self):
        post = _post_returning(payload=_load("rates_26651"))
        q = pr.quote("26651", "p24x10x4", 6.0, post=post, today=TODAY)
        # UPS Ground is cheapest allowlisted ground within the 7-day ceiling.
        self.assertEqual(q.winner.service, "03")
        self.assertEqual(q.winner.carrier, "UPS")
        self.assertEqual(q.winner.service_title, "UPS Ground")
        self.assertEqual(q.total, 11.83)
        self.assertEqual(q.winner.transit_days, 6)  # 11/19 -> 11/25
        # The $3.45 Delivery Area surcharge is itemised on the winner.
        names = {s.name: s.amount for s in q.winner.surcharges}
        self.assertEqual(names.get("Delivery Area"), 3.45)
        # The non-allowlisted UPS Next Day Air rate is filtered out.
        self.assertEqual({o.service for o in q.options}, {"03", "93", "GroundAdvantage"})

    def test_28401_cheapest_within_ceiling_no_delivery_area(self):
        post = _post_returning(payload=_load("rates_28401"))
        q = pr.quote("28401", "p24x10x4", 6.0, post=post, today=TODAY)
        # Ground Saver is cheaper than Ground here and still within the ceiling.
        self.assertEqual(q.winner.service, "93")
        self.assertEqual(q.total, 8.90)
        # No Delivery Area surcharge at this urban ZIP.
        self.assertNotIn("Delivery Area", {s.name for o in q.options for s in o.surcharges})

    def test_query_retired_raises(self):
        post = _post_returning(payload=_load("query_retired"))
        with self.assertRaises(pr.QueryRetired):
            pr.quote("26651", "p24x10x4", 6.0, post=post, today=TODAY)


class TestErrorMapping(unittest.TestCase):
    def test_http_403_is_blocked(self):
        post = _post_returning(payload={}, status_code=403)
        with self.assertRaises(pr.Blocked):
            pr.quote("26651", "p24x10x4", 6.0, post=post, today=TODAY)

    def test_http_429_is_blocked(self):
        post = _post_returning(payload={}, status_code=429)
        with self.assertRaises(pr.Blocked):
            pr.quote("26651", "p24x10x4", 6.0, post=post, today=TODAY)

    def test_non_json_challenge_is_blocked(self):
        post = _post_returning(raise_json=True)
        with self.assertRaises(pr.Blocked):
            pr.quote("26651", "p24x10x4", 6.0, post=post, today=TODAY)

    def test_timeout_is_timeout(self):
        def post(*a, **k):
            raise requests.Timeout("too slow")

        with self.assertRaises(pr.Timeout):
            pr.quote("26651", "p24x10x4", 6.0, post=post, today=TODAY)

    def test_connection_error_is_blocked(self):
        def post(*a, **k):
            raise requests.ConnectionError("dead host")

        with self.assertRaises(pr.Blocked):
            pr.quote("26651", "p24x10x4", 6.0, post=post, today=TODAY)

    def test_no_allowlisted_rate_is_noservice(self):
        payload = {
            "data": {
                "rates": [
                    {
                        "title": "UPS Next Day Air",
                        "mailClassKey": "01",
                        "carrier": {"title": "UPS"},
                        "totalPrice": 78.0,
                        "surcharges": [],
                    }
                ]
            }
        }
        post = _post_returning(payload=payload)
        with self.assertRaises(pr.NoService):
            pr.quote("26651", "p24x10x4", 6.0, post=post, today=TODAY)

    def test_unexpected_graphql_error_is_badresponse(self):
        post = _post_returning(payload={"errors": [{"message": "Variable $weight of required type"}]})
        with self.assertRaises(pr.BadResponse):
            pr.quote("26651", "p24x10x4", 6.0, post=post, today=TODAY)

    def test_unknown_box_is_noservice(self):
        post = _post_returning(payload=_load("rates_26651"))
        with self.assertRaises(pr.NoService):
            pr.quote("26651", "nope", 6.0, post=post, today=TODAY)


class TestSelection(unittest.TestCase):
    def _opt(self, service, total, transit):
        carrier = "USPS" if service == "GroundAdvantage" else "UPS"
        return pr.RateOption(
            service=service,
            carrier=carrier,
            service_title=pr.SERVICE_TITLES[(carrier, service)],
            total=total,
            transit_days=transit,
        )

    def test_cheapest_within_ceiling_wins(self):
        opts = [self._opt("03", 20.0, 3), self._opt("93", 15.0, 6), self._opt("GroundAdvantage", 25.0, 2)]
        self.assertEqual(pr.select_winner(opts).service, "93")

    def test_over_ceiling_excluded_even_if_cheapest(self):
        # Ground Saver is cheapest but delivers in 9 days (> 7) -> excluded.
        opts = [self._opt("03", 20.0, 5), self._opt("93", 12.0, 9)]
        self.assertEqual(pr.select_winner(opts).service, "03")

    def test_none_within_ceiling_fastest_wins(self):
        # No option fits the ceiling -> fastest known wins (ties break cheapest).
        opts = [self._opt("03", 20.0, 9), self._opt("93", 12.0, 11), self._opt("GroundAdvantage", 30.0, 9)]
        winner = pr.select_winner(opts)
        self.assertEqual(winner.transit_days, 9)
        self.assertEqual(winner.service, "03")  # cheaper of the two 9-day options

    def test_unknown_transit_counts_as_within_ceiling(self):
        opts = [self._opt("03", 20.0, 5), self._opt("93", 10.0, None)]
        self.assertEqual(pr.select_winner(opts).service, "93")

    def test_empty_options_none(self):
        self.assertIsNone(pr.select_winner([]))


class TestTransitParsing(unittest.TestCase):
    def test_rolls_to_next_year(self):
        # A late-December quote of an early-January date rolls to next year.
        self.assertEqual(pr.parse_transit_days("delivery 1/3", date(2026, 12, 30)), 4)

    def test_unparsable_is_none(self):
        self.assertIsNone(pr.parse_transit_days("shipped soon", TODAY))
        self.assertIsNone(pr.parse_transit_days(None, TODAY))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
