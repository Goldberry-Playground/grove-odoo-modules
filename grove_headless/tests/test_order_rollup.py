"""Weekly order rollup Discord routing (GOL-1978 wrong-channel fix): the digest
is an order/pickup summary, so it must post to the same channel as individual
order pings (DISCORD_ORDERS_WEBHOOK_URL) — not the bot-logs/ops channel it was
mistakenly hardcoded to. Mirrors controllers/main._notify_discord's fallback
contract (orders webhook wins; falls back to ops so a missing orders webhook
surfaces visibly instead of silently dropping the rollup)."""

from unittest import mock

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestOrderRollupDiscordRouting(TransactionCase):
    def test_prefers_orders_webhook_over_ops(self):
        rollup = self.env["grove.order.rollup"]
        with mock.patch.dict(
            "os.environ",
            {
                "DISCORD_ORDERS_WEBHOOK_URL": "https://discord.example/orders",
                "DISCORD_OPS_WEBHOOK_URL": "https://discord.example/ops",
            },
        ):
            with mock.patch("odoo.addons.grove_headless.models.order_rollup.requests.post") as post:
                rollup._discord_digest(self.env.company, "weekly rollup body")
        post.assert_called_once()
        self.assertEqual(post.call_args[0][0], "https://discord.example/orders")

    def test_falls_back_to_ops_when_orders_webhook_unset(self):
        rollup = self.env["grove.order.rollup"]
        with mock.patch.dict("os.environ", {"DISCORD_OPS_WEBHOOK_URL": "https://discord.example/ops"}, clear=True):
            with mock.patch("odoo.addons.grove_headless.models.order_rollup.requests.post") as post:
                rollup._discord_digest(self.env.company, "weekly rollup body")
        post.assert_called_once()
        self.assertEqual(post.call_args[0][0], "https://discord.example/ops")

    def test_noop_when_neither_webhook_is_set(self):
        rollup = self.env["grove.order.rollup"]
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("odoo.addons.grove_headless.models.order_rollup.requests.post") as post:
                rollup._discord_digest(self.env.company, "weekly rollup body")
        post.assert_not_called()


@tagged("post_install", "-at_install")
class TestNotifyDiscordTopicChannels(TransactionCase):
    """Topic routing for controllers/main._notify_discord (Josh 2026-10-30):
    sales and shipping alerts prefer their own webhook and otherwise fall back
    to the orders -> ops chain, so an unset topic webhook never drops an alert."""

    def _url(self, env, channel):
        from odoo.addons.grove_headless.controllers import main as grove_main

        with mock.patch.dict("os.environ", env, clear=True):
            return grove_main._discord_webhook_url(channel)

    def test_topic_webhook_wins(self):
        env = {
            "DISCORD_SALES_WEBHOOK_URL": "https://d/sales",
            "DISCORD_SHIPPING_WEBHOOK_URL": "https://d/shipping",
            "DISCORD_ORDERS_WEBHOOK_URL": "https://d/orders",
        }
        self.assertEqual(self._url(env, "sales"), "https://d/sales")
        self.assertEqual(self._url(env, "shipping"), "https://d/shipping")
        self.assertEqual(self._url(env, None), "https://d/orders")

    def test_unset_topic_falls_back_to_orders_then_ops(self):
        self.assertEqual(self._url({"DISCORD_ORDERS_WEBHOOK_URL": "https://d/orders"}, "sales"), "https://d/orders")
        self.assertEqual(self._url({"DISCORD_OPS_WEBHOOK_URL": "https://d/ops"}, "shipping"), "https://d/ops")
        self.assertEqual(self._url({}, "sales"), "")

    def test_unknown_channel_uses_orders_chain(self):
        self.assertEqual(self._url({"DISCORD_ORDERS_WEBHOOK_URL": "https://d/orders"}, "nope"), "https://d/orders")
