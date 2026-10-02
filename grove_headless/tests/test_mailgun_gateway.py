"""Pure tests for mailgun_gateway (stdlib only, no Odoo DB).

Loaded by file path so its lack of Odoo imports is honoured and it runs in the
fast pytest lane — same pattern as test_stripe_gateway.py / test_shipment_email.py.
Covers the signature contract (good / bad / replayed-by-staleness), the status
ladder, and event parsing for every tracked Mailgun event type (GOL-2903 §5).
"""

import hashlib
import hmac
import importlib.util
import json
import os
import time
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "mailgun_gateway.py")
_spec = importlib.util.spec_from_file_location("grove_mailgun_gateway", _MODULE_PATH)
mg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mg)

_KEY = "key-test-signing-secret"


def _sign(key, timestamp, token):
    return hmac.new(key.encode(), f"{timestamp}{token}".encode(), hashlib.sha256).hexdigest()


class SignatureTests(unittest.TestCase):
    def test_valid_signature_passes(self):
        now = 1_700_000_000
        ts, token = str(now), "a" * 50
        sig = _sign(_KEY, ts, token)
        self.assertTrue(mg.verify_signature(_KEY, ts, token, sig, now=now))

    def test_tampered_signature_rejected(self):
        now = 1_700_000_000
        ts, token = str(now), "a" * 50
        bad = _sign("wrong-key", ts, token)
        with self.assertRaises(mg.MailgunError):
            mg.verify_signature(_KEY, ts, token, bad, now=now)

    def test_tampered_token_rejected(self):
        now = 1_700_000_000
        ts, token = str(now), "a" * 50
        sig = _sign(_KEY, ts, token)
        with self.assertRaises(mg.MailgunError):
            mg.verify_signature(_KEY, ts, "a-different-token", sig, now=now)

    def test_stale_timestamp_rejected_as_replay(self):
        now = 1_700_000_000
        old = now - (mg.SIG_TOLERANCE + 60)
        ts, token = str(old), "a" * 50
        sig = _sign(_KEY, ts, token)
        with self.assertRaises(mg.MailgunError):
            mg.verify_signature(_KEY, ts, token, sig, now=now)

    def test_future_timestamp_rejected(self):
        now = 1_700_000_000
        future = now + (mg.SIG_TOLERANCE + 60)
        ts, token = str(future), "a" * 50
        sig = _sign(_KEY, ts, token)
        with self.assertRaises(mg.MailgunError):
            mg.verify_signature(_KEY, ts, token, sig, now=now)

    def test_missing_signing_key_rejected(self):
        with self.assertRaises(mg.MailgunError):
            mg.verify_signature("", "1700000000", "tok", "sig")

    def test_incomplete_triple_rejected(self):
        with self.assertRaises(mg.MailgunError):
            mg.verify_signature(_KEY, "", "tok", "sig")

    def test_non_integer_timestamp_rejected(self):
        with self.assertRaises(mg.MailgunError):
            mg.verify_signature(_KEY, "not-a-number", "tok", "sig")


class StatusLadderTests(unittest.TestCase):
    def test_positive_ladder_is_monotonic(self):
        self.assertLess(mg.status_rank("sent"), mg.status_rank("delivered"))
        self.assertLess(mg.status_rank("delivered"), mg.status_rank("opened"))

    def test_late_delivered_does_not_outrank_opened(self):
        # The exact invariant from §2: opened must not be overwritten by a late
        # delivered. A caller applies the new status only if its rank is higher.
        self.assertLessEqual(mg.status_rank("delivered"), mg.status_rank("opened"))

    def test_permanent_failure_is_terminal(self):
        # Nothing short of a complaint supersedes a permanent failure.
        self.assertGreater(mg.status_rank("failed", "permanent"), mg.status_rank("delivered"))
        self.assertGreater(mg.status_rank("failed", "permanent"), mg.status_rank("opened"))

    def test_temporary_failure_can_be_superseded_by_delivery(self):
        # A soft bounce / deferral ranks below delivered so a retry that lands
        # correctly promotes the row to delivered.
        self.assertLess(mg.status_rank("failed", "temporary"), mg.status_rank("delivered"))

    def test_complaint_always_wins(self):
        for s in ("delivered", "opened", "failed", "blocked"):
            self.assertGreater(mg.status_rank("complained"), mg.status_rank(s))


def _event(event, **data):
    base = {
        "event": event,
        "recipient": "buyer@example.com",
        "message": {"headers": {"message-id": "<mg-123@mg>"}},
        "user-variables": {"grove_log_id": "42", "order_ref": "S00349"},
        "timestamp": 1_700_000_000.5,
    }
    base.update(data)
    return {"event-data": base}


class ParseEventTests(unittest.TestCase):
    def test_delivered(self):
        out = mg.parse_event(_event("delivered"))
        self.assertEqual(out["status"], "delivered")
        self.assertEqual(out["log_id"], 42)
        self.assertEqual(out["order_ref"], "S00349")
        self.assertEqual(out["recipient"], "buyer@example.com")
        self.assertEqual(out["mailgun_message_id"], "<mg-123@mg>")

    def test_opened(self):
        self.assertEqual(mg.parse_event(_event("opened"))["status"], "opened")

    def test_complained(self):
        self.assertEqual(mg.parse_event(_event("complained"))["status"], "complained")

    def test_permanent_failure_carries_reason(self):
        out = mg.parse_event(
            _event(
                "failed",
                severity="permanent",
                reason="bounce",
                **{"delivery-status": {"message": "550 5.1.1 user unknown"}},
            )
        )
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["severity"], "permanent")
        self.assertIn("550", out["failure_reason"])

    def test_temporary_failure(self):
        out = mg.parse_event(_event("failed", severity="temporary", reason="bounce"))
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["severity"], "temporary")

    def test_suppression_maps_to_blocked(self):
        out = mg.parse_event(_event("failed", severity="permanent", reason="suppress-bounce"))
        self.assertEqual(out["status"], "blocked")

    def test_accepted_is_untracked(self):
        self.assertIsNone(mg.parse_event(_event("accepted"))["status"])

    def test_unknown_event_is_untracked(self):
        self.assertIsNone(mg.parse_event(_event("clicked"))["status"])

    def test_missing_log_id_is_none(self):
        out = mg.parse_event(_event("delivered", **{"user-variables": {}}))
        self.assertIsNone(out["log_id"])

    def test_non_integer_log_id_is_none(self):
        out = mg.parse_event(_event("delivered", **{"user-variables": {"grove_log_id": "x"}}))
        self.assertIsNone(out["log_id"])

    def test_broken_payload_raises(self):
        with self.assertRaises(mg.MailgunError):
            mg.parse_event("not-a-dict")


class VariablesHeaderTests(unittest.TestCase):
    def test_round_trips_through_user_variables(self):
        header = mg.variables_header(42, "S00349")
        decoded = json.loads(header)
        self.assertEqual(decoded["grove_log_id"], "42")
        self.assertEqual(decoded["order_ref"], "S00349")

    def test_none_order_ref_is_empty_string(self):
        decoded = json.loads(mg.variables_header(7, None))
        self.assertEqual(decoded["order_ref"], "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
