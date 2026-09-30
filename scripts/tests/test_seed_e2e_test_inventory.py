"""Pure tests for the QA E2E test-inventory seed (no Odoo, no network).

Covers the two invariants that are silent when broken:

  * the **fixture table** — which fixtures are qualifying plants, which are
    deliberately NOT, and how deep each is stocked (GOL-2463/GOL-2464); and
  * the **volume-tier probe** — that a ``qualifyingUnits: 0`` answer FAILS the
    run instead of printing a warning nobody reads.

A mis-set ``categ_xmlid`` or a shallow ``qty`` on the plants fixture produces a
green seed and a red e2e suite hours later, for a reason that looks nothing like
the cause, so both are asserted here rather than discovered on QA.
"""

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

_PATH = os.path.join(os.path.dirname(__file__), "..", "seed_e2e_test_inventory.py")
_spec = importlib.util.spec_from_file_location("seed_e2e_test_inventory", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _fixture(key):
    return next(f for f in mod.FIXTURES if f["key"] == key)


class TestFixtureTable(unittest.TestCase):
    def test_three_fixtures_with_unique_keys_and_skus(self):
        keys = [f["key"] for f in mod.FIXTURES]
        skus = [f["sku"] for f in mod.FIXTURES]
        self.assertEqual(sorted(keys), ["bareroot", "plants", "potted"])
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual(len(set(skus)), len(skus))

    def test_only_the_plants_fixture_is_categorised(self):
        # The whole point of GOL-2463: 797 (bareroot) and the potted fixture must
        # stay OUT of Plants so every deterministic gate cart keeps its exact
        # total. Exactly one fixture is a qualifying plant.
        categorised = [f["key"] for f in mod.FIXTURES if f.get("categ_xmlid")]
        self.assertEqual(categorised, ["plants"])

    def test_plants_fixture_sits_under_the_plants_root(self):
        # is_qualifying_plant() walks parent_path from grove_headless.categ_plants,
        # so the xmlid must be that root or a descendant of it. categ_trees is.
        self.assertEqual(_fixture("plants")["categ_xmlid"], "grove_headless.categ_trees")

    def test_plants_fixture_is_gate_exempt_so_it_can_publish(self):
        # Under Plants the listing-content gate (GOL-2382) blocks publish until
        # the full botanical field set is present; gate_exempt is the honest
        # escape for a synthetic fixture, and is_qualifying_plant() ignores it.
        self.assertTrue(_fixture("plants").get("gate_exempt"))
        for key in ("potted", "bareroot"):
            self.assertFalse(_fixture(key).get("gate_exempt"), f"{key} is not gated; must not be exempt")

    def test_plants_fixture_is_stocked_deeper_than_the_gate_fixtures(self):
        # A tier run buys 5-10 units and CONFIRMS them, so on-hand only ratchets
        # down between seeds. At E2E_QTY (50) the fixture drains in ~5-10 runs and
        # the drained cart reads as a deposit cart -> false red (GOL-2463).
        plants_qty = _fixture("plants")["qty"]
        self.assertGreaterEqual(plants_qty, 10 * mod.E2E_TIERS_QTY)
        self.assertGreater(plants_qty, mod.E2E_QTY)

    def test_gate_fixtures_carry_no_explicit_qty_and_fall_back_to_e2e_qty(self):
        for key in ("potted", "bareroot"):
            self.assertNotIn("qty", _fixture(key))

    def test_every_fixture_name_keeps_the_e2e_prefix(self):
        # grove-sites E2E_FIXTURE_NAME_RE (/^AAA QA E2E /) excludes exactly this
        # prefix from the catalog set; these fixtures carry no photo, so any other
        # name reds qa-photos.spec.ts on a "Photo coming soon" placeholder.
        for f in mod.FIXTURES:
            self.assertTrue(f["name"].startswith("AAA QA E2E "), f["name"])

    def test_plants_fixture_never_sorts_ahead_of_the_gate_fixtures(self):
        # /shop is name-asc and grove-sites findProductByCta (apps/nursery/e2e/
        # helpers.ts) returns the FIRST product whose CTA matches. If the tier
        # fixture sorted first, every bare findProductByCta("Add to Cart") spec
        # would start buying a discountable plant and its asserted totals would
        # quietly shift by 10-20%.
        names = sorted(f["name"] for f in mod.FIXTURES)
        self.assertNotEqual(names[0], _fixture("plants")["name"])

    def test_plants_fixture_name_does_not_match_the_ship_specs_name_filter(self):
        # The @stripe ship specs narrow with nameMatch=/bareroot/i to skip the
        # pickup-only potted fixture. The tier fixture is ALSO a shippable
        # bareroot line, so if its NAME said "bareroot" those specs could select
        # it instead of 797 and inherit a volume discount on a deterministic cart.
        import re

        self.assertFalse(re.search(r"bareroot", _fixture("plants")["name"], re.I))
        self.assertTrue(re.search(r"bareroot", _fixture("bareroot")["name"], re.I))

    def test_plants_fixture_is_shippable_with_a_tree_length(self):
        # A potted line 400s on a ship submit, and a bareroot line with no
        # grove_tree_length can't be box-sized, so shipping comes back $0.
        plants = _fixture("plants")
        self.assertEqual(plants["shipping_tier"], "bareroot")
        self.assertTrue(plants["tree_length"])


class TestVolumeTierProbe(unittest.TestCase):
    def test_probe_posts_the_cart_shape_the_bff_expects(self):
        captured = {}

        class _Resp:
            def read(self):
                return b'{"qualifyingUnits": 6, "tiers": []}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = req.data
            captured["ctype"] = req.get_header("Content-type")
            return _Resp()

        with mock.patch.object(mod._ureq, "urlopen", _urlopen):
            body = mod.probe_volume_tiers("https://qa.example/api/cart/tiers", 205, 797, 6)

        self.assertEqual(body["qualifyingUnits"], 6)
        self.assertEqual(captured["url"], "https://qa.example/api/cart/tiers")
        self.assertEqual(captured["ctype"], "application/json")
        self.assertEqual(
            mod._json.loads(captured["body"]),
            {"items": [{"variantId": 797, "templateId": 205, "quantity": 6}]},
        )

    def test_zero_qualifying_units_fails_the_run(self):
        # THE regression this file exists for: a non-qualifying fixture answers 0
        # for any quantity. That must abort, not warn — otherwise the tier specs
        # assert against a silently tier-free cart and "pass" for the wrong reason.
        with mock.patch.object(mod, "probe_volume_tiers", return_value={"qualifyingUnits": 0, "tiers": []}):
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    mod.verify_plant_tiers(205, 797)

    def test_missing_qualifying_units_key_fails_the_run(self):
        with mock.patch.object(mod, "probe_volume_tiers", return_value={"tiers": []}):
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    mod.verify_plant_tiers(205, 797)

    def test_a_transport_error_fails_the_run_instead_of_propagating(self):
        with mock.patch.object(mod, "probe_volume_tiers", side_effect=OSError("connection refused")):
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    mod.verify_plant_tiers(205, 797)

    def test_non_zero_qualifying_units_passes(self):
        with mock.patch.object(
            mod, "probe_volume_tiers", return_value={"qualifyingUnits": 6, "tiers": [{"minQty": 5}]}
        ):
            with redirect_stdout(io.StringIO()) as out:
                mod.verify_plant_tiers(205, 797)
        self.assertIn("qualifyingUnits=6", out.getvalue())


class TestQaGuard(unittest.TestCase):
    def test_prod_host_is_refused_for_a_live_run(self):
        # This script publishes buyable "AAA ..." fixtures that sort FIRST in /shop.
        with (
            mock.patch.object(mod, "DRY_RUN", False),
            mock.patch.object(mod, "FORCE_NOT_QA", False),
            mock.patch.object(mod, "ODOO_URL", "https://odoo.gatheringatthegrove.com"),
            mock.patch.object(mod, "ODOO_DB", "Goldberry"),
        ):
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    mod.guard_environment()

    def test_known_qa_target_is_allowed_for_a_live_run(self):
        with (
            mock.patch.object(mod, "DRY_RUN", False),
            mock.patch.object(mod, "FORCE_NOT_QA", False),
            mock.patch.object(mod, "ODOO_URL", "https://odoo.qa.gatheringatthegrove.com"),
            mock.patch.object(mod, "ODOO_DB", "odoo"),
        ):
            with redirect_stdout(io.StringIO()):
                mod.guard_environment()  # must not raise


if __name__ == "__main__":
    unittest.main()
