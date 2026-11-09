"""Order lanes and holds on sale.order (Discord order operations).

A hold records "the customer asked us to wait" so the order is not forgotten.
It never ships, charges or emails anything by itself; every state change writes
chatter naming the actor.
"""

import logging
from datetime import date, timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .order_lanes import lane_for, suggest_hold_date
from .shipping_boxes import dormancy_window
from .shipping_calendar import merge_calendar_override, preorder_waves

_logger = logging.getLogger(__name__)

# Stages where a hold is meaningless: the order is already out the door or finished.
# (label_purchased is deliberately allowed: a bought label can still be held.)
_HOLD_REFUSED_STAGES = ("shipped", "delivered", "collected", "cancelled")


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
