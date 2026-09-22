"""Request-content-draft button + Content Drafter group (GOL-2384/C). DB tests."""

from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestContentDraftRequest(GroveTaxFixtureMixin, TransactionCase):
    def _tmpl(self, **vals):
        base = {"name": "Test Fig", "type": "consu"}
        base.update(vals)
        return self.env["product.template"].create(base)

    def _with_facts(self, **vals):
        """A template with a botanical name and one fetched fact in provenance."""
        base = {
            "grove_botanical_name": "Ficus carica",
            "grove_soil": "well-drained",
            "grove_facts_provenance": {
                "grove_soil": {"source": "perenual", "ref": "2001", "at": "2026-09-22T00:00:00"}
            },
        }
        base.update(vals)
        return self._tmpl(**base)

    # ── Guards ──────────────────────────────────────────────────────────
    def test_requires_botanical_name(self):
        t = self._tmpl(grove_facts_provenance={"grove_soil": {"source": "usda", "ref": "X", "at": "z"}})
        with self.assertRaises(UserError):
            t.action_request_draft()
        self.assertEqual(t.grove_draft_state, "none")

    def test_requires_a_fetched_fact(self):
        t = self._tmpl(grove_botanical_name="Ficus carica")
        with self.assertRaises(UserError):
            t.action_request_draft()
        self.assertEqual(t.grove_draft_state, "none")

    def test_exempt_product_cannot_request(self):
        t = self._with_facts(grove_gate_exempt=True)
        with self.assertRaises(UserError):
            t.action_request_draft()
        self.assertEqual(t.grove_draft_state, "none")

    # ── Happy path ──────────────────────────────────────────────────────
    def test_request_sets_state_and_posts_note(self):
        t = self._with_facts()
        before = len(t.message_ids)
        t.action_request_draft()
        self.assertEqual(t.grove_draft_state, "requested")
        self.assertGreater(len(t.message_ids), before, "a chatter note should be posted")

    def test_request_preserves_facts_reviewed(self):
        """Setting the draft state must not stamp provenance, so a human sign-off survives."""
        t = self._with_facts(grove_facts_reviewed=True)
        t.action_request_draft()
        self.assertEqual(t.grove_draft_state, "requested")
        self.assertTrue(t.grove_facts_reviewed, "requesting a draft must not clear Facts reviewed")

    # ── Security group ──────────────────────────────────────────────────
    def test_content_drafter_group_and_acl(self):
        group = self.env.ref("grove_headless.group_content_drafter")
        self.assertIn(
            self.env.ref("base.group_user"),
            group.implied_ids,
            "Content Drafter implies Internal User so the service account can post chatter",
        )
        access = self.env["ir.model.access"].search(
            [
                ("group_id", "=", group.id),
                ("model_id.model", "=", "product.template"),
            ]
        )
        self.assertTrue(access, "the group carries a product.template ACL")
        self.assertTrue(all(a.perm_read and a.perm_write for a in access))
        self.assertFalse(any(a.perm_create or a.perm_unlink for a in access))
