"""Planner learning-loop models, nightly cron, and human-gate actions
(GOL-3549, planner winter beta spec B). Odoo-runtime (DB) tests.

What must hold:
  * the seeded rules equal the shipped constants and the feed helper serves
    them (spec B.1);
  * three removes of one product in one zone band open one priority proposal,
    accepting it moves the product's priority and stamps provenance + clears
    the reviewed sign-off (spec B.4/B.5);
  * three un-reviewed contradictions on a product clear its reviewed sign-off
    and post a chatter line (spec B.4);
  * Accept-as-catalog-candidate creates an unpublished, not-for-sale template
    with the layer from the stratum (spec B.6);
  * Approve-as-guild (staff only) creates a phantom-BoM bundle in the Guilds
    category priced as the sum of the lines (spec B.7);
  * a tester token resolves to a name only while active (spec B.6).
"""

from odoo.addons.grove_headless.services.planner.rules import PLANNER_RULE_DEFAULTS
from odoo.tests import TransactionCase, tagged

from .common import GroveTaxFixtureMixin


@tagged("grove_headless", "post_install", "-at_install")
class TestPlannerLearning(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.Proposal = self.env["grove.planner.proposal"]
        self.Signal = self.env["grove.planner.signal"]
        self.plant = self.env["product.template"].create(
            {"name": "Learning Pawpaw", "type": "consu", "list_price": 30.0}
        )

    # ── Rules + feed ──────────────────────────────────────────────────────────

    def test_seeded_rules_equal_the_constants_and_feed_serves_them(self):
        served = self.env["grove.planner.rule"].rules_for_scope("retail")
        self.assertEqual(served, PLANNER_RULE_DEFAULTS)

    def test_rule_key_scope_is_unique(self):
        from odoo.tools import mute_logger
        from psycopg2 import IntegrityError

        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError):
            with self.cr.savepoint():
                self.env["grove.planner.rule"].create({"key": "priority_weight", "scope": "retail", "value": 9})

    # ── Signals → priority proposal → accept ──────────────────────────────────

    def test_three_removes_open_one_priority_proposal(self):
        for _ in range(3):
            self.Signal.create(
                {"kind": "removed", "product_tmpl_id": self.plant.id, "zone_band": "6-7", "browser_id": "b1"}
            )
        self.Proposal.cron_planner_learning()
        proposals = self.Proposal.search([("kind", "=", "priority"), ("product_tmpl_id", "=", self.plant.id)])
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals.change, {"grove_planner_priority": 60})  # 50 default + 10
        self.assertIn("removed 3 times in zone band 6-7", proposals.reason)
        # Idempotent: a second run does not open a duplicate while one is open.
        self.Proposal.cron_planner_learning()
        self.assertEqual(
            self.Proposal.search_count([("kind", "=", "priority"), ("product_tmpl_id", "=", self.plant.id)]),
            1,
        )

    def test_two_removes_do_not_open_a_proposal(self):
        for _ in range(2):
            self.Signal.create(
                {"kind": "removed", "product_tmpl_id": self.plant.id, "zone_band": "6-7", "browser_id": "b1"}
            )
        self.Proposal.cron_planner_learning()
        self.assertFalse(self.Proposal.search([("product_tmpl_id", "=", self.plant.id)]))

    def test_three_adds_lower_priority(self):
        for _ in range(3):
            self.Signal.create(
                {"kind": "added", "product_tmpl_id": self.plant.id, "zone_band": "8-9", "browser_id": "b1"}
            )
        self.Proposal.cron_planner_learning()
        proposal = self.Proposal.search([("kind", "=", "priority"), ("product_tmpl_id", "=", self.plant.id)])
        self.assertEqual(proposal.change, {"grove_planner_priority": 40})  # 50 default - 10

    def test_accepting_a_priority_proposal_writes_through_and_clears_review(self):
        self.plant.grove_facts_reviewed = True
        proposal = self.Proposal.create(
            {"kind": "priority", "product_tmpl_id": self.plant.id, "change": {"grove_planner_priority": 70}}
        )
        proposal.action_accept()
        self.assertEqual(proposal.status, "accepted")
        self.assertTrue(proposal.decided_by and proposal.decided_at)
        self.assertEqual(self.plant.grove_planner_priority, 70)
        self.assertFalse(self.plant.grove_facts_reviewed)
        prov = self.plant.grove_facts_provenance or {}
        self.assertIn("grove_planner_priority", prov)
        self.assertEqual(prov["grove_planner_priority"]["ref"], f"proposal:{proposal.id}")

    def test_accepting_a_rule_proposal_changes_the_weight(self):
        rule = self.env["grove.planner.rule"].search([("key", "=", "priority_weight"), ("scope", "=", "retail")])
        proposal = self.Proposal.create({"kind": "rule", "rule_id": rule.id, "change": {"value": 5.0}})
        proposal.action_accept()
        self.assertEqual(rule.value, 5.0)

    def test_dismiss_leaves_the_target_untouched(self):
        proposal = self.Proposal.create(
            {"kind": "priority", "product_tmpl_id": self.plant.id, "change": {"grove_planner_priority": 99}}
        )
        proposal.action_dismiss()
        self.assertEqual(proposal.status, "dismissed")
        self.assertEqual(self.plant.grove_planner_priority, 50)

    # ── Contradictions → flag / clear review ──────────────────────────────────

    def test_three_contradictions_clear_review_and_post_chatter(self):
        self.plant.grove_facts_reviewed = True
        before = len(self.plant.message_ids)
        for _ in range(3):
            self.env["grove.planner.evidence"].create(
                {"product_tmpl_id": self.plant.id, "fact": "zone_min", "contradicts": True, "source": "tester"}
            )
        self.Proposal.cron_planner_learning()
        self.assertFalse(self.plant.grove_facts_reviewed)
        msgs = self.plant.message_ids
        self.assertTrue(len(msgs) > before)
        self.assertTrue(any("zone_min" in (m.body or "") for m in msgs))

    def test_reviewed_contradictions_do_not_count(self):
        self.plant.grove_facts_reviewed = True
        for _ in range(3):
            self.env["grove.planner.evidence"].create(
                {
                    "product_tmpl_id": self.plant.id,
                    "fact": "zone_min",
                    "contradicts": True,
                    "source": "tester",
                    "reviewed": True,
                }
            )
        self.Proposal.cron_planner_learning()
        self.assertTrue(self.plant.grove_facts_reviewed)

    def test_mark_reviewed_stamps_who_and_when(self):
        evidence = self.env["grove.planner.evidence"].create(
            {"product_tmpl_id": self.plant.id, "fact": "pollination", "source": "tester"}
        )
        evidence.action_mark_reviewed()
        self.assertTrue(evidence.reviewed)
        self.assertEqual(evidence.reviewed_by, self.env.user)
        self.assertTrue(evidence.reviewed_at)

    # ── Suggestions ────────────────────────────────────────────────────────────

    def test_accept_as_candidate_creates_unpublished_template(self):
        suggestion = self.env["grove.planner.suggestion"].create(
            {"name": "Sea Buckthorn", "stratum": "overstory", "note": "fixes nitrogen", "by": "public"}
        )
        suggestion.action_accept_as_candidate()
        self.assertEqual(suggestion.status, "accepted")
        product = suggestion.product_tmpl_id
        self.assertTrue(product)
        self.assertFalse(product.sale_ok)
        self.assertFalse(product.website_published)
        self.assertEqual(product.grove_layer, "canopy")  # overstory → canopy
        self.assertTrue(any("fixes nitrogen" in (m.body or "") for m in product.message_ids))

    # ── Saved plan → guild ─────────────────────────────────────────────────────

    def test_approve_as_guild_builds_a_phantom_bom_bundle(self):
        self.env.user.group_ids |= self.env.ref("grove_headless.group_nursery_staff")
        collection = self.env["product.public.category"].create({"name": "Guilds", "grove_node_kind": "collection"})
        companion = self.env["product.template"].create({"name": "Comfrey", "type": "consu", "list_price": 8.0})
        plan = self.env["grove.planner.plan"].create(
            {
                "name": "Backyard Food Forest",
                "zone": 6,
                "line_ids": [
                    (0, 0, {"product_tmpl_id": self.plant.id, "qty": 1, "role": "anchor", "stratum": "overstory"}),
                    (0, 0, {"product_tmpl_id": companion.id, "qty": 3, "role": "companion", "stratum": "understory"}),
                ],
            }
        )
        plan.action_approve_as_guild()
        self.assertEqual(plan.status, "guild")
        bundle = plan.guild_product_id
        self.assertTrue(bundle)
        self.assertFalse(bundle.website_published)
        self.assertIn(collection, bundle.public_categ_ids)
        self.assertAlmostEqual(bundle.list_price, 30.0 * 1 + 8.0 * 3)
        bom = (
            self.env["mrp.bom"]._bom_find(bundle.product_variant_id, bom_type="phantom").get(bundle.product_variant_id)
        )
        self.assertTrue(bom and bom.type == "phantom")
        self.assertEqual(len(bom.bom_line_ids), 2)

    def test_approve_as_guild_requires_staff(self):
        from odoo.exceptions import AccessError

        # A plain internal user (no nursery-staff group) may not approve.
        user = self.env["res.users"].create(
            {
                "name": "Plain User",
                "login": "plain_planner_user",
                "group_ids": [(6, 0, [self.env.ref("base.group_user").id])],
            }
        )
        self.env["product.public.category"].create({"name": "Guilds", "grove_node_kind": "collection"})
        plan = self.env["grove.planner.plan"].create(
            {
                "name": "No Rights Plan",
                "line_ids": [(0, 0, {"product_tmpl_id": self.plant.id, "qty": 1})],
            }
        )
        with self.assertRaises(AccessError):
            plan.with_user(user).action_approve_as_guild()

    # ── Tester tokens ──────────────────────────────────────────────────────────

    def test_tester_token_resolves_only_when_active(self):
        tester = self.env["grove.planner.tester"].create({"name": "Ada Agent", "token": "tok-123"})
        Tester = self.env["grove.planner.tester"]
        self.assertEqual(Tester.name_for_token("tok-123"), "Ada Agent")
        self.assertIsNone(Tester.name_for_token("nope"))
        self.assertIsNone(Tester.name_for_token(None))
        tester.active = False
        self.assertIsNone(Tester.name_for_token("tok-123"))
