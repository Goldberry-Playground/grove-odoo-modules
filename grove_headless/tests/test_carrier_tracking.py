"""TDD tests for carrier_tracking (pure Python, no Odoo DB, no network).

Loaded by file path like test_shippo_client so the module's imports don't need
the Odoo runtime. Every carrier call is served by an injected fake session, so
this suite makes ZERO live UPS/USPS requests — the CI guardrail (spec § C).
"""

import importlib.util
import os
import unittest
from unittest import mock

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "carrier_tracking.py")
_spec = importlib.util.spec_from_file_location("grove_carrier_tracking", _MODULE_PATH)
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    """Fake requests session: queue token responses (post) and track responses
    (get) and record the calls."""

    def __init__(self, token_resp=None, track_resp=None):
        self.token_resp = token_resp
        self.track_resp = track_resp
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.token_resp

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.track_resp


_TOKEN_OK = _Resp(200, {"access_token": "tok-123", "expires_in": 3600})


# ── Status mapping ───────────────────────────────────────────────────────────


class TestUpsMapping(unittest.TestCase):
    def _payload(self, *, type_="", desc="", activity=None):
        current = {}
        if type_ or desc:
            current = {"type": type_, "description": desc}
        pkg = {"currentStatus": current}
        if activity is not None:
            pkg["activity"] = activity
        return {"trackResponse": {"shipment": [{"package": [pkg]}]}}

    def test_delivered(self):
        self.assertEqual(ct.map_ups_status(self._payload(type_="D", desc="Delivered")), ct.STATUS_DELIVERED)

    def test_in_transit(self):
        self.assertEqual(ct.map_ups_status(self._payload(type_="I", desc="Departed from Facility")), ct.STATUS_TRANSIT)

    def test_out_for_delivery_from_description(self):
        # UPS keeps type 'I' while the description says out for delivery.
        self.assertEqual(
            ct.map_ups_status(self._payload(type_="I", desc="Out For Delivery Today")),
            ct.STATUS_OUT_FOR_DELIVERY,
        )

    def test_exception(self):
        self.assertEqual(ct.map_ups_status(self._payload(type_="X", desc="Exception")), ct.STATUS_FAILURE)

    def test_manifest_is_no_change(self):
        # Label created / billing info received but not moving -> no change.
        self.assertIsNone(ct.map_ups_status(self._payload(type_="M", desc="Shipper created a label")))

    def test_falls_back_to_latest_activity(self):
        payload = self._payload(activity=[{"status": {"type": "D", "description": "Delivered"}}])
        self.assertEqual(ct.map_ups_status(payload), ct.STATUS_DELIVERED)

    def test_empty_payload_is_no_change(self):
        self.assertIsNone(ct.map_ups_status({}))
        self.assertIsNone(ct.map_ups_status({"trackResponse": {"shipment": []}}))


class TestUspsMapping(unittest.TestCase):
    def _p(self, status, category="", summary=""):
        return {"status": status, "statusCategory": category, "statusSummary": summary}

    def test_delivered(self):
        self.assertEqual(ct.map_usps_status(self._p("Delivered, In/At Mailbox")), ct.STATUS_DELIVERED)

    def test_delivered_to_agent(self):
        self.assertEqual(ct.map_usps_status(self._p("Delivered to Agent")), ct.STATUS_DELIVERED)

    def test_out_for_delivery(self):
        self.assertEqual(ct.map_usps_status(self._p("Out for Delivery")), ct.STATUS_OUT_FOR_DELIVERY)

    def test_in_transit(self):
        self.assertEqual(ct.map_usps_status(self._p("In Transit to Next Facility")), ct.STATUS_TRANSIT)

    def test_accepted_is_transit(self):
        self.assertEqual(ct.map_usps_status(self._p("Accepted")), ct.STATUS_TRANSIT)

    def test_alert_is_failure(self):
        self.assertEqual(ct.map_usps_status(self._p("Alert", category="Alert")), ct.STATUS_FAILURE)

    def test_pre_shipment_is_no_change(self):
        self.assertIsNone(ct.map_usps_status(self._p("Pre-Shipment Info Sent, USPS Awaiting Item")))

    def test_empty_is_no_change(self):
        self.assertIsNone(ct.map_usps_status({}))


class TestLeastAdvanced(unittest.TestCase):
    def test_picks_least_advanced(self):
        self.assertEqual(ct.least_advanced_status([ct.STATUS_DELIVERED, ct.STATUS_TRANSIT]), ct.STATUS_TRANSIT)

    def test_all_delivered(self):
        self.assertEqual(ct.least_advanced_status([ct.STATUS_DELIVERED, ct.STATUS_DELIVERED]), ct.STATUS_DELIVERED)

    def test_ignores_failure_and_none(self):
        self.assertEqual(
            ct.least_advanced_status([ct.STATUS_FAILURE, ct.STATUS_OUT_FOR_DELIVERY]), ct.STATUS_OUT_FOR_DELIVERY
        )

    def test_none_when_no_progress(self):
        self.assertIsNone(ct.least_advanced_status([ct.STATUS_FAILURE]))
        self.assertIsNone(ct.least_advanced_status([]))


# ── OAuth clients ────────────────────────────────────────────────────────────


class TestUpsClient(unittest.TestCase):
    def _delivered_track(self):
        return _Resp(200, {"trackResponse": {"shipment": [{"package": [{"currentStatus": {"type": "D"}}]}]}})

    def test_token_fetch_then_track(self):
        session = _Session(token_resp=_TOKEN_OK, track_resp=self._delivered_track())
        client = ct.UpsTrackClient("id", "secret", session=session)
        self.assertEqual(client.track("1Z999"), ct.STATUS_DELIVERED)
        # One token POST, one track GET; the track carries the bearer + UPS headers.
        self.assertEqual(len(session.post_calls), 1)
        self.assertIn("/security/v1/oauth/token", session.post_calls[0][0])
        get_url, get_kwargs = session.get_calls[0]
        self.assertIn("/api/track/v1/details/1Z999", get_url)
        self.assertEqual(get_kwargs["headers"]["Authorization"], "Bearer tok-123")
        self.assertEqual(get_kwargs["headers"]["transactionSrc"], "grove_headless")

    def test_token_is_cached_across_calls(self):
        session = _Session(token_resp=_TOKEN_OK, track_resp=self._delivered_track())
        client = ct.UpsTrackClient("id", "secret", session=session)
        client.track("1Z999")
        client.track("1Z888")
        self.assertEqual(len(session.post_calls), 1, "token must be reused within its expiry")
        self.assertEqual(len(session.get_calls), 2)

    def test_bad_credentials_raise_auth_error(self):
        session = _Session(token_resp=_Resp(401, {"error": "invalid_client"}))
        client = ct.UpsTrackClient("id", "bad", session=session)
        with self.assertRaises(ct.CarrierAuthError):
            client.track("1Z999")

    def test_track_401_raises_auth_error_and_clears_token(self):
        session = _Session(token_resp=_TOKEN_OK, track_resp=_Resp(401))
        client = ct.UpsTrackClient("id", "secret", session=session)
        with self.assertRaises(ct.CarrierAuthError):
            client.track("1Z999")
        self.assertIsNone(client._token)

    def test_track_500_raises_carrier_error_not_auth(self):
        session = _Session(token_resp=_TOKEN_OK, track_resp=_Resp(503))
        client = ct.UpsTrackClient("id", "secret", session=session)
        with self.assertRaises(ct.CarrierError) as cm:
            client.track("1Z999")
        self.assertNotIsInstance(cm.exception, ct.CarrierAuthError)


class TestUspsClient(unittest.TestCase):
    def test_token_fetch_then_track(self):
        track = _Resp(200, {"status": "Delivered", "statusCategory": "Delivered"})
        session = _Session(token_resp=_TOKEN_OK, track_resp=track)
        client = ct.UspsTrackClient("id", "secret", session=session)
        self.assertEqual(client.track("9400111"), ct.STATUS_DELIVERED)
        self.assertIn("/oauth2/v3/token", session.post_calls[0][0])
        # USPS sends client id/secret in the JSON body, not Basic auth.
        self.assertEqual(session.post_calls[0][1]["json"]["client_id"], "id")
        self.assertIn("/tracking/v3/tracking/9400111", session.get_calls[0][0])

    def test_missing_access_token_is_auth_error(self):
        session = _Session(token_resp=_Resp(200, {"expires_in": 3600}))  # no access_token
        client = ct.UspsTrackClient("id", "secret", session=session)
        with self.assertRaises(ct.CarrierAuthError):
            client.track("9400111")


class TestBuildClients(unittest.TestCase):
    def test_none_when_creds_absent(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            clients = ct.build_clients()
        self.assertIsNone(clients["UPS"])
        self.assertIsNone(clients["USPS"])

    def test_built_when_creds_present(self):
        env = {
            "UPS_CLIENT_ID": "u",
            "UPS_CLIENT_SECRET": "us",
            "USPS_CLIENT_ID": "p",
            "USPS_CLIENT_SECRET": "ps",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            clients = ct.build_clients()
        self.assertIsInstance(clients["UPS"], ct.UpsTrackClient)
        self.assertIsInstance(clients["USPS"], ct.UspsTrackClient)

    def test_one_carrier_only(self):
        env = {"USPS_CLIENT_ID": "p", "USPS_CLIENT_SECRET": "ps"}
        with mock.patch.dict(os.environ, env, clear=True):
            clients = ct.build_clients()
        self.assertIsNone(clients["UPS"])
        self.assertIsInstance(clients["USPS"], ct.UspsTrackClient)


if __name__ == "__main__":
    unittest.main()
