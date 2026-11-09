"""Order lanes and holds on sale.order (Discord order operations).

A hold records "the customer asked us to wait" so the order is not forgotten.
It never ships, charges or emails anything by itself; every state change writes
chatter naming the actor.
"""

import logging
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import config

from .order_lanes import clean_tracking, lane_for, suggest_hold_date, zone_alert_kind
from .shipping_boxes import dormancy_window
from .shipping_calendar import (
    SPRING_BUILD_LEAD_DAYS,
    USDA_ZONE_SELECTION,
    merge_calendar_override,
    preorder_waves,
    spring_build_window,
)

_logger = logging.getLogger(__name__)

# The shop's day, for the hold nudge's "due" and "already nudged today" checks.
_NUDGE_TZ = ZoneInfo("America/New_York")
_NUDGE_FROM_HOUR = 8
# A dead bridge should cost a few timeouts, not one per order.
_NUDGE_MAX_FAILURES = 3
# The late-orders post names this many orders, then says how many more.
_LATE_NAMES_SHOWN = 20
# A card edit can wait out one Discord rate limit inside the bridge.
_CARD_EDIT_TIMEOUT = 15

# Stages where a hold is meaningless: the order is already out the door or finished.
# (label_purchased is deliberately allowed: a bought label can still be held.)
_HOLD_REFUSED_STAGES = ("shipped", "delivered", "collected", "cancelled")

# The box has left. Mark shipped with the same number is a no-op; a shipped order
# (not a delivered one) can also have its label replaced.
_ALREADY_SHIPPED_STAGES = ("shipped", "delivered")
# Stages where an order's label can still be swapped for a different number.
_LABEL_REPLACEABLE_STAGES = ("label_purchased", "shipped")
# Stages Mark shipped can start from. A deposit_paid order also qualifies when it
# has a fall wave, because the wave assignment happens as part of the same submit.
_MARK_SHIPPED_STAGES = ("awaiting_label", "wave_assigned", "label_purchased", *_ALREADY_SHIPPED_STAGES)
# The Mark shipped modal has no carrier field. Pirate Ship sells only UPS and USPS
# and a UPS number is recognised by its 1Z prefix, so anything else is USPS.
_TYPED_LABEL_CARRIER = "USPS"


def _window_start_date(window, today):
    """Date the (start, end) month/day window next opens, or ``today`` when it is open now."""
    (sm, sd), (em, ed) = window
    t = (today.month, today.day)
    inside = (sm, sd) <= t <= (em, ed) if (sm, sd) <= (em, ed) else t >= (sm, sd) or t <= (em, ed)
    if inside:
        return today
    this_year = date(today.year, sm, sd)
    return this_year if this_year >= today else date(today.year + 1, sm, sd)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    grove_lane = fields.Selection(
        [("spring", "Spring wave"), ("individual", "Individual"), ("none", "Not shipped by us")],
        compute="_compute_grove_lane",
        store=True,
        index=True,
        readonly=True,
        copy=False,
    )
    grove_hold_until = fields.Date(readonly=True, copy=False, index=True)
    grove_hold_reason = fields.Char(readonly=True, copy=False)
    grove_hold_set_by = fields.Char(readonly=True, copy=False)
    grove_hold_last_nudge = fields.Date(readonly=True, copy=False)
    # Stamped once the launch repost has put the new-style card in Discord for this
    # order, so a second run (or a resume after a failure) never double-posts.
    grove_card_reposted = fields.Boolean(readonly=True, copy=False)
    # The Discord message holding this order's card, so it can be edited in place.
    grove_card_message_id = fields.Char(readonly=True, copy=False)
    # A label import changed this order: its card still needs editing. Set in the
    # import's transaction, cleared when the edit lands, retried by the hourly job.
    grove_card_needs_refresh = fields.Boolean(readonly=True, copy=False)

    @api.depends("grove_fulfillment", "grove_is_printify", "grove_ship_wave")
    def _compute_grove_lane(self):
        for order in self:
            order.grove_lane = lane_for(order.grove_fulfillment, order.grove_is_printify, order.grove_ship_wave)

    def _grove_require_order_operator(self):
        """Gate for operator actions that write with sudo(): superuser (controllers,
        crons) or a user with access to all sales orders."""
        if not self.env.is_superuser() and not self.env.user.has_group("sales_team.group_sale_salesman_all_leads"):
            raise AccessError("Order actions need access to all sales orders.")

    def _grove_suggested_hold(self):
        """Default hold date to pre-fill the form: the later of dormancy start and
        the zone's ship-window start (the order's own wave, else fall). Does not raise
        on a missing zone (the value only pre-fills a form)."""
        self.ensure_one()
        today = fields.Date.context_today(self)
        dormancy_start = _window_start_date(dormancy_window(self.env), today)
        zone_start = None
        if self.grove_usda_zone:
            from ..controllers.main import _parse_calendar_override

            try:
                calendar = merge_calendar_override(_parse_calendar_override(self.env))
                waves = preorder_waves(int(self.grove_usda_zone), today, calendar)
                wanted = self.grove_ship_wave or "fall"
                wave = next((w for w in waves if w["wave"] == wanted), None)
                if wave:
                    (sm, sd), (em, ed) = wave["ship_window"]
                    zone_start = _window_start_date(((sm, sd), (em, ed)), today)
            except (KeyError, ValueError):
                _logger.warning(
                    "%s: zone %s not in the shipping calendar; hold suggestion uses dormancy only",
                    self.name,
                    self.grove_usda_zone,
                )
        return suggest_hold_date(today, dormancy_start, zone_start)

    def _grove_refuse_finished(self):
        if self.grove_fulfillment_stage in _HOLD_REFUSED_STAGES:
            raise UserError(f"{self.name} is already {self.grove_fulfillment_stage}; it cannot be held.")

    def action_grove_set_hold(self, until, reason, actor):
        self._grove_require_order_operator()
        self.ensure_one()
        reason = (reason or "").strip()
        if not reason:
            raise UserError("Enter a reason for the hold.")
        self._grove_refuse_finished()
        if until < fields.Date.context_today(self):
            raise UserError("The hold date cannot be in the past.")
        self.sudo().write(
            {
                "grove_hold_until": until,
                "grove_hold_reason": reason,
                "grove_hold_set_by": actor,
                "grove_hold_last_nudge": False,
            }
        )
        self.message_post(body=f"{actor} put this order on hold until {until.isoformat()}: {reason}")
        return True

    def action_grove_release_hold(self, actor):
        self._grove_require_order_operator()
        self.ensure_one()
        if not self.grove_hold_until:
            return False
        self.sudo().write(
            {
                "grove_hold_until": False,
                "grove_hold_reason": False,
                "grove_hold_set_by": False,
                "grove_hold_last_nudge": False,
            }
        )
        self.message_post(body=f"{actor} released the hold.")
        return True

    def action_grove_snooze_hold(self, actor, days=7):
        self._grove_require_order_operator()
        self.ensure_one()
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise UserError("Snooze days must be a whole number from 1 to 365.")
        self._grove_refuse_finished()
        if not self.grove_hold_until:
            raise UserError(f"{self.name} has no hold to snooze.")
        until = fields.Date.context_today(self) + timedelta(days=days)
        self.sudo().write({"grove_hold_until": until, "grove_hold_last_nudge": False})
        self.message_post(body=f"{actor} snoozed the hold to {until.isoformat()}.")
        return True

    def action_grove_label_refunded(self, actor):
        """The label was refunded: clear it and send the order back to needing a
        label (a wave order to Assigned to ship wave, any other to Paid, awaiting
        label). Moves no money; the label can be recorded again afterwards."""
        self._grove_require_order_operator()
        self.ensure_one()
        stage = self.grove_fulfillment_stage
        if stage in _ALREADY_SHIPPED_STAGES:
            raise UserError(
                f"{self.name} has already shipped, so a refunded label can't be handled from Discord. "
                "Ask Josh to fix it."
            )
        if stage != "label_purchased":
            raise UserError(
                f"{self.name} has no purchased label to refund. Its status is: {self._grove_stage_label()}."
            )
        old = (self.grove_tracking_numbers or "").split()
        old_text = ", ".join(old) if old else "no number on file"
        target = "wave_assigned" if self.grove_ship_wave else "awaiting_label"
        moved = self.sudo()._grove_advance_state(
            target,
            source="label_refunded",
            operator=actor,
            note=f"{actor} marked the label refunded (was {old_text}). The order needs a label again.",
            allow_backward=True,
        )
        if not moved:
            raise UserError(f"{self.name} could not be sent back to needing a label. Nothing was changed.")
        self.sudo().write(
            {
                "grove_tracking_numbers": False,
                "grove_shipping_carriers": False,
                "grove_shipping_services": False,
                "grove_label_urls": False,
                "grove_actual_shipping_cost": 0.0,
                "grove_label_purchased_at": False,
                "grove_label_batch_id": False,
                "grove_delivery_status": False,
                # So the next label is tracked and earns its delivered email.
                "grove_carrier_poll_stopped": False,
                "grove_carrier_exception_noted": False,
            }
        )
        return True

    def action_grove_move_wave(self, wave, actor):
        """Move an order between the fall and spring waves. Returns True when it
        moved, False when it was already in that wave (a Discord retry). Never
        emails the customer and never touches deposits or payments."""
        self._grove_require_order_operator()
        self.ensure_one()
        if wave not in ("fall", "spring"):
            raise UserError("Pick fall or spring.")
        if self.grove_ship_wave == wave:
            return False
        stage = self.grove_fulfillment_stage
        if self.state == "cancel" or stage == "cancelled":
            raise UserError(f"{self.name} is cancelled; it can't be moved.")
        if stage in _HOLD_REFUSED_STAGES:
            raise UserError(f"{self.name} is already {stage}; it can't be moved.")
        if self.grove_fulfillment != "ship":
            raise UserError(f"{self.name} isn't a shipped order, so it has no ship wave.")
        if (self.grove_tracking_numbers or "").strip():
            raise UserError(f"{self.name} already has a label. Use Label refunded first, then move it.")
        if not self.grove_usda_zone:
            raise UserError(f"{self.name} has no known zone, so it can't be moved. Check its shipping ZIP.")
        old = self.grove_ship_wave or "no wave"
        self.sudo().write({"grove_ship_wave": wave})
        self.message_post(body=f"{actor} moved this order from {old} to {wave}.")
        if wave == "fall":
            self._grove_release_from_unbought_batch()
        return True

    def _grove_release_from_unbought_batch(self):
        """Take an order that has left the spring lane out of its open or exported
        batch. Its row stays on the batch so an import of the old CSV finds it and
        refuses it (``_reconcile_rows``) instead of charging it; a fresh CSV or a
        rebuild leaves it out."""
        batch = self.grove_label_batch_id
        if batch.state not in ("open", "exported"):
            return
        self.sudo().write({"grove_label_batch_id": False})
        batch.sudo().write({"order_ids": [(3, self.id)]})
        bought = (
            " If its label was already bought from that export, refund it in Pirate Ship."
            if batch.state == "exported"
            else ""
        )
        self.message_post(body=f"Order released from {batch.name}: it left the spring lane.{bought}")

    def _grove_needs_wave_for_ship(self):
        return self.grove_fulfillment_stage == "deposit_paid" and self.grove_ship_wave == "fall"

    def _grove_stage_label(self):
        """The order's fulfilment stage in the words shown to staff."""
        stage = self.grove_fulfillment_stage
        return dict(self._fields["grove_fulfillment_state"].selection).get(stage, stage)

    def _grove_refuse_mark_shipped(self, tracking):
        """Raise ``UserError`` when this order cannot be marked shipped with this
        number. The message is shown to the operator as written. An order already
        out the door skips the hold and spring checks, so a retry stays a no-op.
        A cancelled order is refused first: Odoo's own cancel leaves the Grove stage
        alone, so the stage checks below would let it charge and email."""
        stage = self.grove_fulfillment_stage
        if self.state == "cancel" or stage == "cancelled":
            raise UserError(f"{self.name} is cancelled, so it can't be marked shipped.")
        current = (self.grove_tracking_numbers or "").split()
        if stage in _ALREADY_SHIPPED_STAGES and not current:
            raise UserError(
                f"{self.name} already shipped with no tracking number on file, so one can't be added "
                f"from Discord. Its status is: {self._grove_stage_label()}."
            )
        if current and tracking not in current and stage not in _LABEL_REPLACEABLE_STAGES:
            raise UserError(
                f"{self.name} already has tracking {', '.join(current)}, and it can't be changed now. "
                f"Its status is: {self._grove_stage_label()}."
            )
        if stage in _ALREADY_SHIPPED_STAGES:
            return
        if self.grove_hold_until:
            until = self.grove_hold_until
            raise UserError(
                f"{self.name} is on hold until {until:%b} {until.day}, {until.year} "
                f"({self.grove_hold_reason}). Release the hold before marking it shipped."
            )
        if self.grove_lane == "spring" and not current:
            zone = f"Zone {self.grove_usda_zone}" if self.grove_usda_zone else "an unknown zone"
            raise UserError(
                f"{self.name} is a spring order for {zone}. It ships with its zone's batch, "
                "so it needs its batch label before it can be marked shipped."
            )
        if stage not in _MARK_SHIPPED_STAGES and not self._grove_needs_wave_for_ship():
            raise UserError(f"{self.name} can't be marked shipped yet. Its status is: {self._grove_stage_label()}.")

    def _grove_apply_typed_label(self, tracking, cost, who, source):
        """Put the operator's number and cost on the order: record it, replace a
        different label, or leave a matching one alone."""
        current = (self.grove_tracking_numbers or "").split()
        # Read before the write: a settled order was charged on the cost it had then.
        charged_note = ""
        if self.grove_checkout_status == "settled":
            earlier_cost = self.grove_actual_shipping_cost or 0.0
            charged_note = (
                f" The balance was already charged on the earlier cost (${earlier_cost:.2f}), "
                "so this does not change what the customer paid."
            )
        if tracking in current:
            # A retry or a prefilled modal. Before the ship the typed cost is recorded,
            # and an unsettled balance will settle on it. An order settled earlier keeps
            # the charge it has, and the chatter says so. After the ship this is a no-op.
            shipped = self.grove_fulfillment_stage in _ALREADY_SHIPPED_STAGES
            if not shipped and round(cost, 2) != round(self.grove_actual_shipping_cost or 0.0, 2):
                self.write({"grove_actual_shipping_cost": round(cost, 2)})
                self.message_post(body=f"{who} set the cost of label {tracking} to ${cost:.2f}.{charged_note}")
        elif current:
            self.action_grove_replace_label(
                tracking, carrier=_TYPED_LABEL_CARRIER, actual_cost=cost, source=f"{source}, by {who}"
            )
            if charged_note:
                self.message_post(body=f"{who} replaced the label with {tracking} at ${cost:.2f}.{charged_note}")
        else:
            other = self._grove_other_order_with_tracking(tracking)
            if other:
                raise UserError(f"{tracking} is already the label on {other.name}. Check the number and try again.")
            self.action_grove_record_hand_label(tracking, carrier=_TYPED_LABEL_CARRIER, actual_cost=cost)
            self.message_post(body=f"{who} recorded label {tracking} ({self.grove_shipping_carriers}) at ${cost:.2f}.")

    def _grove_ship_with_label(self, tracking, cost, actor, source="discord"):
        """Operator Mark shipped with the label typed in: refuse a held order or a
        spring order with no batch label, assign a fall deposit order to its wave,
        record the label (or replace a different one), then ship and settle.

        The savepoint covers only the wave and the label, so a label failure writes
        nothing. ``_operator_mark_shipped`` runs after it on purpose: rolling back
        after a Stripe charge would erase the record of a real charge. A failure
        there leaves a recorded label on an unshipped order, which is visible and
        retryable. Raises ``UserError`` with a message written for the operator.
        Returns ``_operator_mark_shipped``'s dict."""
        self.ensure_one()
        from ..controllers.main import _operator_mark_shipped

        # Same row lock as action_grove_mark_shipped, taken here so two submits
        # cannot both pass the checks below on a stale read.
        self.env.cr.execute("SELECT id FROM sale_order WHERE id = %s FOR UPDATE", (self.id,))
        self.invalidate_recordset()
        tracking = clean_tracking(tracking)
        self._grove_refuse_mark_shipped(tracking)
        who = actor or "An operator"
        with self.env.cr.savepoint():
            if self._grove_needs_wave_for_ship():
                self.action_grove_assign_wave(wave_ref=f"fall (by {who})")
            self._grove_apply_typed_label(tracking, cost, who, source)
        return _operator_mark_shipped(self.env, self, actor=actor, source=source)

    def _grove_post_notice_status(self, company, title, lines, buttons):
        """POST a notice to the Discord bridge's ``/orders/notice``. Returns
        ``"ok"`` (accepted), ``"unmapped"`` (HTTP 422: no order channel for this
        company, so retrying cannot help) or ``"failed"`` (anything else). Never
        raises."""
        try:
            secret = os.environ.get("ORDER_ALERT_SECRET", "")
            base = os.environ.get("DISCORD_BRIDGE_BASE_URL", "").rstrip("/")
            if not secret or not base:
                _logger.warning("Order notice not sent: the Discord bridge is not configured")
                return "failed"
            resp = requests.post(
                f"{base}/orders/notice",
                json={"companyId": company.id, "title": title, "lines": lines, "buttons": buttons},
                headers={"X-Order-Alert-Secret": secret},
                timeout=10,
            )
            if resp.status_code == 200:
                return "ok"
            _logger.warning("Order notice POST returned %s", resp.status_code)
            if resp.status_code == 422:
                return "unmapped"
        except Exception:  # noqa: BLE001 — a notice is best-effort and must not break the cron
            _logger.warning("Order notice POST failed", exc_info=True)
        return "failed"

    def _grove_hold_nudge_notice(self):
        """Title, lines and buttons for one due hold."""
        self.ensure_one()
        zone = f"Zone {self.grove_usda_zone}" if self.grove_usda_zone else "no zone on file"
        held_by = f" (held by {self.grove_hold_set_by})" if self.grove_hold_set_by else ""
        title = f"{self.name}: held order is due"
        lines = [
            f"Customer: {self.partner_id.name or 'Unknown'}",
            f"Zone: {zone}",
            f"Held for: {self.grove_hold_reason or 'no reason recorded'}{held_by}",
            f"Held until: {self.grove_hold_until.isoformat()}",
            "Is it ready to ship, or should the hold move?",
        ]
        buttons = [
            {"label": "Ready to ship", "style": 3, "customId": f"ord:ready:{self.id}"},
            {"label": "Snooze a week", "style": 2, "customId": f"ord:snooze:{self.id}"},
            {
                "label": "Change date",
                "style": 2,
                "customId": f"ord:holddate:{self.id}:{self.grove_hold_until.isoformat()}",
            },
        ]
        return title, lines, buttons

    @api.model
    def _cron_grove_order_nudges(self, now=None):
        """Hourly: from 8am New York on, post one notice per order whose hold has
        come due (on or before today, New York date) and not been nudged today.
        The notice repeats each morning until the hold is answered. Stamps
        ``grove_hold_last_nudge`` only after a post succeeded; one order's failure
        never stops the rest. ``now`` (UTC, naive or aware) is for tests."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        local = now.astimezone(_NUDGE_TZ)
        if local.hour < _NUDGE_FROM_HOUR:
            return
        today = local.date()
        companies = self.env["res.company"].sudo().search([])
        orders = (
            self.sudo()
            .with_context(allowed_company_ids=companies.ids)
            .search(
                [
                    ("grove_hold_until", "!=", False),
                    ("grove_hold_until", "<=", today),
                    "|",
                    ("grove_hold_last_nudge", "=", False),
                    ("grove_hold_last_nudge", "<", today),
                    ("grove_fulfillment_stage", "not in", list(_HOLD_REFUSED_STAGES)),
                    ("state", "!=", "cancel"),
                ]
            )
        )
        # Skip the per-order commit under the test runner so a TransactionCase
        # stays isolated (same as the settlement retry cron).
        in_test = bool(config["test_enable"])
        failures = 0
        unmapped = set()
        for order in orders:
            company = order.company_id
            if company.id in unmapped:
                continue
            try:
                title, lines, buttons = order._grove_hold_nudge_notice()
                status = order._grove_post_notice_status(company, title, lines, buttons)
                if status == "ok":
                    failures = 0
                    order.write({"grove_hold_last_nudge": today})
                    # A Discord post cannot be undone, so keep its stamp even if a
                    # later order fails and the run rolls back.
                    if not in_test:
                        self.env.cr.commit()
                elif status == "unmapped":
                    unmapped.add(company.id)
                    _logger.error(
                        "Hold nudges skipped for company %s (id %s): the bridge has no order channel for it",
                        company.name,
                        company.id,
                    )
                else:
                    failures += 1
            except Exception:  # noqa: BLE001 — keep nudging the other orders
                _logger.exception("Hold nudge failed for order %s", order.id)
                failures += 1
            if failures >= _NUDGE_MAX_FAILURES:
                _logger.error("Hold nudges stopped after %s consecutive failures; will retry next run", failures)
                break

    @api.model
    def _cron_grove_order_ops(self, now=None):
        """The hourly job: hold nudges, spring zone warnings, the late-orders post,
        then retries of labels-recorded notices and card edits that failed. A
        failure in one never stops the next."""
        for name in (
            "_cron_grove_order_nudges",
            "_cron_grove_zone_alerts",
            "_cron_grove_late_orders",
            "_cron_grove_labels_notices",
            "_cron_grove_card_refresh",
        ):
            try:
                getattr(self, name)(now=now)
            except Exception:  # noqa: BLE001 — the next step must still run
                _logger.exception("%s failed", name)
                if not config["test_enable"]:
                    self.env.cr.rollback()

    @api.model
    def _grove_zone_alert_text(self, kind, zone, today, ship_start, total, held, labelled=0):
        """The one-line warning for a zone, with the true number of days when a
        missed run is catching up. From the start day on it also counts the orders
        labelled but not marked shipped, and says what to do about them."""

        def n(count, noun):
            return f"{count} {noun}" if count == 1 else f"{count} {noun}s"

        if kind == "t5":
            days = (ship_start - today).days
            return f"Zone {zone} spring shipping starts in {n(days, 'day')}: {n(total, 'order')}, {held} on hold."
        if today == ship_start:
            when = "starts today"
        else:
            when = f"started {n((today - ship_start).days, 'day')} ago"
        parts = []
        if total - held:
            parts.append(f"{n(total - held, 'order')} ready to batch")
        if labelled:
            parts.append(f"{labelled} labelled, press Mark all")
        return f"Zone {zone} spring shipping {when}: {', '.join(parts)}."

    @api.model
    def _cron_grove_zone_alerts(self, now=None):
        """Hourly: from 8am New York on, warn once per company, zone and year
        five days before a zone's spring ship start, and once on the day. Only
        zones with orders post. A missed day catches up on the next run (a missed
        5-day warning is skipped once the start day has come). The alert row is
        written only after the bridge accepted the post. ``now`` is for tests."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        local = now.astimezone(_NUDGE_TZ)
        if local.hour < _NUDGE_FROM_HOUR:
            return
        today = local.date()
        from ..controllers.main import _parse_calendar_override

        calendar = merge_calendar_override(_parse_calendar_override(self.env))
        companies = self.env["res.company"].sudo().search([])
        Order = self.sudo().with_context(allowed_company_ids=companies.ids)
        orders = Order.search(
            [
                ("grove_lane", "=", "spring"),
                ("grove_usda_zone", "!=", False),
                ("state", "in", ["sale", "done"]),
                ("grove_fulfillment_stage", "not in", ["shipped", "delivered", "collected", "cancelled"]),
            ]
        )
        # (company, zone) -> [orders with no label, of which on hold, labelled and free to ship]
        counts = {}
        for order in orders:
            entry = counts.setdefault((order.company_id.id, order.grove_usda_zone), [0, 0, 0])
            if (order.grove_tracking_numbers or "").strip():
                entry[2] += 0 if order.grove_hold_until else 1
                continue
            entry[0] += 1
            entry[1] += 1 if order.grove_hold_until else 0

        Alert = self.env["grove.zone.alert"].sudo()
        in_test = bool(config["test_enable"])
        failures = 0
        unmapped = set()
        for (company_id, zone), (total, held, labelled) in sorted(counts.items()):
            if company_id in unmapped:
                continue
            company = companies.browse(company_id)
            try:
                lead_open, ship_end = spring_build_window(int(zone), today, calendar)
                ship_start = lead_open + timedelta(days=SPRING_BUILD_LEAD_DAYS)
                kind = zone_alert_kind(today, ship_start, ship_end)
                if not kind:
                    continue
                # The 5-day warning counts orders still needing a label. Day-of counts
                # those ready to batch and those to mark shipped, so a zone whose
                # orders are all on hold has nothing to say yet.
                if not (total if kind == "t5" else total - held + labelled):
                    continue
                key = [("company_id", "=", company_id), ("zone", "=", zone), ("year", "=", ship_start.year)]
                if Alert.search_count([*key, ("kind", "=", kind)]):
                    continue
                text = self._grove_zone_alert_text(kind, zone, today, ship_start, total, held, labelled)
                status = self._grove_post_notice_status(company, f"Zone {zone} spring shipping", [text], [])
                if status == "ok":
                    failures = 0
                    Alert.create({"company_id": company_id, "zone": zone, "year": ship_start.year, "kind": kind})
                    # A Discord post cannot be undone, so keep its record even if a
                    # later zone fails and the run rolls back.
                    if not in_test:
                        self.env.cr.commit()
                elif status == "unmapped":
                    unmapped.add(company_id)
                    _logger.error(
                        "Zone alerts skipped for company %s (id %s): the bridge has no order channel for it",
                        company.name,
                        company_id,
                    )
                else:
                    failures += 1
            except Exception:  # noqa: BLE001 — keep warning the other zones
                _logger.exception("Zone alert failed for company %s zone %s", company_id, zone)
                failures += 1
            if failures >= _NUDGE_MAX_FAILURES:
                _logger.error("Zone alerts stopped after %s consecutive failures; will retry next run", failures)
                break

    @api.model
    def _grove_late_orders(self, companies):
        """Confirmed spring-lane orders that have not left: not on hold and not
        finished. ``_grove_late_kind`` says what each one is waiting for."""
        return (
            self.sudo()
            .with_context(allowed_company_ids=companies.ids)
            .search(
                [
                    ("grove_lane", "=", "spring"),
                    ("state", "in", ["sale", "done"]),
                    ("grove_hold_until", "=", False),
                    ("grove_fulfillment_stage", "not in", ["shipped", "delivered", "collected", "cancelled"]),
                ]
            )
        )

    def _grove_late_kind(self):
        """What a late spring order is waiting for: ``labelled`` (press Mark all),
        ``built`` (a batch is open or exported but its labels are not imported) or
        ``batch`` (it needs one; an order back from a purchased batch with no
        label counts)."""
        self.ensure_one()
        if (self.grove_tracking_numbers or "").strip():
            return "labelled"
        return "built" if self.grove_label_batch_id.state in ("open", "exported") else "batch"

    @api.model
    def _cron_grove_late_orders(self, now=None):
        """Hourly: from 8am New York on, once per New York day, name the spring
        orders that have not left: those still needing a batch, those in a batch
        whose labels were never imported, and those labelled but not marked shipped.

        One post per company and zone from the zone's ship start through its ship
        end ("Zone 6: 2 spring orders still need a batch."), recorded as
        ``late:YYYY-MM-DD`` after the bridge accepted it. Spring orders with no
        zone can't be in any batch, so they get one company-level post a day
        (stored under zone ``none``) while any zone's spring window is open.
        Zero orders posts and records nothing. Same failure handling as the zone
        alerts. ``now`` is for tests."""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        local = now.astimezone(_NUDGE_TZ)
        if local.hour < _NUDGE_FROM_HOUR:
            return
        today = local.date()
        from ..controllers.main import _parse_calendar_override

        calendar = merge_calendar_override(_parse_calendar_override(self.env))
        companies = self.env["res.company"].sudo().search([])
        orders = self._grove_late_orders(companies)

        def window(zone):
            """(ship_start, ship_end) of the zone's window around today; None when the
            calendar has no such zone."""
            try:
                lead_open, ship_end = spring_build_window(int(zone), today, calendar)
            except (KeyError, ValueError):
                _logger.warning("Late orders: zone %s is not in the shipping calendar", zone)
                return None
            return lead_open + timedelta(days=SPRING_BUILD_LEAD_DAYS), ship_end

        # What to post: (company id, zone key, [(headline, order names)], window year).
        posts = []
        by_zone = {}
        no_zone = {}
        for order in orders:
            kind = order._grove_late_kind()
            if order.grove_usda_zone:
                by_zone.setdefault((order.company_id.id, order.grove_usda_zone), {}).setdefault(kind, []).append(
                    order.name
                )
            elif kind == "batch":
                no_zone.setdefault(order.company_id.id, []).append(order.name)
        for (company_id, zone), kinds in sorted(by_zone.items()):
            span = window(zone)
            if span and span[0] <= today <= span[1]:
                sections = [
                    (self._grove_late_text(zone, kinds[kind], kind), kinds[kind])
                    for kind in ("batch", "built", "labelled")
                    if kind in kinds
                ]
                posts.append((company_id, zone, sections, span[0].year))
        if no_zone:
            # The zone is the thing that is missing, so any open spring window counts.
            open_windows = [w for w in (window(z) for z in dict(USDA_ZONE_SELECTION)) if w and w[0] <= today <= w[1]]
            if open_windows:
                for company_id, names in sorted(no_zone.items()):
                    posts.append((company_id, "none", [(self._grove_late_text(None, names), names)], today.year))

        Alert = self.env["grove.zone.alert"].sudo()
        kind = f"late:{today.isoformat()}"
        in_test = bool(config["test_enable"])
        failures = 0
        unmapped = set()
        for company_id, zone, sections, year in posts:
            if company_id in unmapped:
                continue
            company = companies.browse(company_id)
            try:
                if Alert.search_count(
                    [("company_id", "=", company_id), ("zone", "=", zone), ("year", "=", year), ("kind", "=", kind)]
                ):
                    continue
                lines = []
                for headline, names in sections:
                    shown = ", ".join(names[:_LATE_NAMES_SHOWN])
                    if len(names) > _LATE_NAMES_SHOWN:
                        shown += f" and {len(names) - _LATE_NAMES_SHOWN} more"
                    lines += [headline, f"Orders: {shown}"]
                title = f"Zone {zone} late orders" if zone != "none" else "Spring orders with no zone"
                status = self._grove_post_notice_status(company, title, lines, [])
                if status == "ok":
                    failures = 0
                    Alert.create({"company_id": company_id, "zone": zone, "year": year, "kind": kind})
                    # A Discord post cannot be undone, so keep its record even if a
                    # later post fails and the run rolls back.
                    if not in_test:
                        self.env.cr.commit()
                elif status == "unmapped":
                    unmapped.add(company_id)
                    _logger.error(
                        "Late-order posts skipped for company %s (id %s): the bridge has no order channel for it",
                        company.name,
                        company_id,
                    )
                else:
                    failures += 1
            except Exception:  # noqa: BLE001 — keep posting the other zones
                _logger.exception("Late-order post failed for company %s zone %s", company_id, zone)
                failures += 1
            if failures >= _NUDGE_MAX_FAILURES:
                _logger.error("Late-order posts stopped after %s consecutive failures; will retry next run", failures)
                break

    @api.model
    def _grove_late_text(self, zone, names, kind="batch"):
        """One headline for the late-orders post. ``zone`` None means the orders
        have none; ``kind`` is what they are waiting for (``_grove_late_kind``)."""
        count = len(names)
        noun = "spring order" if count == 1 else "spring orders"
        if kind == "labelled":
            return f"Zone {zone}: {count} labelled, press Mark all."
        if kind == "built":
            return f"Zone {zone}: {count} {noun}, batch built, labels not imported."
        if zone is None:
            verb = "has" if count == 1 else "have"
            return (
                f"{count} {noun} {verb} no zone, so can't be put in a batch. "
                "The zone is unknown and needs fixing on the order."
            )
        verb = "needs" if count == 1 else "need"
        return f"Zone {zone}: {count} {noun} still {verb} a batch."

    @api.model
    def _cron_grove_labels_notices(self, now=None):
        """Hourly: send again any "labels recorded" notice that failed. Each batch
        is committed once its notice is delivered, since a post cannot be undone;
        a company with no order channel counts as cleared, and the run stops after
        a few failures in a row. ``now`` is unused (the hourly job passes it)."""
        batches = self.env["grove.label.batch"].sudo().search([("labels_notice_pending", "=", True)])
        in_test = bool(config["test_enable"])
        failures = 0
        for batch in batches:
            try:
                status = batch._post_labels_notice(len(batch.line_ids.filtered("tracking_number")))
                if status == "failed":
                    failures += 1
                else:
                    failures = 0
                    batch.write({"labels_notice_pending": False})
                    if not in_test:
                        self.env.cr.commit()
            except Exception:  # noqa: BLE001 — keep retrying the other batches
                _logger.exception("Labels-recorded retry failed for batch %s", batch.id)
                failures += 1
            if failures >= _NUDGE_MAX_FAILURES:
                _logger.error("Labels-recorded retries stopped after %s consecutive failures", failures)
                break

    def _grove_edit_card_status(self):
        """Ask the bridge to redraw this order's Discord card from its current
        state. Returns ``"ok"``, ``"gone"`` (Discord no longer has the message),
        ``"unmapped"`` (the company has no order channel) or ``"failed"`` (anything
        else, worth retrying). Never raises."""
        self.ensure_one()
        from ..controllers import main as controllers

        try:
            secret = os.environ.get("ORDER_ALERT_SECRET", "")
            base = os.environ.get("DISCORD_BRIDGE_BASE_URL", "").rstrip("/")
            if not secret or not base:
                _logger.warning("Card for %s not refreshed: the Discord bridge is not configured", self.name)
                return "failed"
            with self.env.cr.savepoint():
                card = controllers._order_card_payload(self.with_context(tz=controllers.ORDER_CARD_TZ))
            resp = requests.post(
                f"{base}/orders/alert/edit",
                json={**card, "messageId": self.grove_card_message_id},
                headers={"X-Order-Alert-Secret": secret},
                timeout=_CARD_EDIT_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 — a card edit is best-effort and is retried
            _logger.warning("Card refresh failed for %s", self.name, exc_info=True)
            return "failed"
        if resp.status_code == 200:
            return "ok"
        if resp.status_code == 410:
            return "gone"
        if resp.status_code == 422:
            return "unmapped"
        _logger.warning("Card refresh for %s returned %s", self.name, resp.status_code)
        return "failed"

    def _grove_refresh_cards(self):
        """Edit each flagged order's card and clear its flag. An order with no
        stored message id, a card Discord has lost, or a company with no channel
        cannot be helped by a retry, so those are logged and cleared too. Commits
        per order and stops after a few failures in a row, leaving the rest flagged
        for the hourly job. Must not run in a transaction that charges a card."""
        in_test = bool(config["test_enable"])
        failures = 0
        for order in self:
            try:
                if order.grove_card_message_id:
                    status = order._grove_edit_card_status()
                else:
                    status = "no_card"
                    _logger.warning("%s has no Discord card on record to refresh; a repost gives it one", order.name)
                if status == "failed":
                    failures += 1
                else:
                    failures = 0
                    if status == "gone":
                        _logger.warning("Discord no longer has the card for %s; stopped refreshing it", order.name)
                    elif status == "unmapped":
                        _logger.error("Card for %s not refreshed: its company has no order channel", order.name)
                    order.write({"grove_card_needs_refresh": False})
                    if not in_test:
                        self.env.cr.commit()
            except Exception:  # noqa: BLE001 — keep refreshing the other cards
                _logger.exception("Card refresh failed for order %s", order.id)
                failures += 1
            if failures >= _NUDGE_MAX_FAILURES:
                _logger.error("Card refresh stopped after %s consecutive failures; will retry next run", failures)
                break

    def _grove_refresh_cards_after_commit(self):
        """Edit these orders' cards once the current transaction has committed, on
        a cursor of their own, so the Discord calls sit outside whatever charged
        them. A failure leaves the flags for the hourly job."""
        order_ids = self.ids
        registry = self.env.registry

        def refresh():
            try:
                with registry.cursor() as cr:
                    api.Environment(cr, SUPERUSER_ID, {})["sale.order"].browse(order_ids)._grove_refresh_cards()
            except Exception:  # noqa: BLE001 — the import is committed; the hourly job retries
                _logger.exception("Card refresh after import failed")

        self.env.cr.postcommit.add(refresh)

    @api.model
    def _cron_grove_card_refresh(self, now=None):
        """Hourly: retry card edits that have not landed. ``now`` is unused (the
        hourly job passes it)."""
        companies = self.env["res.company"].sudo().search([])
        self.sudo().with_context(allowed_company_ids=companies.ids).search(
            [("grove_card_needs_refresh", "=", True)]
        )._grove_refresh_cards()

    @api.model
    def _grove_repost_open_order_cards(self):
        """Run once by hand from an Odoo shell after the lanes launch: repost the
        confirmed ship orders that are still open so they get the new buttons.

        Each order is stamped (``grove_card_reposted``) only after its post
        succeeded and committed, so a second run posts nothing and a partial
        failure can be resumed by running it again. One order's failure never
        stops the others. Consecutive failures are counted per company (each
        company has its own bridge channel); after three in a row a company's
        remaining orders are left for this run, unstamped, and the other companies
        carry on. A success resets only that company's count. Printify orders are
        left out (they have no buttons to show).
        Returns ``{"posted": n, "skipped": n, "failed": n}``; ``skipped`` counts
        open orders already reposted plus orders left unattempted when their
        company was stopped, so every open order is in exactly one count."""
        from ..controllers import main as controllers

        domain = [
            ("state", "in", ("sale", "done")),
            ("grove_fulfillment", "=", "ship"),
            ("grove_is_printify", "=", False),
            ("grove_fulfillment_stage", "not in", list(_HOLD_REFUSED_STAGES)),
        ]
        orders = self.sudo().search(domain)
        pending = orders.filtered(lambda o: not o.grove_card_reposted)
        result = {"posted": 0, "skipped": len(orders) - len(pending), "failed": 0}
        # Skip the per-order commit under the test runner so a TransactionCase
        # stays isolated (same as the crons).
        in_test = bool(config["test_enable"])
        consecutive = {}
        for order in pending:
            company_id = order.company_id.id
            if consecutive.get(company_id, 0) >= _NUDGE_MAX_FAILURES:
                result["skipped"] += 1
                continue
            try:
                scoped = order.with_context(tz=controllers.ORDER_CARD_TZ)
                context = controllers._order_alert_context(scoped)
                is_deposit = bool((scoped.grove_preorder_variant_ids or "").strip())
                posted = controllers._post_order_alert_card(scoped, context, is_deposit)
            except Exception:  # noqa: BLE001 — keep reposting the other orders
                _logger.exception("Card repost failed for order %s", order.id)
                posted = False
            if posted:
                consecutive[company_id] = 0
                order.write({"grove_card_reposted": True})
                # A Discord post cannot be undone, so keep its stamp even if a
                # later order fails.
                if not in_test:
                    self.env.cr.commit()
                result["posted"] += 1
                continue
            result["failed"] += 1
            consecutive[company_id] = consecutive.get(company_id, 0) + 1
            if consecutive[company_id] == _NUDGE_MAX_FAILURES:
                _logger.error(
                    "Card repost stopped for company %s after %s consecutive failures; run it again to resume",
                    company_id,
                    _NUDGE_MAX_FAILURES,
                )
        return result
