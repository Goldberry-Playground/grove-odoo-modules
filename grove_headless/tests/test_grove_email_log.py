"""grove.email.log send + event-apply (GOL-2903).

Runs under Odoo's --test-enable runner (needs a DB for sale.order / mail.mail),
so it is listed in tests/__init__.py AND excluded from pytest in conftest.py so
it is not double-skipped (GOL-1936). The pure signature/parse logic is covered
separately in test_mailgun_gateway.py (pytest lane).

What must hold:
  * every send site logs a row through the single choke point, marked Sent;
  * a send error records a permanent Failed row and alerts once;
  * events advance the status, never regress it (a late delivered can't clobber
    an opened), and are idempotent;
  * a failed / blocked / complained event fires exactly one ops Discord alert;
  * the webhook match resolves by log id, then falls back to recipient.
"""

from unittest import mock

from odoo.addons.grove_headless.models import mailgun_gateway
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from .common import GroveTaxFixtureMixin

_POST = "odoo.addons.grove_headless.models.grove_email_log.requests.post"


@tagged("post_install", "-at_install")
class TestGroveEmailLog(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")
        self.partner = self.env["res.partner"].create(
            {"name": "Email Customer", "email": "buyer@example.com", "company_id": self.company.id}
        )
        self.product = self.env["product.product"].create(
            {"name": "American Plum", "type": "consu", "list_price": 22.0}
        )
        self.order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "company_id": self.company.id,
                "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0})],
            }
        )
        self.Log = self.env["grove.email.log"]

    def _mail_vals(self):
        return {
            "subject": "Test",
            "email_to": self.partner.email,
            "body_html": "<p>hi</p>",
            "auto_delete": True,
        }

    # ── send side ─────────────────────────────────────────────────────────

    def test_log_and_send_creates_sent_row(self):
        with mock.patch.object(type(self.env["mail.mail"]), "send", lambda self, *a, **k: True):
            log = self.Log.log_and_send(
                self.order, "deposit", self.partner.email, "Your deposit", mail_vals=self._mail_vals()
            )
        self.assertEqual(log.status, "sent")
        self.assertEqual(log.kind, "deposit")
        self.assertEqual(log.order_id, self.order)
        self.assertEqual(log.company_id, self.company)
        self.assertEqual(log.recipient, self.partner.email)
        self.assertTrue(log.sent_at)
        # Linked for the sale-order "Emails" tab.
        self.assertIn(log, self.order.grove_email_log_ids)

    @mute_logger("odoo.addons.grove_headless.models.grove_email_log")
    def test_send_error_records_permanent_failure_and_alerts_once(self):
        def _boom(self, *a, **k):
            raise RuntimeError("smtp down")

        with mock.patch.dict("os.environ", {"DISCORD_OPS_WEBHOOK_URL": "https://d/ops"}, clear=True):
            with mock.patch.object(type(self.env["mail.mail"]), "send", _boom):
                with mock.patch(_POST) as post:
                    log = self.Log.log_and_send(
                        self.order, "receipt", self.partner.email, "Receipt", mail_vals=self._mail_vals()
                    )
        self.assertEqual(log.status, "failed")
        self.assertEqual(log.failure_severity, "permanent")
        self.assertTrue(log.alerted)
        post.assert_called_once()

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            self.Log.log_and_send(self.order, "not-a-kind", self.partner.email, "x", mail_vals=self._mail_vals())

    # ── receive side ──────────────────────────────────────────────────────

    def _row(self, **vals):
        base = {
            "order_id": self.order.id,
            "company_id": self.company.id,
            "kind": "receipt",
            "recipient": self.partner.email,
            "subject": "Receipt",
            "status": "sent",
        }
        base.update(vals)
        return self.Log.create(base)

    def _parsed(self, event, **extra):
        data = {
            "event": event,
            "recipient": self.partner.email,
            "message": {"headers": {"message-id": "<mg@mg>"}},
            "user-variables": {"grove_log_id": str(extra.pop("log_id", "")), "order_ref": self.order.name},
            "timestamp": 1_700_000_000.0,
        }
        data.update(extra)
        return mailgun_gateway.parse_event({"event-data": data})

    def test_delivered_then_opened_advances(self):
        log = self._row()
        log.apply_event(self._parsed("delivered", log_id=log.id))
        self.assertEqual(log.status, "delivered")
        self.assertTrue(log.delivered_at)
        log.apply_event(self._parsed("opened", log_id=log.id))
        self.assertEqual(log.status, "opened")
        self.assertTrue(log.opened_at)

    def test_late_delivered_does_not_regress_opened(self):
        log = self._row(status="opened")
        log.apply_event(self._parsed("delivered", log_id=log.id))
        self.assertEqual(log.status, "opened")

    def test_duplicate_delivered_is_idempotent(self):
        log = self._row()
        self.assertFalse(log.apply_event(self._parsed("delivered", log_id=log.id)))
        first = log.delivered_at
        self.assertFalse(log.apply_event(self._parsed("delivered", log_id=log.id)))
        self.assertEqual(log.delivered_at, first)
        self.assertEqual(log.status, "delivered")

    def test_temporary_failure_then_delivered_promotes(self):
        log = self._row()
        log.apply_event(self._parsed("failed", severity="temporary", reason="bounce", log_id=log.id))
        self.assertEqual(log.status, "failed")
        self.assertEqual(log.failure_severity, "temporary")
        # A retry that lands supersedes the soft bounce.
        log.apply_event(self._parsed("delivered", log_id=log.id))
        self.assertEqual(log.status, "delivered")

    def test_permanent_failure_is_terminal_against_delivered(self):
        log = self._row()
        log.apply_event(
            self._parsed(
                "failed",
                severity="permanent",
                reason="bounce",
                log_id=log.id,
                **{"delivery-status": {"message": "550 user unknown"}},
            )
        )
        self.assertEqual(log.status, "failed")
        self.assertIn("550", log.failure_reason)
        log.apply_event(self._parsed("delivered", log_id=log.id))
        self.assertEqual(log.status, "failed")

    def test_failure_alerts_exactly_once(self):
        log = self._row()
        with mock.patch.dict("os.environ", {"DISCORD_OPS_WEBHOOK_URL": "https://d/ops"}, clear=True):
            with mock.patch(_POST) as post:
                fired = log.apply_event(self._parsed("failed", severity="permanent", reason="bounce", log_id=log.id))
                self.assertTrue(fired)
                # A duplicate failed event must not ping a second time.
                log.apply_event(self._parsed("failed", severity="permanent", reason="bounce", log_id=log.id))
        post.assert_called_once()
        self.assertEqual(post.call_args[0][0], "https://d/ops")

    def test_suppression_maps_to_blocked_and_alerts(self):
        log = self._row()
        with mock.patch.dict("os.environ", {"DISCORD_OPS_WEBHOOK_URL": "https://d/ops"}, clear=True):
            with mock.patch(_POST) as post:
                log.apply_event(self._parsed("failed", severity="permanent", reason="suppress-bounce", log_id=log.id))
        self.assertEqual(log.status, "blocked")
        post.assert_called_once()

    def test_complaint_wins_over_opened(self):
        log = self._row(status="opened")
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch(_POST) as post:
                log.apply_event(self._parsed("complained", log_id=log.id))
        self.assertEqual(log.status, "complained")
        # No webhook configured → no post, but the alerted guard is still set.
        post.assert_not_called()
        self.assertTrue(log.alerted)

    def test_match_event_by_log_id(self):
        log = self._row()
        parsed = self._parsed("delivered", log_id=log.id)
        self.assertEqual(self.Log.match_event(parsed), log)

    def test_match_event_falls_back_to_recipient(self):
        log = self._row()
        parsed = self._parsed("delivered")  # no log id in user-variables
        self.assertIsNone(parsed["log_id"])
        self.assertEqual(self.Log.match_event(parsed), log)

    def test_match_event_no_candidate_is_empty(self):
        parsed = self._parsed("delivered", recipient="nobody@example.com")
        self.assertFalse(self.Log.match_event(parsed))

    def test_mailgun_message_id_captured_once(self):
        log = self._row()
        log.apply_event(self._parsed("delivered", log_id=log.id))
        self.assertEqual(log.mailgun_message_id, "<mg@mg>")
