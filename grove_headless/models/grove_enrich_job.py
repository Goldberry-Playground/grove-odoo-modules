"""Perenual enrichment job queue + budgeted drain cron (GOL-2391, spec B).

The Fetch-facts button (product.template.action_fetch_facts) runs USDA
synchronously and then enqueues one ``grove.enrich.job`` per product for the
Perenual half. Perenual is never called from the button: it is rate-limited to
a small free-tier daily budget, so it is drained here by a cron under a
per-UTC-day call counter.

Budget accounting (all pure helpers live in services/plant_data/mapping.py so
the queue and the providers agree on the arithmetic):

  * ``counter_key(utc_date)`` -> the ir.config_parameter holding today's real
    Perenual HTTP call count. The date is in the key, so the counter rolls over
    to 0 automatically at UTC midnight — no reset job.
  * ``calls_needed(has_cached_id)`` -> 2 calls (species-list + details), or 1
    when the product already has a resolved grove_perenual_id.
  * ``under_budget(used, needed, budget)`` -> whether the next job still fits.

The cron drains oldest-first and STOPS at the first job that would not fit the
remaining budget (it does not skip ahead to a cheaper job — oldest-first is the
contract). Each real HTTP call increments the counter via the provider's
``on_call`` hook, so the count reflects reality even when a lookup fails partway
through. A genuine HTTP 429 (daily quota) marks the whole day exhausted and
requeues the job for the next UTC day. A 429 with an ``Upgrade Plan`` body is a
*per-species paywall*, not day-exhaustion: that job fails alone, the counter is
left untouched, and the drain continues to the next queued job (otherwise a
single paid-plan species would head-of-line-block the whole catalog every day).
Any other error is retried once, then the job fails with the HTTP status
recorded in ``note``.
"""

from datetime import datetime, timezone

from odoo import api, fields, models

from ..services.plant_data import mapping
from ..services.plant_data.perenual import PerenualPlanGated, PerenualProvider, PerenualRateLimited

# ir.config_parameter key for the daily Perenual call budget. Seeded to 100 by
# data/grove_config_params.xml (noupdate) so an admin can raise/lower it in
# Settings > Technical > System Parameters without a deploy — the adjustability
# is the requirement, so the default must never be a hard-coded constant here.
PERENUAL_BUDGET_PARAM = "grove_headless.perenual_daily_budget"


class GroveEnrichJob(models.Model):
    _name = "grove.enrich.job"
    _description = "Plant-fact enrichment job (Perenual, budgeted)"
    _order = "create_date asc, id asc"  # oldest-first drain

    product_tmpl_id = fields.Many2one(
        "product.template",
        string="Product",
        required=True,
        ondelete="cascade",
        index=True,
    )
    provider = fields.Selection(
        [("perenual", "Perenual")],
        string="Provider",
        required=True,
        default="perenual",
    )
    state = fields.Selection(
        [
            ("queued", "Queued"),
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="State",
        default="queued",
        required=True,
        index=True,
    )
    attempts = fields.Integer(string="Attempts", default=0)
    note = fields.Text(string="Note")

    # ── Budget helpers ──────────────────────────────────────────────────────
    def _budget(self):
        icp = self.env["ir.config_parameter"].sudo()
        raw = icp.get_param(PERENUAL_BUDGET_PARAM, mapping.DEFAULT_DAILY_BUDGET)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return mapping.DEFAULT_DAILY_BUDGET

    def _counter(self, key):
        icp = self.env["ir.config_parameter"].sudo()
        try:
            return int(icp.get_param(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _set_counter(self, key, value):
        self.env["ir.config_parameter"].sudo().set_param(key, str(int(value)))

    def _perenual_provider(self, on_call):
        """Build the provider for a drain. Split out as the test seam — the key
        comes from the PERENUAL_API_KEY container env (never the repo/db)."""
        return PerenualProvider(on_call=on_call)

    def _counter_key_today(self):
        """ir.config_parameter key for today's Perenual call counter (UTC).

        The date is baked into the key, so the counter rolls over to 0 at UTC
        midnight with no reset job — yesterday's spend never blocks today.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return mapping.counter_key(today)

    # ── Cron entry point ────────────────────────────────────────────────────
    @api.model
    def _cron_process_enrich_jobs(self):
        """Drain queued Perenual jobs oldest-first while today's budget allows."""
        # No PERENUAL_API_KEY yet? Leave every job queued rather than draining
        # the backlog into a no-op ``done`` (lookup() short-circuits without an
        # HTTP call when unkeyed). This preserves the "queue now, drain when
        # keyed" contract: products fetched before the key lands still enrich
        # once it does, instead of needing an operator to re-press Fetch.
        if not self._perenual_provider(lambda: None).configured:
            return

        key = self._counter_key_today()
        budget = self._budget()

        jobs = self.search([("state", "=", "queued")])  # _order = oldest-first
        for job in jobs:
            has_cached = bool(job.product_tmpl_id.grove_perenual_id)
            needed = mapping.calls_needed(has_cached)
            used = self._counter(key)
            if not mapping.under_budget(used, needed, budget):
                break  # oldest queued job no longer fits today's budget — stop
            outcome = job._process_one(key, budget)
            if outcome == "rate_limited":
                break  # day exhausted mid-run — leave the rest queued
            # "plan_gated" (paid-plan species) and "failed"/"requeued"/"done"
            # all fall through: only a genuine quota 429 stops the drain.

    # ── Single job ──────────────────────────────────────────────────────────
    def _process_one(self, counter_key, budget):
        """Run one Perenual lookup, apply facts, update job state.

        Returns one of ``done`` / ``failed`` / ``requeued`` / ``rate_limited`` /
        ``plan_gated``. Never raises: every provider error is folded into the
        job note.
        """
        self.ensure_one()
        self.state = "running"
        tmpl = self.product_tmpl_id

        def on_call():
            # one real Perenual HTTP call is about to happen — count it
            self._set_counter(counter_key, self._counter(counter_key) + 1)

        provider = self._perenual_provider(on_call)
        try:
            facts = provider.lookup(tmpl.grove_botanical_name, cached_id=tmpl.grove_perenual_id or None)
            applied = tmpl._grove_apply_facts(facts, "perenual")
            if facts.resolved_id and not tmpl.grove_perenual_id:
                try:
                    tmpl.grove_perenual_id = int(facts.resolved_id)
                except (TypeError, ValueError):
                    pass
            self.state = "done"
            self.note = self._summary(facts, applied)
            return "done"
        except PerenualPlanGated as exc:
            # 429 with an "Upgrade Plan" body: this SPECIES is behind a paid
            # Perenual plan (a permanent paywall, not day-exhaustion). Fail just
            # this job WITHOUT touching the day counter, and let the cron drain
            # the next queued job — one paid-plan species must not stall the
            # whole catalog. Any real calls already made were counted via
            # on_call, so today's spend stays honest.
            self.attempts += 1
            self.state = "failed"
            if exc.species_id and not tmpl.grove_perenual_id:
                # cache the resolved id so a re-press skips the species-list call
                try:
                    tmpl.grove_perenual_id = int(exc.species_id)
                except (TypeError, ValueError):
                    pass
            self.note = (
                "Perenual plan-gated: this species requires a paid Perenual plan "
                f"(HTTP 429 Upgrade Plan). Not a rate limit; other jobs continue. {exc}"
            )
            return "plan_gated"
        except PerenualRateLimited as exc:
            # 429: the day's budget is spent. Exhaust the counter so no other job
            # is attempted today, and requeue this one for the next UTC day.
            self._set_counter(counter_key, budget)
            self.state = "queued"
            self.note = f"Perenual daily budget exhausted (HTTP 429) — retrying next UTC day. {exc}"
            return "rate_limited"
        except Exception as exc:  # noqa: BLE001 — one retry, then fail with status
            self.attempts += 1
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"{type(exc).__name__}: {exc}"
            if status:
                detail += f" (HTTP {status})"
            if self.attempts >= 2:
                self.state = "failed"
                self.note = f"Failed after {self.attempts} attempts — {detail}"
                return "failed"
            self.state = "queued"
            self.note = f"Attempt {self.attempts} failed, will retry — {detail}"
            return "requeued"

    @staticmethod
    def _summary(facts, applied):
        # ``applied`` is what _grove_apply_facts actually WROTE (provider was
        # authoritative-first AND the field was empty), not facts.fields — the
        # provider's raw proposals, most of which are usually skipped.
        filled = ", ".join(sorted(applied)) or "no empty fields to fill"
        parts = [f"Perenual applied: {filled}."]
        if facts.hints:
            parts.append("Notes: " + " | ".join(facts.hints))
        return "\n".join(parts)
