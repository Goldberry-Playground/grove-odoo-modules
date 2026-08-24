"""Pure tests for the preorder-deposit email copy (GOL-1666).

``preorder_email`` is stdlib-only (its only dependency is
``stripe_gateway.PREORDER_DEPOSIT``), so it loads by file path like the other
pure tests. We register the modules under their real dotted names so the
module's ``from .stripe_gateway import PREORDER_DEPOSIT`` resolves against the
``grove_headless.models`` stub package the root conftest installs.
"""

import importlib.util
import os
import sys
import unittest

_MODELS = os.path.join(os.path.dirname(__file__), "..", "models")


def _load(modname):
    spec = importlib.util.spec_from_file_location(
        f"grove_headless.models.{modname}", os.path.join(_MODELS, f"{modname}.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


sg = _load("stripe_gateway")
pe = _load("preorder_email")


class DepositLabelTests(unittest.TestCase):
    def test_whole_dollar_has_no_trailing_zeros(self):
        self.assertEqual(pe.deposit_amount_label(10.0), "$10")
        self.assertEqual(pe.deposit_amount_label(10), "$10")

    def test_fractional_keeps_two_places(self):
        self.assertEqual(pe.deposit_amount_label(10.5), "$10.50")

    def test_default_tracks_gateway_source_of_truth(self):
        self.assertEqual(pe.deposit_amount_label(), pe.deposit_amount_label(sg.PREORDER_DEPOSIT))


class ConfirmationLineTests(unittest.TestCase):
    def test_ratified_voice_with_season(self):
        # Exact ratified line (GOL-1189 / GOL-1302).
        self.assertEqual(
            pe.confirmation_deposit_line("spring"),
            "$10 deposit per tree today, balance when your tree ships this spring.",
        )
        self.assertIn("this fall", pe.confirmation_deposit_line("fall"))

    def test_unknown_season_drops_the_season_word(self):
        line = pe.confirmation_deposit_line(None)
        self.assertEqual(line, "$10 deposit per tree today, balance when your tree ships.")
        self.assertNotIn("this None", line)

    def test_uses_flat_deposit_not_a_percentage(self):
        line = pe.confirmation_deposit_line("spring")
        self.assertIn("$10", line)
        self.assertNotIn("%", line)

    def test_no_em_dashes_anywhere(self):
        for season in ("spring", "fall", None):
            self.assertNotIn("—", pe.confirmation_deposit_line(season))
            self.assertNotIn("—", pe.preship_balance_line(season))


class PreshipLineTests(unittest.TestCase):
    def test_states_deposit_and_balance_arrangement(self):
        line = pe.preship_balance_line("spring")
        self.assertIn("$10 deposit per tree", line)
        self.assertIn("balance", line)
        self.assertIn("this spring", line)

    def test_unknown_season_uses_generic_ship_phrase(self):
        line = pe.preship_balance_line(None)
        self.assertIn("as your trees ship", line)
        self.assertNotIn("None", line)


if __name__ == "__main__":
    unittest.main()
