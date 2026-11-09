"""Every Discord post names the Odoo instance that sent it.

Prod, QA and any restored copy share one ops webhook, and the audit text carried
no marker, so a stale QA/clone post is indistinguishable from prod in the
channel (2026-11-09 hunt: a Saturday prod post was chased as a live incident
across four environments). The tag is the host of ``web.base.url`` — the one
value every environment already sets differently — with the database name as
the fallback when the parameter is blank.
"""

import os
from unittest.mock import patch

from odoo.addons.grove_headless.controllers import main as controllers_main
from odoo.addons.grove_headless.models.discord_tag import discord_env_tag, tag_discord_message
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestDiscordEnvTag(TransactionCase):
    def _set_base_url(self, value):
        self.env["ir.config_parameter"].sudo().set_param("web.base.url", value)

    def test_tag_is_host_of_web_base_url(self):
        self._set_base_url("https://odoo.qa.example.test")
        self.assertEqual(discord_env_tag(self.env), "[odoo.qa.example.test]")

    def test_tag_keeps_a_non_default_port(self):
        self._set_base_url("http://localhost:8069")
        self.assertEqual(discord_env_tag(self.env), "[localhost:8069]")

    def test_tag_falls_back_to_database_name_when_base_url_blank(self):
        self._set_base_url("")
        self.assertEqual(discord_env_tag(self.env), f"[db:{self.env.cr.dbname}]")

    def test_tag_message_prefixes_with_one_space(self):
        self._set_base_url("https://odoo.example.test")
        self.assertEqual(tag_discord_message(self.env, "7 things"), "[odoo.example.test] 7 things")

    # ── controllers.main._notify_discord ──────────────────────────────────

    def test_notify_discord_prefixes_when_env_given(self):
        self._set_base_url("https://odoo.example.test")
        with (
            patch.dict(os.environ, {"DISCORD_OPS_WEBHOOK_URL": "https://discord.example/ops"}, clear=True),
            patch("odoo.addons.grove_headless.controllers.main.requests") as mock_requests,
        ):
            controllers_main._notify_discord("Order S1 placed", env=self.env)
        _, kwargs = mock_requests.post.call_args
        self.assertEqual(kwargs["json"]["content"], "[odoo.example.test] Order S1 placed")
        # The mention guard must survive the prefix.
        self.assertEqual(kwargs["json"]["allowed_mentions"], {"parse": []})

    def test_notify_discord_without_env_or_request_posts_untagged(self):
        with (
            patch.dict(os.environ, {"DISCORD_OPS_WEBHOOK_URL": "https://discord.example/ops"}, clear=True),
            patch("odoo.addons.grove_headless.controllers.main.requests") as mock_requests,
            patch("odoo.addons.grove_headless.controllers.main.request", None),
        ):
            controllers_main._notify_discord("bare ping")
        _, kwargs = mock_requests.post.call_args
        self.assertEqual(kwargs["json"]["content"], "bare ping")
