"""grove.planner.proposal — the nightly learning loop and the human gate on it
(GOL-3549, planner winter beta spec B.4/B.5).

Machines propose, a named human accepts. The nightly ``cron_planner_learning``
turns 30 days of signals into priority proposals and counts contradictions into
a product flag; it never changes a fact or a weight on its own. Accepting a
proposal applies its exact ``change`` through ``write()`` (so the target's
chatter shows it), stamps ``grove_facts_provenance`` with ``proposal:<id>`` and
clears the human ``grove_facts_reviewed`` sign-off on a product change — the
content gate's rule that every machine-origin write clears review.
"""

import json
import logging
import os
from datetime import timedelta

import requests
from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Signals older than this never feed a proposal (spec B.4: "last 30 days").
SIGNAL_TALLY_DAYS = 30
# N occurrences of one kind for one product in one zone band opens a proposal.
SIGNAL_PROPOSAL_THRESHOLD = 3
# N un-reviewed contradictions on one fact flags the product.
CONTRADICTION_FLAG_THRESHOLD = 3
# Each accepted priority proposal moves the product's global planner priority by
# this much (lower priority number = recommended sooner).
PRIORITY_STEP = 10


class GrovePlannerProposal(models.Model):
    _name = "grove.planner.proposal"
    _description = "Planner learning-loop proposal (human-accepted)"
    _order = "create_date desc, id desc"

    kind = fields.Selection(
        [("priority", "Priority"), ("rule", "Rule weight"), ("fact", "Fact")],
        string="Kind",
        required=True,
        index=True,
    )
    product_tmpl_id = fields.Many2one("product.template", string="Product", ondelete="cascade", index=True)
    rule_id = fields.Many2one("grove.planner.rule", string="Rule", ondelete="cascade")
    change = fields.Json(
        string="Change",
        help='The exact write accepting this proposal applies, e.g. {"grove_planner_priority": 60} or {"value": 1.0}.',
    )
    # Read-only text rendering of the Json so the form can show the pending
    # write without a json field widget (the web client has none); mirrors the
    # grove_enrich_hints_display pattern.
    change_display = fields.Char(string="Change (text)", compute="_compute_change_display", readonly=True)
    reason = fields.Text(string="Reason")
    status = fields.Selection(
        [("pending", "Pending"), ("accepted", "Accepted"), ("dismissed", "Dismissed")],
        string="Status",
        default="pending",
        required=True,
        index=True,
    )
    decided_by = fields.Many2one("res.users", string="Decided By", readonly=True)
    decided_at = fields.Datetime(string="Decided At", readonly=True)

    @api.depends("change")
    def _compute_change_display(self):
        for proposal in self:
            proposal.change_display = json.dumps(proposal.change) if proposal.change else ""

    # ── Human gate ──────────────────────────────────────────────────────────

    def action_accept(self):
        """Apply ``change`` to the target through ``write()`` and stamp provenance.

        Idempotent per row: a non-pending proposal is skipped. A product change
        also clears ``grove_facts_reviewed`` so a reviewer must re-sign (spec
        B.5). A rule change writes the new weight, which the feed serves within
        the cache window with no republish.
        """
        now = fields.Datetime.now()
        for proposal in self:
            if proposal.status != "pending":
                continue
            change = proposal.change or {}
            if not isinstance(change, dict) or not change:
                raise UserError(f"Proposal {proposal.id} has no change to apply.")
            if proposal.kind == "rule":
                if not proposal.rule_id:
                    raise UserError(f"Proposal {proposal.id} is a rule proposal with no rule.")
                proposal.rule_id.write(dict(change))
            else:
                product = proposal.product_tmpl_id
                if not product:
                    raise UserError(f"Proposal {proposal.id} has no product to change.")
                vals = dict(change)
                provenance = dict(product.grove_facts_provenance or {})
                stamp = {"source": "proposal", "ref": f"proposal:{proposal.id}", "at": now.isoformat()}
                for key in change:
                    provenance[key] = stamp
                vals["grove_facts_provenance"] = provenance
                # Explicit so a non-fact field (grove_planner_priority) still
                # clears the sign-off — product_template.write only auto-clears
                # when a *fact* field is written (spec B.5: "on a product change").
                vals["grove_facts_reviewed"] = False
                product.write(vals)
            proposal.write({"status": "accepted", "decided_by": self.env.user.id, "decided_at": now})

    def action_dismiss(self):
        self.write(
            {
                "status": "dismissed",
                "decided_by": self.env.user.id,
                "decided_at": fields.Datetime.now(),
            }
        )

    # ── Nightly learning loop ────────────────────────────────────────────────

    @api.model
    def cron_planner_learning(self):
        """Signals → priority proposals; contradictions → product flag; Discord
        summary. Runs after the listing-content audit on the same channel
        (spec B.4). Never changes a fact or a weight on its own.
        """
        cutoff = fields.Datetime.now() - timedelta(days=SIGNAL_TALLY_DAYS)
        new_proposals = self._tally_signals_to_proposals(cutoff)
        flagged = self._count_contradictions_to_flags()
        self._post_learning_summary(new_proposals, flagged)
        return {"new_proposals": len(new_proposals), "flagged": len(flagged)}

    @api.model
    def _tally_signals_to_proposals(self, cutoff):
        """Open a priority proposal when a product is removed (or added) >= the
        threshold in one zone band and no priority proposal is already open for
        it. Returns the created proposals.
        """
        Signal = self.env["grove.planner.signal"].sudo()
        groups = Signal._read_group(
            domain=[
                ("create_date", ">=", fields.Datetime.to_string(cutoff)),
                ("kind", "in", ("removed", "added")),
                ("product_tmpl_id", "!=", False),
                ("zone_band", "!=", False),
            ],
            groupby=["product_tmpl_id", "zone_band", "kind"],
            aggregates=["__count"],
        )
        created = self.browse()
        cutoff_date = fields.Datetime.to_string(cutoff)[:10]
        for product, band, kind, count in groups:
            if count < SIGNAL_PROPOSAL_THRESHOLD:
                continue
            # One open priority proposal per product at a time (spec B.4).
            if self.search_count(
                [("kind", "=", "priority"), ("product_tmpl_id", "=", product.id), ("status", "=", "pending")]
            ):
                continue
            current = product.grove_planner_priority or 0
            # removed => recommend less (raise the number); added => the opposite.
            delta = PRIORITY_STEP if kind == "removed" else -PRIORITY_STEP
            new_value = max(0, current + delta)
            created |= self.create(
                {
                    "kind": "priority",
                    "product_tmpl_id": product.id,
                    "change": {"grove_planner_priority": new_value},
                    "reason": f"{kind} {count} times in zone band {band} since {cutoff_date}",
                }
            )
        return created

    @api.model
    def _count_contradictions_to_flags(self):
        """Three un-reviewed contradictions on one product+fact flag the product
        and clear its reviewed sign-off; a fact that drops below the threshold
        clears the flag only when no proposal still references that product
        (spec B.4). Returns the products newly flagged.

        ``grove_planner_flagged`` is a section-A field (GOL-3548); until A lands
        the flag write is skipped so B stays loadable and green on its own. The
        contradiction tally and chatter still run.
        """
        Evidence = self.env["grove.planner.evidence"].sudo()
        Product = self.env["product.template"].sudo()
        has_flag = "grove_planner_flagged" in Product._fields

        groups = Evidence._read_group(
            domain=[("contradicts", "=", True), ("reviewed", "=", False), ("product_tmpl_id", "!=", False)],
            groupby=["product_tmpl_id", "fact"],
            aggregates=["__count"],
        )
        counts_by_product = {}
        for product, fact, count in groups:
            counts_by_product.setdefault(product, {})[fact] = count

        flagged = Product.browse()
        for product, by_fact in counts_by_product.items():
            over = {fact: n for fact, n in by_fact.items() if n >= CONTRADICTION_FLAG_THRESHOLD}
            if over:
                if has_flag and not product.grove_planner_flagged:
                    product.grove_planner_flagged = True
                if product.grove_facts_reviewed:
                    product.grove_facts_reviewed = False
                lines = "; ".join(f"{fact} ({n} open)" for fact, n in sorted(over.items()))
                product.message_post(body=f"Planner: flagged — contradicted facts: {lines}")
                flagged |= product
            elif has_flag and product.grove_planner_flagged:
                # Below threshold: clear only if nothing still references it.
                if not self.search_count([("product_tmpl_id", "=", product.id), ("status", "=", "pending")]):
                    product.grove_planner_flagged = False
        return flagged

    @api.model
    def _post_learning_summary(self, new_proposals, flagged):
        """Best-effort Discord summary on the ops channel (spec B.4), mirroring
        the listing audit's env-var mechanism; a missing webhook no-ops."""
        queued_suggestions = self.env["grove.planner.suggestion"].sudo().search_count([("status", "=", "queued")])
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        link = ""
        action = self.env.ref("grove_headless.action_grove_planner_proposal", raise_if_not_found=False)
        if base and action:
            link = f" {base}/odoo/action-{action.id}"
        text = (
            f"Planner learning loop: {len(new_proposals)} new proposal(s), "
            f"{len(flagged)} product(s) flagged, {queued_suggestions} suggestion(s) queued.{link}"
        )
        url = os.environ.get("DISCORD_OPS_WEBHOOK_URL", "")
        if not url:
            _logger.info("planner learning loop: DISCORD_OPS_WEBHOOK_URL unset — %s", text)
            return
        try:
            requests.post(url, json={"content": text[:2000]}, timeout=10)
        except Exception:
            _logger.warning("planner learning loop: Discord notify failed", exc_info=True)
