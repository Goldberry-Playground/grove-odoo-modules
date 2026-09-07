"""Odoo-runtime tests for the grove_support livechat glue (GOL-2022).

Run under Odoo's --test-enable runner (the install-smoke-test CI job), not plain
pytest — these are TransactionCase tests against a real DB.

Coverage focuses on grove_support's OWN logic — the four jobs and the
orchestration/idempotency around them — plus the chat-start Discord hook on
``create()``. The email-extraction seam
(``_chatbot_find_customer_values_in_messages``) is Odoo's own, tested upstream;
we patch it where an email is needed rather than rebuild fragile chatbot-script
fixtures (the doc explicitly warns against inheriting broken fixtures). The
``_forward_human_operator`` override is a thin best-effort wrapper around
``_grove_support_process_chat``, which is exercised directly here.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from odoo.addons.grove_support.models import discuss_channel as gs_channel
from odoo.tests.common import TransactionCase, tagged


@tagged("grove_support", "post_install", "-at_install")
class GroveSupportCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.other_company = cls.env["res.company"].create({"name": "Other Grove Co"})
        cls.livechat_channel = cls.env["im_livechat.channel"].create({"name": "Grove Support"})

    def _make_channel(self):
        """A livechat discuss.channel with the current user as operator.

        Creating it fires the chat-start hook; there is no DISCORD_OPS_WEBHOOK_URL
        in the test env so notify_discord is a no-op."""
        return self.env["discuss.channel"].create(
            {
                "name": "Support conversation",
                "channel_type": "livechat",
                "livechat_channel_id": self.livechat_channel.id,
                "livechat_operator_id": self.env.user.partner_id.id,
            }
        )

    # ── Job 1: partner match (checkout discipline) ───────────────────────

    def test_match_reuses_existing_partner_as_is(self):
        existing = self.env["res.partner"].create(
            {"name": "Real Name", "email": "reuse@example.com", "company_id": self.company.id}
        )
        channel = self._make_channel()
        matched = channel._grove_support_match_partner("reuse@example.com", self.company)
        self.assertEqual(matched, existing)
        # Never-overwrite: the chat input did not mutate the stored record.
        self.assertEqual(matched.name, "Real Name")

    def test_match_creates_partner_when_absent(self):
        channel = self._make_channel()
        matched = channel._grove_support_match_partner("fresh@example.com", self.company)
        self.assertTrue(matched)
        self.assertEqual(matched.email, "fresh@example.com")
        self.assertEqual(matched.company_id, self.company)

    def test_match_is_company_scoped(self):
        """A partner in another company must NOT be returned; a company-less
        (shared) partner MUST match."""
        other = self.env["res.partner"].create(
            {"name": "Elsewhere", "email": "scoped@example.com", "company_id": self.other_company.id}
        )
        channel = self._make_channel()
        matched = channel._grove_support_match_partner("scoped@example.com", self.company)
        self.assertNotEqual(matched, other)
        self.assertEqual(matched.company_id, self.company)

        shared = self.env["res.partner"].create({"name": "Shared", "email": "shared@example.com", "company_id": False})
        matched_shared = channel._grove_support_match_partner("shared@example.com", self.company)
        self.assertEqual(matched_shared, shared)

    # ── Job 2: lead creation ─────────────────────────────────────────────

    def test_create_lead_links_and_tags(self):
        partner = self.env["res.partner"].create(
            {"name": "Lead Person", "email": "lead@example.com", "company_id": self.company.id}
        )
        channel = self._make_channel()
        lead = channel._grove_support_create_lead(partner, "lead@example.com", self.company)
        self.assertEqual(lead.partner_id, partner)
        self.assertEqual(lead.type, "lead")
        self.assertEqual(lead.email_from, "lead@example.com")
        self.assertIn("livechat", lead.tag_ids.mapped("name"))
        # Transcript deep-link present in the description.
        self.assertIn("model=discuss.channel", lead.description or "")
        self.assertIn(f"id={channel.id}", lead.description or "")

    def test_livechat_tag_is_reused(self):
        channel = self._make_channel()
        first = channel._grove_support_livechat_tag()
        second = channel._grove_support_livechat_tag()
        self.assertEqual(first, second)
        self.assertEqual(
            self.env["crm.tag"].search_count([("name", "=", "livechat")]),
            1,
            "the livechat crm.tag must be created exactly once",
        )

    # ── Job 3: recent-orders note ────────────────────────────────────────

    def test_recent_orders_note_lists_three_most_recent(self):
        partner = self.env["res.partner"].create(
            {"name": "Buyer", "email": "buyer@example.com", "company_id": self.company.id}
        )
        base = datetime(2026, 1, 1, 12, 0, 0)
        orders = []
        for i in range(4):
            orders.append(
                self.env["sale.order"].create(
                    {
                        "partner_id": partner.id,
                        "company_id": self.company.id,
                        "date_order": base + timedelta(days=i),
                    }
                )
            )
        oldest = orders[0]  # base + 0 days -> excluded from the top 3
        channel = self._make_channel()
        channel._grove_support_post_recent_orders(partner, self.company)

        note = channel.message_ids.filtered(
            lambda m: m.subtype_id == self.env.ref("mail.mt_note") and "Recent orders" in (m.body or "")
        )
        self.assertEqual(len(note), 1, "exactly one recent-orders note should be posted")
        body = note.body
        for recent in orders[1:]:
            self.assertIn(recent.name, body)
        self.assertNotIn(oldest.name, body)

    def test_recent_orders_note_when_no_orders(self):
        partner = self.env["res.partner"].create(
            {"name": "No Orders", "email": "noorders@example.com", "company_id": self.company.id}
        )
        channel = self._make_channel()
        channel._grove_support_post_recent_orders(partner, self.company)
        note = channel.message_ids.filtered(lambda m: "Recent orders" in (m.body or ""))
        self.assertEqual(len(note), 1)
        self.assertIn("none on file", note.body)

    # ── Orchestration + idempotency ──────────────────────────────────────

    def test_process_chat_creates_partner_lead_and_note(self):
        channel = self._make_channel()
        with patch.object(
            type(channel),
            "_chatbot_find_customer_values_in_messages",
            return_value={"email_from": "Chat@Example.com "},
        ):
            channel._grove_support_process_chat()

        self.assertTrue(channel.grove_support_lead_id)
        lead = channel.grove_support_lead_id
        partner = lead.partner_id
        # email_normalize lowercases + strips before the partner is matched.
        self.assertEqual(partner.email, "chat@example.com")
        self.assertIn("livechat", lead.tag_ids.mapped("name"))
        self.assertTrue(
            channel.message_ids.filtered(lambda m: "Recent orders" in (m.body or "")),
            "a recent-orders note should be posted",
        )

    def test_process_chat_is_idempotent(self):
        channel = self._make_channel()
        with patch.object(
            type(channel),
            "_chatbot_find_customer_values_in_messages",
            return_value={"email_from": "dupe@example.com"},
        ):
            channel._grove_support_process_chat()
            first_lead = channel.grove_support_lead_id
            channel._grove_support_process_chat()

        self.assertEqual(channel.grove_support_lead_id, first_lead)
        self.assertEqual(
            self.env["crm.lead"].search_count([("partner_id", "=", first_lead.partner_id.id)]),
            1,
            "a second handoff must not duplicate the lead",
        )

    def test_process_chat_no_email_is_noop(self):
        channel = self._make_channel()
        with patch.object(type(channel), "_chatbot_find_customer_values_in_messages", return_value={}):
            channel._grove_support_process_chat()
        self.assertFalse(channel.grove_support_lead_id)

    def test_process_chat_ignores_invalid_email(self):
        channel = self._make_channel()
        with patch.object(
            type(channel),
            "_chatbot_find_customer_values_in_messages",
            return_value={"email_from": "not-an-email"},
        ):
            channel._grove_support_process_chat()
        self.assertFalse(channel.grove_support_lead_id)

    # ── Job 4: Discord chat-start ping ───────────────────────────────────

    def test_chat_start_pings_discord_for_livechat(self):
        with patch.object(gs_channel, "notify_discord") as mock_notify:
            self._make_channel()
        mock_notify.assert_called_once()

    def test_chat_start_does_not_ping_for_non_livechat(self):
        with patch.object(gs_channel, "notify_discord") as mock_notify:
            self.env["discuss.channel"].create({"name": "team room", "channel_type": "channel"})
        mock_notify.assert_not_called()
