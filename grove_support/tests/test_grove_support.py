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

    def _make_lead(self, partner):
        return self.env["crm.lead"].create({"name": "Livechat lead", "partner_id": partner.id})

    def _orders_notes(self, record):
        return record.message_ids.filtered(lambda m: "Recent orders" in (m.body or ""))

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
        lead = self._make_lead(partner)
        channel._grove_support_post_recent_orders(lead, partner, self.company)

        note = self._orders_notes(lead).filtered(lambda m: m.subtype_id == self.env.ref("mail.mt_note"))
        self.assertEqual(len(note), 1, "exactly one recent-orders note should be posted on the lead")
        self.assertEqual(note.author_id, self.env.ref("base.partner_root"))
        body = note.body
        for recent in orders[1:]:
            self.assertIn(recent.name, body)
        self.assertNotIn(oldest.name, body)

    def test_recent_orders_note_when_no_orders(self):
        partner = self.env["res.partner"].create(
            {"name": "No Orders", "email": "noorders@example.com", "company_id": self.company.id}
        )
        channel = self._make_channel()
        lead = self._make_lead(partner)
        channel._grove_support_post_recent_orders(lead, partner, self.company)
        note = self._orders_notes(lead)
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
        self.assertTrue(self._orders_notes(lead), "a recent-orders note should be posted on the lead")

    def test_orders_never_posted_into_visitor_channel(self):
        """Regression guard (2026-10-28 leak): the visitor is a member of the
        livechat channel and sees every message in it, mt_note included. Order
        history must never be posted there — the email that selects the partner
        is unverified chat input, so anyone could pull a customer's orders."""
        partner = self.env["res.partner"].create(
            {"name": "Victim", "email": "victim@example.com", "company_id": self.company.id}
        )
        order = self.env["sale.order"].create({"partner_id": partner.id, "company_id": self.company.id})
        channel = self._make_channel()
        with patch.object(
            type(channel),
            "_chatbot_find_customer_values_in_messages",
            return_value={"email_from": "victim@example.com"},
        ):
            channel._grove_support_process_chat()

        self.assertFalse(self._orders_notes(channel), "order history leaked into the visitor's chat")
        self.assertFalse(
            channel.message_ids.filtered(lambda m: order.name in (m.body or "")),
            "an order reference leaked into the visitor's chat",
        )
        self.assertIn(order.name, self._orders_notes(channel.grove_support_lead_id).body)

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

    # ── GOL-3459 defect 1: lead on the no-operator (chatbot-advance) path ──

    def _make_step(self):
        """A trivial chatbot step to advance the channel onto (GOL-3459). The
        step type is irrelevant here — we only need a valid id to write into
        ``chatbot_current_step_id`` to fire the step-advance seam."""
        script = self.env["chatbot.script"].create({"title": "Nursery Support Capture Bot"})
        return self.env["chatbot.script.step"].create(
            {"chatbot_script_id": script.id, "step_type": "text", "message": "Hi"}
        )

    def test_step_advance_creates_lead_without_operator(self):
        """The main GOL-3459 path: no operator ever forwards, the chatbot just
        advances past the email step. The step-advance write must create the
        lead — this is the case that produced zero leads on prod QA."""
        step = self._make_step()
        channel = self._make_channel()
        # Deliberately no live operator handoff: we never call
        # _forward_human_operator; only the chatbot advances its step.
        with patch.object(
            type(channel),
            "_chatbot_find_customer_values_in_messages",
            return_value={"email_from": "offline@example.com"},
        ):
            channel.chatbot_current_step_id = step.id

        self.assertTrue(channel.grove_support_lead_id, "a lead must be created when the bot advances past email")
        self.assertEqual(channel.grove_support_lead_id.partner_id.email, "offline@example.com")
        self.assertIn("livechat", channel.grove_support_lead_id.tag_ids.mapped("name"))

    def test_step_advance_before_email_is_noop(self):
        """Advancing steps before the email is collected must not create a lead;
        the no-email guard in _grove_support_process_chat holds."""
        step = self._make_step()
        channel = self._make_channel()
        with patch.object(type(channel), "_chatbot_find_customer_values_in_messages", return_value={}):
            channel.chatbot_current_step_id = step.id
        self.assertFalse(channel.grove_support_lead_id)

    def test_step_advance_does_not_duplicate_existing_lead(self):
        """A second step write on a conversation that already has a lead is a
        no-op — the three lead seams must never collectively dupe a lead."""
        step = self._make_step()
        channel = self._make_channel()
        with patch.object(
            type(channel),
            "_chatbot_find_customer_values_in_messages",
            return_value={"email_from": "once@example.com"},
        ):
            channel.chatbot_current_step_id = step.id
            first_lead = channel.grove_support_lead_id
            channel.chatbot_current_step_id = step.id

        self.assertEqual(channel.grove_support_lead_id, first_lead)
        self.assertEqual(
            self.env["crm.lead"].search_count([("partner_id", "=", first_lead.partner_id.id)]),
            1,
            "a second step advance must not duplicate the lead",
        )

    def test_non_chatbot_write_does_not_create_lead(self):
        """A plain write that does not touch chatbot_current_step_id must not
        run the glue, even when an email would be resolvable."""
        channel = self._make_channel()
        with patch.object(
            type(channel),
            "_chatbot_find_customer_values_in_messages",
            return_value={"email_from": "noise@example.com"},
        ):
            channel.name = "Renamed conversation"
        self.assertFalse(channel.grove_support_lead_id)

    # ── GOL-3459 defect 2: lead/partner land in the channel's company ─────

    def test_company_prefers_livechat_channel_company(self):
        """The explicit channel company wins over the operator's company, so a
        nursery chat handled by a farm-company operator still files under the
        nursery."""
        self.livechat_channel.company_id = self.other_company
        channel = self._make_channel()  # operator is the current user (company 1)
        self.assertEqual(channel._grove_support_company(), self.other_company)

    def test_company_falls_back_to_operator_when_channel_unset(self):
        channel = self._make_channel()
        self.assertFalse(self.livechat_channel.company_id)
        self.assertEqual(channel._grove_support_company(), self.env.user.company_id)

    def test_lead_and_partner_land_in_channel_company(self):
        self.livechat_channel.company_id = self.other_company
        channel = self._make_channel()
        with patch.object(
            type(channel),
            "_chatbot_find_customer_values_in_messages",
            return_value={"email_from": "nursery@example.com"},
        ):
            channel._grove_support_process_chat()

        lead = channel.grove_support_lead_id
        self.assertEqual(lead.company_id, self.other_company)
        self.assertEqual(lead.partner_id.company_id, self.other_company)

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

    # ── Duplicate-chat fix: livechat_service_patch.js ships in the embed ──

    def test_persist_patch_is_in_both_embed_bundles(self):
        """The persist() patch must reach the widget the storefronts load.
        /im_livechat/assets_embed.js serves ``assets_embed_cors`` cross-origin
        (the storefronts) and ``assets_embed_external`` otherwise; both include
        ``assets_embed_core``, where the manifest registers the patch. The JS
        behaviour itself was verified in a browser against a cross-origin page
        (second send after ~5 s: two channels without the patch, one with)."""
        patch_url = "/grove_support/static/src/embed/livechat_service_patch.js"
        for bundle in ("im_livechat.assets_embed_cors", "im_livechat.assets_embed_external"):
            files, _external = self.env["ir.qweb"]._get_asset_content(bundle)
            self.assertIn(patch_url, [f["url"] for f in files], f"{bundle} is missing the duplicate-chat patch")
