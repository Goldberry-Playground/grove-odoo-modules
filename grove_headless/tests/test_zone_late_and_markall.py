"""Late spring orders, the labels-recorded post, and Mark all shipped.

DB-backed, so listed in tests/__init__.py AND conftest.py's collect_ignore_glob.
Zone 6 spring ships Apr 5 to Apr 15 (its batch opens Mar 31).
"""

import csv
import io
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.addons.grove_headless.controllers import main as grove_main
from odoo.addons.grove_headless.models import label_batch as label_batch_module
from odoo.addons.grove_headless.models import sale_order as sale_order_module
from odoo.addons.grove_headless.models.shipping_boxes import PackedBox
from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tests.common import HttpCase, get_db_name
from psycopg2 import errors

POST_STATUS = "odoo.addons.grove_headless.models.sale_order_ops.SaleOrder._grove_post_notice_status"
POST = "odoo.addons.grove_headless.models.sale_order_ops.SaleOrder._grove_post_notice"
ZONE_6_ZIP = "26651"
START = date(2027, 4, 5)
BEFORE_START = date(2027, 4, 4)
END = date(2027, 4, 15)
BUILD_DAY = date(2027, 4, 1)
_T = [f"1Z999AA1012345{n:04d}" for n in range(40)]


def _at(day, hour=13):
    """``hour`` UTC on ``day`` (13:00 UTC is 9am New York in April)."""
    return datetime(day.year, day.month, day.day, hour, 0)


class _Fixture(GroveTaxFixtureMixin):
    def _setup_fixture(self):
        self.company = self.env.ref("base.main_company")
        self.Batch = self.env["grove.label.batch"]
        self.Alert = self.env["grove.zone.alert"]
        self.product = self.env["product.product"].create(
            {
                "name": "Late Dogwood",
                "type": "consu",
                "list_price": 40.0,
                "grove_shipping_tier": "bareroot",
                "grove_tree_length": "20",
            }
        )

    def _order(self, zip_code=ZONE_6_ZIP, checkout="deposit_paid", wave="spring", **extra):
        partner = self.env["res.partner"].create(
            {
                "name": "Late Customer",
                "street": "1 Grove Way",
                "city": "Summersville",
                "state_id": self.env.ref("base.state_us_48").id,
                "zip": zip_code,
                "country_id": self.env.ref("base.us").id,
                "email": f"late-{self.env['res.partner'].search_count([])}@example.com",
            }
        )
        vals = {
            "partner_id": partner.id,
            "company_id": self.company.id,
            "grove_fulfillment": "ship",
            "grove_checkout_status": checkout,
            "order_line": [(0, 0, {"product_id": self.product.id, "product_uom_qty": 1.0, "price_unit": 40.0})],
        }
        if wave:
            vals["grove_ship_wave"] = wave
        order = self.env["sale.order"].with_company(self.company).create(vals)
        if checkout == "deposit_paid":
            order.grove_preorder_variant_ids = str(self.product.id)
        if extra:
            order.sudo().write(extra)
        return order

    @contextmanager
    def _on(self, day=BUILD_DAY):
        """Pin today and stub the rate table only."""
        with (
            patch.object(fields.Date, "context_today", return_value=day),
            patch.object(sale_order_module, "pack_for_state", return_value=[PackedBox(box_id="p24x10x4", count=1)]),
            patch.object(sale_order_module, "unshippable_reason", return_value=None),
            patch.object(label_batch_module, "box_rate", return_value=24.0),
            patch.object(label_batch_module, "box_service_title", return_value="UPS Ground"),
        ):
            yield

    def _build(self, zone="6"):
        with self._on():
            return self.Batch.build_open_batch(self.company, zone)

    def _csv(self, rows):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Grove Ref", "Tracking Number", "Carrier", "Cost"])
        writer.writerows(rows)
        return buf.getvalue().encode("utf-8")

    def _rows(self, orders, start=0):
        return [[f"{o.name}/1", _T[start + i], "UPS", "9.10"] for i, o in enumerate(orders)]

    def _import(self, orders, start=0):
        raw = self._csv(self._rows(orders, start))
        with patch.object(type(self.env["sale.order"]), "_grove_settle_at_ship", return_value="already_settled"):
            return self.Batch.import_pirateship_file(raw, filename="export.csv", company=self.company)

    def _purchased_batch(self, count=3):
        """A zone 6 batch whose ``count`` orders all have labels. Built on Apr 1."""
        orders = self.env["sale.order"]
        for _ in range(count):
            orders |= self._order()
        batch = self._build()["batch"]
        self._tracking_used = getattr(self, "_tracking_used", 0)
        self._import(orders, start=self._tracking_used)
        self._tracking_used += count
        self.assertEqual(batch.state, "purchased")
        self._built_on(batch, BUILD_DAY)
        return batch, orders

    def _built_on(self, batch, day):
        self.env.cr.execute("UPDATE grove_label_batch SET create_date = %s WHERE id = %s", (_at(day), batch.id))
        batch.invalidate_recordset(["create_date"])


@tagged("post_install", "-at_install")
class TestLateOrders(_Fixture, TransactionCase):
    def setUp(self):
        super().setUp()
        self._setup_fixture()

    def _run(self, day=START, ret="ok", hour=13):
        with patch(POST_STATUS, return_value=ret) as post:
            self.env["sale.order"]._cron_grove_late_orders(now=_at(day, hour))
        return post

    def _late(self, count=1, **extra):
        orders = self.env["sale.order"]
        for _ in range(count):
            orders |= self._order(state="sale", **extra)
        return orders

    def _alerts(self):
        return self.Alert.search([("company_id", "=", self.company.id)])

    def test_late_post_lists_unbatched_spring_orders(self):
        late = self._late(2)
        batch = self.Batch.create(
            {"name": "LB-LATE-1", "company_id": self.company.id, "zone": "6", "state": "exported"}
        )
        # None of these still needs a batch.
        self._late(1, grove_label_batch_id=batch.id)
        self._late(1, grove_hold_until=date(2027, 4, 20))
        self._late(1, grove_tracking_numbers=_T[0])
        self._late(1, grove_fulfillment_stage="shipped")
        self._order(state="draft")
        self._late(1, grove_ship_wave="fall")
        self._late(1, grove_usda_zone="7")  # another zone; zone 7 is not in its window on Apr 5

        post = self._run(START)

        zone_six = [c for c in post.call_args_list if c.args[2][0].startswith("Zone 6")]
        self.assertEqual(len(zone_six), 1)
        company, _title, lines, buttons = zone_six[0].args
        self.assertEqual(company, self.company)
        self.assertEqual(lines[0], "Zone 6: 2 spring orders still need a batch.")
        for order in late:
            self.assertIn(order.name, lines[1])
        self.assertEqual(buttons, [])
        alert = self._alerts().filtered(lambda a: a.zone == "6")
        self.assertEqual((alert.year, alert.kind), (2027, "late:2027-04-05"))

    def test_late_post_is_once_per_day_and_repeats_the_next_day(self):
        self._late(1)
        self.assertEqual(self._run(START).call_count, 1)
        self.assertEqual(self._run(START).call_count, 0)
        self.assertEqual(self._run(START + timedelta(days=1)).call_count, 1)
        self.assertEqual(
            sorted(self._alerts().mapped("kind")),
            ["late:2027-04-05", "late:2027-04-06"],
        )

    def test_one_late_order_is_singular_and_none_posts_nothing(self):
        self._run(START).assert_not_called()
        self.assertFalse(self._alerts())
        self._late(1)
        post = self._run(START)
        self.assertEqual(post.call_args.args[2][0], "Zone 6: 1 spring order still needs a batch.")

    def test_late_post_stops_after_ship_end(self):
        self._late(1)
        self._run(BEFORE_START).assert_not_called()
        self.assertEqual(self._run(END).call_count, 1)
        self._run(END + timedelta(days=1)).assert_not_called()
        self.assertEqual(len(self._alerts()), 1)

    def test_nothing_before_8am_new_york(self):
        self._late(1)
        self._run(START, hour=11).assert_not_called()

    def test_failed_post_records_nothing_and_retries(self):
        self._late(1)
        self._run(START, ret="failed")
        self.assertFalse(self._alerts())
        self.assertEqual(self._run(START).call_count, 1)

    def test_order_back_from_a_purchased_batch_with_no_label_counts_as_late(self):
        batch = self.Batch.create(
            {"name": "LB-LATE-2", "company_id": self.company.id, "zone": "6", "state": "purchased"}
        )
        self._late(1, grove_label_batch_id=batch.id)
        self.assertEqual(self._run(START).call_args.args[2][0], "Zone 6: 1 spring order still needs a batch.")

    def test_late_post_names_spring_orders_with_no_zone(self):
        no_zone = self._late(2, grove_usda_zone=False)
        post = self._run(START)
        self.assertEqual(post.call_count, 1)
        company, _title, lines, buttons = post.call_args.args
        self.assertEqual(company, self.company)
        self.assertIn("2 spring orders have no zone", lines[0])
        self.assertIn("zone is unknown and needs fixing on the order", lines[0])
        for order in no_zone:
            self.assertIn(order.name, lines[1])
        self.assertEqual(buttons, [])
        alert = self._alerts()
        self.assertEqual((alert.zone, alert.kind), ("none", "late:2027-04-05"))
        # Once per New York day.
        self.assertEqual(self._run(START).call_count, 0)
        self.assertEqual(self._run(START + timedelta(days=1)).call_count, 1)

    def test_no_zone_post_waits_for_a_spring_window_to_open(self):
        self._late(1, grove_usda_zone=False)
        self._run(date(2027, 11, 9)).assert_not_called()
        self.assertFalse(self._alerts())


@tagged("post_install", "-at_install")
class TestLabelsRecordedPost(_Fixture, TransactionCase):
    def setUp(self):
        super().setUp()
        self._setup_fixture()

    def test_import_posts_labels_recorded_with_markall(self):
        orders = self.env["sale.order"]
        for _ in range(14):
            orders |= self._order()
        batch = self._build()["batch"]

        with patch(POST, return_value=True) as post:
            self._import(orders)

        self.assertEqual(batch.state, "purchased")
        self.assertEqual(post.call_count, 1)
        company, _title, lines, buttons = post.call_args.args
        self.assertEqual(company, self.company)
        self.assertEqual(lines, ["Zone 6: 14 labels recorded"])
        self.assertEqual(buttons, [{"label": "Mark all shipped", "style": 3, "customId": f"ord:markall:{batch.id}"}])

    def test_strict_import_posts_too(self):
        order = self._order()
        batch = self._build()["batch"]
        with (
            patch.object(type(self.env["sale.order"]), "_grove_settle_at_ship", return_value="already_settled"),
            patch(POST, return_value=True) as post,
        ):
            batch.import_tracking(self._csv(self._rows(order)), filename="track.csv")
        self.assertEqual(post.call_args.args[2], ["Zone 6: 1 label recorded"])

    def test_a_later_import_into_a_purchased_batch_does_not_post_again(self):
        first, second = self._order(), self._order()
        self._build()
        with patch(POST, return_value=True) as post:
            self._import(first)
            self._import(second, start=1)
        self.assertEqual(post.call_count, 1)

    def test_import_with_no_recorded_label_posts_nothing(self):
        order = self._order()
        batch = self._build()["batch"]
        order.sudo().write({"grove_hold_until": date(2027, 5, 1)})
        with patch(POST, return_value=True) as post:
            self._import(order)
        self.assertEqual(batch.state, "exported")
        post.assert_not_called()

    def test_rejected_strict_import_announces_nothing(self):
        order = self._order()
        batch = self._build()["batch"]
        raw = self._csv([[f"{order.name}/1", "bad", "UPS", "9.10"]])
        with patch(POST, return_value=True) as post, self.assertRaises(label_batch_module.LabelBatchError):
            batch.import_tracking(raw, filename="track.csv")
        post.assert_not_called()

    def test_failed_post_does_not_affect_the_import(self):
        order = self._order()
        batch = self._build()["batch"]
        with patch(POST, side_effect=RuntimeError("bridge down")):
            result = self._import(order)
        self.assertEqual(result["labelled"], [order.name])
        self.assertEqual(batch.state, "purchased")
        self.assertTrue(order.grove_tracking_numbers)

    def test_batch_with_no_zone_posts_nothing(self):
        legacy = self.Batch.create({"name": "LB-LEGACY", "company_id": self.company.id})
        with patch(POST, return_value=True) as post:
            legacy._announce_labels_recorded(5)
        post.assert_not_called()


@tagged("post_install", "-at_install")
class TestMarkAllShipped(_Fixture, TransactionCase):
    def setUp(self):
        super().setUp()
        self._setup_fixture()

    @contextmanager
    def _ship_patches(self):
        """The shipment email is the only thing stubbed (it would send mail)."""
        with patch.object(grove_main, "_apply_delivery_status") as email:
            yield email

    def _markall(self, batch, day=START, actor="Wes"):
        return batch._grove_mark_batch_shipped(actor, now=_at(day))

    def test_markall_refused_before_ship_start(self):
        batch, orders = self._purchased_batch(2)
        with self._ship_patches() as email, self.assertRaises(UserError) as caught:
            self._markall(batch, BEFORE_START)
        self.assertIn("April 5, 2027", str(caught.exception))
        self.assertIn("April 4, 2027", str(caught.exception))
        email.assert_not_called()
        self.assertEqual(set(orders.mapped("grove_fulfillment_stage")), {"label_purchased"})

    def test_markall_allowed_on_ship_start_and_after_ship_end(self):
        batch, orders = self._purchased_batch(2)
        with self._ship_patches():
            self.assertEqual(self._markall(batch, START)["shipped"], 2)
        batch2, _orders2 = self._purchased_batch(1)
        with self._ship_patches():
            # A batch built inside the window (Apr 1), marked after the window closed.
            self.assertEqual(self._markall(batch2, END + timedelta(days=9))["shipped"], 1)

    def test_markall_refuses_a_batch_with_no_zone_or_no_labels(self):
        legacy = self.Batch.create({"name": "LB-LEGACY", "company_id": self.company.id, "state": "purchased"})
        with self.assertRaisesRegex(UserError, "no zone"):
            self._markall(legacy)
        self._order()
        open_batch = self._build()["batch"]
        with self.assertRaisesRegex(UserError, "no recorded labels"):
            self._markall(open_batch)

    def test_markall_ships_only_unmarked_orders_and_skips_held(self):
        batch, orders = self._purchased_batch(4)
        fresh, already, held, unlabelled = orders
        with self._ship_patches():
            already.action_grove_mark_shipped(operator="Josh")
        held.sudo().write({"grove_hold_until": date(2027, 4, 20), "grove_hold_reason": "Away"})
        unlabelled.sudo().write({"grove_tracking_numbers": False})

        with self._ship_patches() as email:
            result = self._markall(batch)

        self.assertEqual(result["shipped"], 1)
        self.assertEqual(result["already"], 1)
        self.assertEqual(
            {s["order"]: s["reason"] for s in result["skipped"]},
            {held.name: "on hold until Apr 20", unlabelled.name: "no label recorded"},
        )
        self.assertEqual(fresh.grove_fulfillment_stage, "shipped")
        self.assertEqual(email.call_count, 1)
        self.assertEqual(held.grove_fulfillment_stage, "label_purchased")
        self.assertTrue(any("Wes" in (m.body or "") for m in fresh.message_ids))

    def test_markall_does_not_charge_again(self):
        batch, orders = self._purchased_batch(2)
        orders.sudo().write({"grove_checkout_status": "settled"})
        outcomes = []
        real = grove_main.settle_order_at_ship

        def spy(env, order):
            outcomes.append(real(env, order))
            return outcomes[-1]

        with self._ship_patches(), patch.object(grove_main, "settle_order_at_ship", side_effect=spy):
            result = self._markall(batch)

        self.assertEqual(result["shipped"], 2)
        self.assertEqual(outcomes, ["already_settled", "already_settled"])
        self.assertEqual(set(orders.mapped("grove_checkout_status")), {"settled"})

    def test_markall_skips_an_order_that_moved_to_a_newer_batch(self):
        x, y = self._order(), self._order()
        batch_a = self._build()["batch"]
        self._import(x)  # y is left without a label
        self.assertEqual(batch_a.state, "purchased")
        self._built_on(batch_a, BUILD_DAY)
        batch_b = self._build()["batch"]
        self.assertNotEqual(batch_b, batch_a)
        self.assertEqual(y.grove_label_batch_id, batch_b)
        self._import(y, start=1)  # B is imported: y now has B's label
        self.assertEqual(y.grove_fulfillment_stage, "label_purchased")

        with self._ship_patches() as email:
            result = self._markall(batch_a)

        self.assertEqual(result["shipped"], 1)
        self.assertEqual(result["skipped"], [{"order": y.name, "reason": f"now in {batch_b.name}"}])
        self.assertEqual(email.call_count, 1)
        self.assertEqual(y.grove_fulfillment_stage, "label_purchased")

    def test_markall_skips_an_order_no_longer_linked_to_any_batch(self):
        batch, orders = self._purchased_batch(2)
        first, second = orders
        second.sudo().write({"grove_label_batch_id": False})
        with self._ship_patches():
            result = self._markall(batch)
        self.assertEqual(result["shipped"], 1)
        self.assertEqual(result["skipped"], [{"order": second.name, "reason": "no longer in this batch"}])
        self.assertEqual(second.grove_fulfillment_stage, "label_purchased")

    def test_a_lock_failure_gets_its_own_reason(self):
        batch, orders = self._purchased_batch(2)
        first, second = sorted(orders, key=lambda o: o.name)
        real = grove_main._operator_mark_shipped

        def locked(env, order, **kwargs):
            if order == first:
                raise errors.LockNotAvailable("could not obtain lock")
            return real(env, order, **kwargs)

        with self._ship_patches(), patch.object(grove_main, "_operator_mark_shipped", side_effect=locked):
            result = self._markall(batch)
        self.assertEqual(result["shipped"], 1)
        self.assertEqual(
            result["skipped"],
            [{"order": first.name, "reason": "was being changed by another run; check it before retrying"}],
        )

    def test_failed_labels_post_is_logged_as_an_error_naming_the_batch(self):
        order = self._order()
        batch = self._build()["batch"]
        with patch(POST, return_value=False), self.assertLogs(label_batch_module._logger, "ERROR") as logs:
            self._import(order)
        self.assertTrue(any(batch.name in line for line in logs.output), logs.output)

    def test_markall_twice_is_safe(self):
        batch, orders = self._purchased_batch(3)
        with self._ship_patches() as email:
            first = self._markall(batch)
            second = self._markall(batch)
        self.assertEqual(first, {"shipped": 3, "already": 0, "skipped": []})
        self.assertEqual(second, {"shipped": 0, "already": 3, "skipped": []})
        self.assertEqual(email.call_count, 3)

    def test_one_order_failing_does_not_stop_or_undo_the_others(self):
        batch, orders = self._purchased_batch(3)
        first, bad, last = sorted(orders, key=lambda o: o.name)
        real = grove_main._operator_mark_shipped

        def flaky(env, order, **kwargs):
            if order == bad:
                raise RuntimeError("secret internal detail")
            return real(env, order, **kwargs)

        with self._ship_patches(), patch.object(grove_main, "_operator_mark_shipped", side_effect=flaky):
            result = self._markall(batch)

        self.assertEqual(result["shipped"], 2)
        self.assertEqual([s["order"] for s in result["skipped"]], [bad.name])
        self.assertNotIn("secret", result["skipped"][0]["reason"])
        self.assertEqual((first | last).mapped("grove_fulfillment_stage"), ["shipped", "shipped"])
        self.assertEqual(bad.grove_fulfillment_stage, "label_purchased")

    def test_each_shipped_order_is_committed_outside_tests(self):
        batch, _orders = self._purchased_batch(2)
        with (
            self._ship_patches(),
            patch.object(label_batch_module, "config", {"test_enable": False}),
            patch.object(self.env.cr, "commit") as commit,
        ):
            self._markall(batch)
        self.assertEqual(commit.call_count, 2)


@tagged("post_install", "-at_install")
@tagged("post_install", "-at_install")
class TestCancelledAndMovedOrders(_Fixture, TransactionCase):
    """A cancelled or moved order must never be charged or emailed from a batch."""

    def setUp(self):
        super().setUp()
        self._setup_fixture()

    @contextmanager
    def _money_spies(self):
        """Yields (settle, email), the two things that cannot be undone."""
        with (
            patch.object(
                type(self.env["sale.order"]), "_grove_settle_at_ship", return_value="already_settled"
            ) as settle,
            patch.object(grove_main, "_apply_delivery_status") as email,
        ):
            yield settle, email

    def _import_rows(self, rows):
        return self.Batch.import_pirateship_file(self._csv(rows), filename="export.csv", company=self.company)

    def test_markall_skips_a_cancelled_order(self):
        batch, orders = self._purchased_batch(2)
        fresh, cancelled = orders
        cancelled.sudo().write({"state": "cancel"})
        with self._money_spies() as (settle, email):
            result = batch._grove_mark_batch_shipped("Wes", now=_at(START))
        self.assertEqual(result["shipped"], 1)
        self.assertEqual(result["skipped"], [{"order": cancelled.name, "reason": "cancelled"}])
        self.assertEqual(settle.call_count, 1)
        self.assertEqual(email.call_count, 1)
        self.assertEqual(fresh.grove_fulfillment_stage, "shipped")
        self.assertEqual(cancelled.grove_fulfillment_stage, "label_purchased")

    def test_tolerant_import_refuses_a_cancelled_row(self):
        kept, cancelled = self._order(), self._order()
        self._build()
        cancelled.sudo().write({"state": "cancel"})
        with self._money_spies() as (settle, email):
            summary = self._import_rows(self._rows(kept | cancelled))
        self.assertEqual(summary["labelled"], [kept.name])
        self.assertTrue(any(cancelled.name in r and "cancelled" in r for r in summary["review"]), summary["review"])
        self.assertFalse(cancelled.grove_tracking_numbers)
        self.assertEqual(settle.call_count, 1, "only the live order settles")
        email.assert_not_called()

    def test_strict_import_rejects_a_file_with_a_cancelled_row(self):
        kept, cancelled = self._order(), self._order()
        batch = self._build()["batch"]
        cancelled.sudo().write({"state": "cancel"})
        with self._money_spies() as (settle, email), self.assertRaises(label_batch_module.LabelBatchError) as caught:
            batch.import_tracking(self._csv(self._rows(kept | cancelled)), filename="export.csv")
        self.assertIn(cancelled.name, str(caught.exception))
        settle.assert_not_called()
        email.assert_not_called()

    def test_loose_row_for_a_cancelled_order_goes_to_review(self):
        order = self._order(checkout="paid", wave=False)
        order.sudo().write({"state": "cancel"})
        summary = self._import_rows([[f"{order.name}/1", _T[0], "UPS", "9.10"]])
        self.assertEqual(summary["labelled"], [])
        self.assertTrue(any(order.name in r and "cancelled" in r for r in summary["review"]), summary["review"])
        self.assertFalse(order.grove_tracking_numbers)

    def _batched(self):
        moved, stays = self._order(), self._order()
        batch = self._build()["batch"]
        self.assertEqual(batch.state, "exported")
        return batch, moved, stays

    def test_move_to_fall_releases_the_order_from_an_exported_batch(self):
        batch, moved, stays = self._batched()
        self.assertTrue(moved.action_grove_move_wave("fall", "Wes"))
        self.assertFalse(moved.grove_label_batch_id)
        self.assertNotIn(moved, batch.order_ids)
        self.assertEqual(stays.grove_label_batch_id, batch)
        self.assertIn(batch.name, " ".join(moved.message_ids.mapped("body")))

    def test_move_to_fall_leaves_a_purchased_batch_alone(self):
        batch, orders = self._purchased_batch(1)
        orders.sudo().write({"grove_tracking_numbers": False})
        orders.action_grove_move_wave("fall", "Wes")
        self.assertEqual(orders.grove_label_batch_id, batch)

    def test_moved_order_drops_out_of_a_fresh_csv(self):
        batch, moved, stays = self._batched()
        moved.action_grove_move_wave("fall", "Wes")
        csv_text = batch._render_csv().decode()
        self.assertNotIn(moved.name, csv_text)
        self.assertIn(stays.name, csv_text)

    def test_tolerant_import_refuses_a_row_for_an_order_moved_out_of_spring(self):
        _batch, moved, stays = self._batched()
        moved.action_grove_move_wave("fall", "Wes")
        with self._money_spies() as (settle, email):
            summary = self._import_rows(self._rows(moved | stays))
        self.assertEqual(summary["labelled"], [stays.name])
        self.assertTrue(any(moved.name in r and "spring" in r for r in summary["review"]), summary["review"])
        self.assertFalse(moved.grove_tracking_numbers)
        self.assertEqual(settle.call_count, 1, "only the order still in spring settles")
        email.assert_not_called()

    def test_strict_import_rejects_a_row_for_an_order_moved_out_of_spring(self):
        batch, moved, stays = self._batched()
        moved.action_grove_move_wave("fall", "Wes")
        with self._money_spies() as (settle, _email), self.assertRaises(label_batch_module.LabelBatchError) as caught:
            batch.import_tracking(self._csv(self._rows(moved | stays)), filename="export.csv")
        self.assertIn(moved.name, str(caught.exception))
        settle.assert_not_called()

    def test_old_csv_row_after_a_rebuild_goes_to_review(self):
        """The rebuild drops the row, so it reaches the no-batch path, which must
        refuse it the same way."""
        _batch, moved, stays = self._batched()
        moved.action_grove_move_wave("fall", "Wes")
        self._build()
        self.assertEqual(self.Batch._open_batch(self.company, "6").line_ids.order_id, stays)
        with self._money_spies() as (settle, _email):
            summary = self._import_rows(self._rows(moved))
        self.assertEqual(summary["labelled"], [])
        self.assertTrue(any(moved.name in r and "spring" in r for r in summary["review"]), summary["review"])
        self.assertFalse(moved.grove_tracking_numbers)
        settle.assert_not_called()

    def test_zone_less_batch_still_reconciles_an_individual_order(self):
        """Older open batches have no zone and hold individual orders."""
        order = self._order(checkout="paid", wave=False)
        self.assertEqual(order.grove_lane, "individual")
        batch = self.Batch.create({"name": "LB-OLD", "company_id": self.company.id, "state": "exported"})
        self.env["grove.label.batch.line"].create(
            {
                "batch_id": batch.id,
                "grove_ref": f"{order.name}/1",
                "order_id": order.id,
                "box_index": 1,
                "box_id": "p24x10x4",
            }
        )
        with self._money_spies():
            result = batch.import_tracking(self._csv(self._rows(order)), filename="export.csv")
        self.assertEqual(result["orders_advanced"], 1)


class TestMarkAllRoute(_Fixture, HttpCase):
    def setUp(self):
        super().setUp()
        admin = self.env.ref("base.user_admin")
        try:
            self.api_key = (
                self.env["res.users.apikeys"]
                .with_user(admin)
                ._generate("rpc", "grove-markall-test", datetime.now() + timedelta(days=1))
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"could not mint an API key for bearer auth: {exc}")
        websites = self.env["website"]
        website = websites._resolve_tenant_slug("goldberry") or websites.search([], limit=1)
        self._setup_fixture()
        self.company = website.company_id

    def _post(self, batch_id, body):
        self.env.flush_all()
        response = self.url_open(
            f"/grove/api/v1/label-batches/{batch_id}/mark-shipped",
            data=json.dumps(body).encode(),
            headers={
                "X-Odoo-Database": get_db_name(),
                "X-Grove-Tenant": "goldberry",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            timeout=60,
        )
        self.env.invalidate_all()
        return response

    def _batch(self, **vals):
        return self.Batch.create(
            {"name": "LB-ROUTE", "company_id": self.company.id, "zone": "6", "state": "purchased", **vals}
        )

    def test_route_refuses_before_ship_start_with_409(self):
        batch = self._batch()
        with patch.object(type(batch), "_zone_ship_start", return_value=date(2099, 4, 5)):
            response = self._post(batch.id, {"actor": "wes"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("April 5, 2099", response.json()["error"])

    def test_route_needs_actor_and_the_right_company(self):
        batch = self._batch()
        self.assertEqual(self._post(batch.id, {}).status_code, 422)
        self.assertEqual(self._post(batch.id, {"actor": "  "}).status_code, 422)
        other = self.env["res.company"].create({"name": "Other Co"})
        foreign = self._batch(name="LB-OTHER", company_id=other.id)
        self.assertEqual(self._post(foreign.id, {"actor": "wes"}).status_code, 404)
        self.assertEqual(self._post(0, {"actor": "wes"}).status_code, 404)

    def test_route_returns_counts_and_skipped(self):
        batch = self._batch()
        with (
            patch.object(
                type(batch),
                "_grove_mark_batch_shipped",
                return_value={"shipped": 1, "already": 2, "skipped": [{"order": "S1", "reason": "x"}]},
            ) as run,
        ):
            response = self._post(batch.id, {"actor": " wes "})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"shipped": 1, "already": 2, "skipped": [{"order": "S1", "reason": "x"}]})
        run.assert_called_once_with("wes")

    def test_route_unexpected_failure_is_500_with_a_check_first_message(self):
        batch = self._batch()
        with patch.object(type(batch), "_grove_mark_batch_shipped", side_effect=RuntimeError("boom")):
            response = self._post(batch.id, {"actor": "wes"})
        self.assertEqual(response.status_code, 500)
        self.assertIn("check the orders before retrying", response.json()["error"])
        self.assertNotIn("boom", response.text)
