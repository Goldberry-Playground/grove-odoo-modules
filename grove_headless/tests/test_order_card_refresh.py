"""Editing Discord cards in place after a label import, and the retried notices.

Spring cards gain Mark shipped and Label refunded only by being edited after a
batch import, so the card edit has to survive a failed Discord call: a flag set
in the import transaction, cleared when the edit lands, retried by the hourly
cron. Nothing here may run inside the import's transaction, because that one
charges cards.

DB-backed, so listed in tests/__init__.py AND conftest.py's collect_ignore_glob.
"""

from unittest.mock import MagicMock, patch

from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import label_batch as label_batch_module
from odoo.addons.grove_headless.models import sale_order_ops as ops_module
from odoo.addons.grove_headless.tests.test_zone_late_and_markall import _T, START, _at, _Fixture
from odoo.tests import TransactionCase, tagged

ENV = {"ORDER_ALERT_SECRET": "s3cret", "DISCORD_BRIDGE_BASE_URL": "http://bridge.test"}
EDIT_URL = "http://bridge.test/orders/alert/edit"
POST_STATUS = "odoo.addons.grove_headless.models.sale_order_ops.SaleOrder._grove_post_notice_status"
MESSAGE_ID = "1500000000000000001"


def _reply(status, body=None):
    response = MagicMock(status_code=status)
    if body is None:
        response.json.side_effect = ValueError("not JSON")
    else:
        response.json.return_value = body
    return response


@tagged("post_install", "-at_install")
class TestCardMessageId(_Fixture, TransactionCase):
    def setUp(self):
        super().setUp()
        self._setup_fixture()

    def _post(self, order, response):
        context = grove_main._order_alert_context(order)
        with patch.dict("os.environ", ENV), patch.object(grove_main.requests, "post", return_value=response):
            return grove_main._post_order_alert_card(order, context, False)

    def test_posting_a_card_stores_its_message_id(self):
        order = self._order(state="sale")
        self.assertTrue(self._post(order, _reply(200, {"ok": True, "message_id": MESSAGE_ID})))
        self.assertEqual(order.grove_card_message_id, MESSAGE_ID)

    def test_a_reply_without_an_id_is_still_a_success(self):
        order = self._order(state="sale")
        self.assertTrue(self._post(order, _reply(200)))
        self.assertFalse(order.grove_card_message_id)

    def test_a_failed_post_stores_nothing(self):
        order = self._order(state="sale")
        self.assertFalse(self._post(order, _reply(500, {"message_id": MESSAGE_ID})))
        self.assertFalse(order.grove_card_message_id)

    def test_the_launch_repost_stores_the_message_id(self):
        order = self._order(state="sale")
        response = _reply(200, {"ok": True, "message_id": MESSAGE_ID})
        with patch.dict("os.environ", ENV), patch.object(grove_main.requests, "post", return_value=response):
            self.env["sale.order"]._grove_repost_open_order_cards()
        self.assertEqual(order.grove_card_message_id, MESSAGE_ID)


@tagged("post_install", "-at_install")
class TestCardRefreshAfterImport(_Fixture, TransactionCase):
    def setUp(self):
        super().setUp()
        self._setup_fixture()

    def _imported(self, count=1, message_id=MESSAGE_ID):
        orders = self.env["sale.order"]
        for _ in range(count):
            orders |= self._order(state="sale", grove_card_message_id=message_id)
        self._build()
        self._import(orders)
        return orders

    def _commit_import(self, *responses):
        """What happens when the import's transaction commits."""
        self.env.flush_all()
        with (
            patch.dict("os.environ", ENV),
            patch.object(ops_module.requests, "post", side_effect=responses) as post,
            self.enter_registry_test_mode(),
        ):
            self.env.cr.postcommit.run()
        self.env.invalidate_all()
        return post

    def _cron(self, *responses):
        with patch.dict("os.environ", ENV), patch.object(ops_module.requests, "post", side_effect=responses) as post:
            self.env["sale.order"]._cron_grove_card_refresh()
        return post

    def test_import_flags_the_card_and_edits_only_after_it_commits(self):
        with patch.object(ops_module.requests, "post") as during_import:
            order = self._imported()
        during_import.assert_not_called()
        self.assertTrue(order.grove_card_needs_refresh)

        post = self._commit_import(_reply(200, {"ok": True}))

        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], EDIT_URL)
        self.assertEqual(post.call_args.kwargs["headers"], {"X-Order-Alert-Secret": "s3cret"})
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["messageId"], MESSAGE_ID)
        self.assertEqual(body["orderId"], order.id)
        self.assertEqual(body["tracking"], _T[0])
        self.assertIs(body["shipped"], False)
        self.assertFalse(order.grove_card_needs_refresh)

    def test_a_failed_edit_stays_flagged_and_the_cron_retries_it(self):
        order = self._imported()
        self._commit_import(_reply(500, {"error": "boom"}))
        self.assertTrue(order.grove_card_needs_refresh)
        post = self._cron(_reply(200, {"ok": True}))
        self.assertEqual(post.call_count, 1)
        self.assertFalse(order.grove_card_needs_refresh)

    def test_order_with_no_message_id_is_cleared_and_logged(self):
        order = self._imported(message_id=False)
        with self.assertLogs(ops_module._logger, "WARNING") as logs:
            post = self._commit_import()
        post.assert_not_called()
        self.assertFalse(order.grove_card_needs_refresh)
        self.assertTrue(any(order.name in line for line in logs.output), logs.output)

    def test_message_not_found_is_cleared_not_retried(self):
        order = self._imported()
        with self.assertLogs(ops_module._logger, "WARNING"):
            self._commit_import(_reply(410, {"error": "message not found"}))
        self.assertFalse(order.grove_card_needs_refresh)

    def test_a_bridge_without_the_route_is_a_failure_not_message_not_found(self):
        order = self._imported()
        self._commit_import(_reply(404, {"error": "not found"}))
        self.assertTrue(order.grove_card_needs_refresh)

    def test_unmapped_company_is_cleared(self):
        order = self._imported()
        with self.assertLogs(ops_module._logger, "ERROR"):
            self._commit_import(_reply(422, {"error": "no order channel"}))
        self.assertFalse(order.grove_card_needs_refresh)

    def test_three_consecutive_failures_stop_the_run(self):
        orders = self._imported(5)
        post = self._commit_import(*[_reply(503, {}) for _ in range(3)])
        self.assertEqual(post.call_count, 3)
        self.assertEqual(set(orders.mapped("grove_card_needs_refresh")), {True})

    def test_a_card_that_will_not_build_fails_without_breaking_the_run(self):
        first, second = self._imported(2)
        real = grove_main._order_card_payload

        def build(order, *args, **kwargs):
            if order.id == first.id:
                order.env.cr.execute("SELECT 1/0")
            return real(order, *args, **kwargs)

        with patch.object(grove_main, "_order_card_payload", side_effect=build):
            self._commit_import(_reply(200, {"ok": True}))
        self.assertTrue(first.grove_card_needs_refresh)
        self.assertFalse(second.grove_card_needs_refresh)

    def test_each_edit_is_committed_outside_tests(self):
        orders = self._imported(2)
        with (
            patch.dict("os.environ", ENV),
            patch.object(ops_module.requests, "post", return_value=_reply(200, {"ok": True})),
            patch.object(ops_module, "config", {"test_enable": False}),
            patch.object(self.env.cr, "commit") as commit,
        ):
            orders._grove_refresh_cards()
        self.assertEqual(commit.call_count, 2)

    def test_a_label_recorded_outside_any_batch_flags_the_card_too(self):
        order = self._order(checkout="paid", wave=False, state="sale", grove_card_message_id=MESSAGE_ID)
        self.Batch.import_pirateship_file(
            self._csv([[f"{order.name}/1", _T[0], "UPS", "9.10"]]), filename="export.csv", company=self.company
        )
        self.assertEqual(order.grove_tracking_numbers, _T[0])
        self.assertTrue(order.grove_card_needs_refresh)
        post = self._commit_import(_reply(200, {"ok": True}))
        self.assertEqual(post.call_count, 1)
        self.assertFalse(order.grove_card_needs_refresh)

    def test_the_hourly_job_runs_the_refresh(self):
        order = self._imported()
        with (
            patch.dict("os.environ", ENV),
            patch.object(ops_module.requests, "post", return_value=_reply(200, {"ok": True})),
        ):
            self.env["sale.order"]._cron_grove_order_ops(now=_at(START))
        self.assertFalse(order.grove_card_needs_refresh)


@tagged("post_install", "-at_install")
class TestLabelsNoticeRetry(_Fixture, TransactionCase):
    def setUp(self):
        super().setUp()
        self._setup_fixture()

    def _batch(self, ret):
        order = self._order()
        batch = self._build()["batch"]
        with patch(POST_STATUS, return_value=ret):
            self._import(order)
        return batch

    def _cron(self, *rets):
        with patch(POST_STATUS, side_effect=list(rets)) as post:
            self.env["sale.order"]._cron_grove_labels_notices()
        return post

    def test_delivered_notice_leaves_nothing_pending(self):
        self.assertFalse(self._batch("ok").labels_notice_pending)

    def test_unmapped_company_counts_as_cleared(self):
        self.assertFalse(self._batch("unmapped").labels_notice_pending)

    def test_failed_notice_is_retried_by_the_cron_and_cleared(self):
        batch = self._batch("failed")
        self.assertTrue(batch.labels_notice_pending)
        post = self._cron("ok")
        company, _title, lines, buttons = post.call_args.args
        self.assertEqual(company, self.company)
        self.assertEqual(lines, ["Zone 6: 1 label recorded"])
        self.assertEqual(buttons, [{"label": "Mark all shipped", "style": 3, "customId": f"ord:markall:{batch.id}"}])
        self.assertFalse(batch.labels_notice_pending)

    def test_retry_that_fails_stays_pending(self):
        batch = self._batch("failed")
        self._cron("failed")
        self.assertTrue(batch.labels_notice_pending)

    def test_retry_for_an_unmapped_company_clears(self):
        batch = self._batch("failed")
        self._cron("unmapped")
        self.assertFalse(batch.labels_notice_pending)

    def test_batch_with_no_zone_is_cleared_without_a_post(self):
        batch = self.Batch.create(
            {"name": "LB-NOZONE", "company_id": self.company.id, "state": "purchased", "labels_notice_pending": True}
        )
        self._cron().assert_not_called()
        self.assertFalse(batch.labels_notice_pending)

    def test_retry_failures_stop_at_three(self):
        batches = self.Batch
        for index, zone in enumerate(("5", "6", "7", "8")):
            batches |= self.Batch.create(
                {
                    "name": f"LB-STOP-{index}",
                    "company_id": self.company.id,
                    "zone": zone,
                    "state": "purchased",
                    "labels_notice_pending": True,
                }
            )
            self.env["grove.label.batch.line"].create(
                {
                    "batch_id": batches[-1].id,
                    "grove_ref": f"X{index}/1",
                    "order_id": self._order().id,
                    "box_index": 1,
                    "box_id": "p24x10x4",
                    "tracking_number": _T[index],
                }
            )
        post = self._cron(*["failed"] * 4)
        self.assertEqual(post.call_count, 3)

    def test_each_retry_is_committed_outside_tests(self):
        self._batch("failed")
        with (
            patch(POST_STATUS, return_value="ok"),
            patch.object(ops_module, "config", {"test_enable": False}),
            patch.object(self.env.cr, "commit") as commit,
        ):
            self.env["sale.order"]._cron_grove_labels_notices()
        self.assertEqual(commit.call_count, 1)

    def test_failed_notice_still_logs_the_batch(self):
        order = self._order()
        batch = self._build()["batch"]
        with patch(POST_STATUS, return_value="failed"), self.assertLogs(label_batch_module._logger, "ERROR") as logs:
            self._import(order)
        self.assertTrue(any(batch.name in line for line in logs.output), logs.output)


@tagged("post_install", "-at_install")
class TestCardContents(_Fixture, TransactionCase):
    def setUp(self):
        super().setUp()
        self._setup_fixture()

    def test_card_names_the_carrier_once_per_carrier(self):
        order = self._order(
            state="sale", grove_tracking_numbers=f"{_T[0]}\n{_T[1]}", grove_shipping_carriers="UPS\nUPS"
        )
        self.assertEqual(grove_main._order_alert_context(order)["carrier"], "UPS")
        order.sudo().write({"grove_shipping_carriers": "UPS\nUSPS"})
        self.assertEqual(grove_main._order_alert_context(order)["carrier"], "UPS, USPS")

    def test_card_with_no_carrier_says_none(self):
        self.assertIsNone(grove_main._order_alert_context(self._order(state="sale"))["carrier"])

    def test_payload_says_when_the_order_has_shipped(self):
        order = self._order(state="sale")
        self.assertIs(grove_main._order_card_payload(order)["shipped"], False)
        for stage in ("shipped", "delivered"):
            order.sudo().write({"grove_fulfillment_state": stage})
            order.invalidate_recordset()
            self.assertIs(grove_main._order_card_payload(order)["shipped"], True, stage)

    def test_safe_card_survives_a_database_error_in_the_build(self):
        order = self._order(state="sale")

        def broken(scoped, *args, **kwargs):
            scoped.env.cr.execute("SELECT 1/0")

        with (
            patch.object(grove_main, "_order_card_payload", side_effect=broken),
            self.assertLogs(grove_main._logger, "ERROR"),
        ):
            self.assertIsNone(grove_main._safe_order_card(order))
        self.env.cr.execute("SELECT 1")
        self.assertEqual(self.env.cr.fetchone(), (1,))
