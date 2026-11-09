"""Order lanes and holds on sale.order (Discord order operations).

A hold records "the customer asked us to wait" so the order is not forgotten.
It never ships, charges or emails anything by itself; every state change writes
chatter naming the actor.
"""

import logging
from datetime import date, timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .order_lanes import clean_tracking, lane_for, suggest_hold_date
from .shipping_boxes import dormancy_window
from .shipping_calendar import merge_calendar_override, preorder_waves

_logger = logging.getLogger(__name__)

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
        self.sudo()._grove_advance_state(
            target,
            source="label_refunded",
            operator=actor,
            note=f"{actor} marked the label refunded (was {old_text}). The order needs a label again.",
        )
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
        if self.state == "cancel" or stage in _HOLD_REFUSED_STAGES:
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
        return True

    def _grove_needs_wave_for_ship(self):
        return self.grove_fulfillment_stage == "deposit_paid" and self.grove_ship_wave == "fall"

    def _grove_stage_label(self):
        """The order's fulfilment stage in the words shown to staff."""
        stage = self.grove_fulfillment_stage
        return dict(self._fields["grove_fulfillment_state"].selection).get(stage, stage)

    def _grove_refuse_mark_shipped(self, tracking):
        """Raise ``UserError`` when this order cannot be marked shipped with this
        number. The message is shown to the operator as written. An order already
        out the door skips the hold and spring checks, so a retry stays a no-op."""
        stage = self.grove_fulfillment_stage
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
