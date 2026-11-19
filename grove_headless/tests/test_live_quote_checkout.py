"""Odoo-runtime tests for the live-ZIP quote cache, alerts and checkout chain
(GOL-3927, spec §2 / §5). Run under Odoo's --test-enable runner.

Three layers:
  * the live-quote cache model (lookup / remember / 24 h TTL);
  * the quote-event model (rolling fallback-rate alert, QueryRetired disable);
  * the flag-gated checkout: flag OFF charges the zone table byte-for-byte; flag
    ON prices live (carrier + $2.50 once) and records the source; a forced live
    failure falls back to the zone table and records ``zone``.
"""

from datetime import timedelta
from unittest import mock

from odoo import fields
from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import live_quote, pirateship_rates
from odoo.addons.grove_headless.models.shipping_zones import box_rate, compute_order_shipping
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import TransactionCase, tagged

FLAG = "grove_headless.zip_live_quotes_enabled"


def _fake_quote(total=11.83, service="03"):
    opt = pirateship_rates.RateOption(
        service=service,
        carrier="UPS",
        service_title="UPS Ground",
        total=total,
        surcharges=(pirateship_rates.Surcharge("Delivery Area", 3.45),),
        transit_days=6,
    )
    return pirateship_rates.Quote(dest_zip="26651", box_id="p24x10x4", weight_lb=6.0, winner=opt, options=(opt,))


@tagged("grove_headless", "post_install", "-at_install")
class TestLiveQuoteCache(GroveTaxFixtureMixin, TransactionCase):
    def test_miss_then_remember_then_hit(self):
        Cache = self.env["grove.shipping.live.quote"]
        self.assertIsNone(Cache.lookup("26651", "p24x10x4", 6.0))
        Cache.remember("26651", "p24x10x4", 6.0, _fake_quote())
        hit = Cache.lookup("26651", "p24x10x4", 6.0)
        self.assertEqual(hit["price"], 11.83)
        self.assertEqual(hit["service"], "03")

    def test_weight_ceiled_into_key(self):
        Cache = self.env["grove.shipping.live.quote"]
        Cache.remember("26651", "p24x10x4", 5.1, _fake_quote())
        # 5.1 and 6.0 both ceil to 6 -> same cache row.
        self.assertIsNotNone(Cache.lookup("26651", "p24x10x4", 6.0))

    def test_expired_row_is_a_miss(self):
        Cache = self.env["grove.shipping.live.quote"]
        Cache.remember("26651", "p24x10x4", 6.0, _fake_quote())
        row = Cache.search([("dest_zip", "=", "26651")], limit=1)
        row.fetched_at = fields.Datetime.now() - timedelta(hours=25)
        self.assertIsNone(Cache.lookup("26651", "p24x10x4", 6.0))

    def test_remember_upserts(self):
        Cache = self.env["grove.shipping.live.quote"]
        Cache.remember("26651", "p24x10x4", 6.0, _fake_quote(total=11.83))
        Cache.remember("26651", "p24x10x4", 6.0, _fake_quote(total=12.99))
        rows = Cache.search([("dest_zip", "=", "26651"), ("box_id", "=", "p24x10x4"), ("weight_lb_ceil", "=", 6)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.total, 12.99)


@tagged("grove_headless", "post_install", "-at_install")
class TestQuoteEventAlerts(GroveTaxFixtureMixin, TransactionCase):
    def test_fallback_rate_alert_fires_over_threshold(self):
        Event = self.env["grove.shipping.quote.event"]
        for outcome in ("live", "live", "live", "fallback", "fallback"):  # 2/5 = 40% > 20%, >= 5
            Event.create({"outcome": outcome, "level": outcome, "box_id": "p24x10x4"})
        with mock.patch.object(live_quote, "_post_discord") as post:
            Event._maybe_alert_fallback_rate()
            self.assertTrue(post.called)

    def test_no_alert_under_minimum(self):
        Event = self.env["grove.shipping.quote.event"]
        for outcome in ("fallback", "fallback"):  # only 2 quotes < 5 minimum
            Event.create({"outcome": outcome, "level": "zone", "box_id": "p24x10x4"})
        with mock.patch.object(live_quote, "_post_discord") as post:
            Event._maybe_alert_fallback_rate()
            self.assertFalse(post.called)

    def test_query_retired_disables_flag(self):
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param(FLAG, "1")
        with mock.patch.object(live_quote, "_post_discord"):
            self.env["grove.shipping.quote.event"].alert_query_retired("test")
        self.assertEqual(icp.get_param(FLAG), "0")


@tagged("grove_headless", "post_install", "-at_install")
class TestLiveQuoteDiscordRouting(GroveTaxFixtureMixin, TransactionCase):
    """The live-quote alerts post to the shipping channel, through the same
    topic fallback chain the order pings use (GOL-4056)."""

    def _post_and_capture(self, env):
        with mock.patch.dict("os.environ", env, clear=True), mock.patch.object(
            live_quote.requests, "post"
        ) as post:
            live_quote._post_discord(self.env, "2/5 (40%) fell back in the last hour")
        return post

    def test_shipping_webhook_wins_when_set(self):
        post = self._post_and_capture(
            {
                "DISCORD_SHIPPING_WEBHOOK_URL": "https://d/shipping",
                "DISCORD_ORDERS_WEBHOOK_URL": "https://d/orders",
                "DISCORD_OPS_WEBHOOK_URL": "https://d/ops",
            }
        )
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://d/shipping")
        # mentions disarmed and the instance tag prefixed, like the order pings.
        self.assertEqual(kwargs["json"]["allowed_mentions"], {"parse": []})
        self.assertIn("fell back", kwargs["json"]["content"])

    def test_falls_back_to_ops_when_only_ops_set(self):
        post = self._post_and_capture({"DISCORD_OPS_WEBHOOK_URL": "https://d/ops"})
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], "https://d/ops")

    def test_no_post_when_no_webhook_set(self):
        post = self._post_and_capture({})
        post.assert_not_called()


class _BarerootOrder(GroveTaxFixtureMixin):
    """A one-template (bareroot) order in the main company, green-state ship-to."""

    def _build(self):
        self.company = self.env.ref("base.main_company")
        self.tmpl = self.env["product.template"].create(
            {
                "name": "Quote Serviceberry",
                "type": "consu",
                "is_storable": True,
                "sale_ok": True,
                "list_price": 12.0,
                "grove_shipping_tier": "bareroot",
                "company_id": self.company.id,
            }
        )
        self.variant = self.tmpl.product_variant_ids[:1]
        partner = self.env["res.partner"].create({"name": "Quote Buyer"})
        self.order = self.env["sale.order"].create(
            {
                "partner_id": partner.id,
                "company_id": self.company.id,
                "order_line": [(0, 0, {"product_id": self.variant.id, "product_uom_qty": 3.0})],
            }
        )

    def _ship_charge(self):
        return grove_main._apply_shipping_line(self.env, self.order, {"state": "WV", "zip": "26651"}, self.company)


@tagged("grove_headless", "post_install", "-at_install")
class TestCheckoutFlag(_BarerootOrder, TransactionCase):
    def setUp(self):
        super().setUp()
        self._build()
        self.icp = self.env["ir.config_parameter"].sudo()

    def test_flag_off_is_zone_table_byte_for_byte(self):
        self.icp.set_param(FLAG, "0")
        charge = self._ship_charge()
        mode = grove_main.packing_mode(grove_main._date.today(), grove_main.dormancy_window(self.env))
        items = [("bareroot", 20, 3.0)]
        self.assertEqual(charge, compute_order_shipping("WV", items, mode))
        # The live-source field is only written on the live path.
        self.assertFalse(self.order.grove_shipping_source)

    def test_flag_on_live_charges_carrier_plus_handling_once(self):
        self.icp.set_param(FLAG, "1")
        with mock.patch.object(pirateship_rates, "quote", return_value=_fake_quote(total=11.83)):
            charge = self._ship_charge()
        # One box: carrier 11.83 + $2.50 once = 14.33.
        self.assertEqual(charge, 14.33)
        self.assertEqual(self.order.grove_shipping_source, "live")
        self.assertIn('"source": "live"', self.order.grove_shipping_quote_detail)

    def test_flag_on_forced_fallback_records_zone(self):
        self.icp.set_param(FLAG, "1")
        with mock.patch.object(pirateship_rates, "quote", side_effect=pirateship_rates.Timeout("dead host")):
            charge = self._ship_charge()
        # Fell all the way back to the zone table -> its baked rate, no extra
        # handling, so it equals the flag-off number (never under-charges).
        self.assertEqual(charge, box_rate("WV", "p24x10x4"))
        self.assertEqual(self.order.grove_shipping_source, "zone")
        self.assertIn("Timeout", self.order.grove_shipping_fallback_reason)
        events = self.env["grove.shipping.quote.event"].search([("order_id", "=", self.order.id)])
        self.assertTrue(events)
        self.assertEqual(events[0].outcome, "fallback")
