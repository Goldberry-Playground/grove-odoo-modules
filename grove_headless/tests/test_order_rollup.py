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
            {"DISCORD_ORDERS_WEBHOOK_URL": "https://discord.example/orders", "DISCORD_OPS_WEBHOOK_URL": "https://discord.example/ops"},
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
