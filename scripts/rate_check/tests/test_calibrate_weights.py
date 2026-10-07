"""Packed-weight calibration tests (GOL-3201).

``calibrate_weights.py`` never writes live — it reads recorded shipped weights
and reports predicted vs. packed per box. These tests pin: the two input shapes
parse; the median/basis logic matches ``shipping_boxes.weight_basis``; a box
under the sample floor (or force-conservative) stays on the conservative weight;
the tare/per-tree least-squares fit recovers planted parameters; and the exit
codes follow the rate-check family (0 no change / 3 refresh / 2 bad input).
"""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout


def _load(name, relpath):
    path = os.path.join(os.path.dirname(__file__), "..", relpath)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cw = _load("calibrate_weights", "calibrate_weights.py")
sb = _load("sb_for_calib", os.path.join("..", "..", "grove_headless", "models", "shipping_boxes.py"))

# The 13 shipped small-box packed weights (CEO Odoo join, GOL-3200): median 6.5.
SMALL_13 = [6.5, 6.5, 6.5, 6.5, 6.5, 6.5, 6.5, 6.5, 6.5, 8.5, 8.5, 12.5, 14.5]
FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "weight_samples_example.json")


def _write(doc) -> str:
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(doc, fh)
    fh.close()
    return fh.name


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cw.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestStats(unittest.TestCase):
    def test_median_odd_and_even(self):
        self.assertEqual(cw.median([1, 2, 3]), 2)
        self.assertEqual(cw.median([1, 2, 3, 4]), 2.5)

    def test_small_13_median_is_6_5(self):
        self.assertEqual(cw.median(SMALL_13), 6.5)

    def test_percentile_nearest_rank(self):
        self.assertEqual(cw.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90), 9)
        self.assertEqual(cw.percentile([5], 90), 5)


class TestLoadSamples(unittest.TestCase):
    def test_grouped_shape(self):
        grouped = cw.load_samples(_write({"small": SMALL_13, "large": [13.0, 13.5]}))
        self.assertEqual([w for w, _ in grouped["small"]], SMALL_13)
        self.assertEqual(len(grouped["large"]), 2)

    def test_golden_labels_shape(self):
        doc = {"labels": [{"box_id": "small", "packed_weight_lb": w} for w in SMALL_13]}
        grouped = cw.load_samples(_write(doc))
        self.assertEqual(len(grouped["small"]), 13)

    def test_underscore_keys_ignored(self):
        grouped = cw.load_samples(_write({"_doc": "x", "small": [6.5, 6.5]}))
        self.assertEqual(set(grouped), {"small"})

    def test_object_sample_with_count(self):
        grouped = cw.load_samples(_write({"small": [{"weight_lb": 6.5, "count": 3}]}))
        self.assertEqual(grouped["small"], [(6.5, 3)])

    def test_bad_inputs_raise(self):
        for doc in ([1, 2, 3], {"small": "nope"}, {"small": [0]}, {"small": [-5]}, {"small": [{"count": 3}]}):
            with self.assertRaises(cw.BadSamples):
                cw.load_samples(_write(doc))

    def test_missing_file_raises(self):
        with self.assertRaises(cw.BadSamples):
            cw.load_samples("/no/such/file.json")

    def test_empty_samples_raise(self):
        with self.assertRaises(cw.BadSamples):
            cw.load_samples(_write({"small": []}))


class TestCalibrateBox(unittest.TestCase):
    def test_small_verifies_and_does_not_move(self):
        # 13 samples, median 6.5 -> ceil 7, which is what `small` already
        # publishes: verified basis, no move.
        rec = cw.calibrate_box("small", [(w, None) for w in SMALL_13])
        self.assertEqual(rec["calibrated_basis"], "verified")
        self.assertEqual(rec["observed"]["median"], 6.5)
        self.assertEqual(rec["calibrated_billable_lb"], 7)
        self.assertEqual(rec["current_published_lb"], 7)
        self.assertFalse(rec["moves"])

    def test_small_moves_when_median_shifts(self):
        heavy = [(w, None) for w in [9.0] * 13]  # median 9 -> ceil 9 != 7
        rec = cw.calibrate_box("small", heavy)
        self.assertEqual(rec["calibrated_billable_lb"], 9)
        self.assertTrue(rec["moves"])

    def test_few_samples_stay_unverified_conservative(self):
        rec = cw.calibrate_box("small", [(6.5, None), (6.5, None)])  # n=2 < 5
        self.assertEqual(rec["calibrated_basis"], "unverified")
        # Keeps the conservative full-capacity weight, not ceil(median)=7 (same
        # number here, but it comes from the conservative path, not the median).
        self.assertEqual(rec["calibrated_billable_lb"], sb.conservative_billable_lb("small"))
        # And because the seed publishes `small` as verified, dropping to 2
        # samples is a basis regression the report flags for a human (moves).
        self.assertTrue(rec["moves"])

    def test_large_force_conservative_even_with_samples(self):
        rec = cw.calibrate_box("large", [(11.0, None)] * 20)  # plenty of samples
        self.assertTrue(rec["force_conservative"])
        self.assertEqual(rec["calibrated_basis"], "unverified")
        self.assertEqual(rec["calibrated_billable_lb"], sb.representative_billable_lb("large"))
        self.assertFalse(rec["moves"])

    def test_unknown_box_is_flagged_not_crashed(self):
        rec = cw.calibrate_box("jumbo", [(20.0, None)] * 6)
        self.assertFalse(rec["known_box"])
        self.assertIsNone(rec["current_published_lb"])
        self.assertFalse(rec["moves"])  # unknown boxes never recommend a move


class TestFit(unittest.TestCase):
    def test_recovers_planted_parameters(self):
        # weight = 4.5 tare + 0.5/tree, exactly -> fit must recover it.
        samples = [(4.5 + 0.5 * c, c) for c in (1, 2, 3, 4, 5)]
        fit = cw.fit_tare_and_per_tree(samples)
        self.assertAlmostEqual(fit["tare_total_lb"], 4.5, places=2)
        self.assertAlmostEqual(fit["per_tree_lb"], 0.5, places=3)
        self.assertAlmostEqual(fit["rms_residual_lb"], 0.0, places=6)

    def test_none_without_distinct_counts(self):
        self.assertIsNone(cw.fit_tare_and_per_tree([(6.5, None)] * 5))
        self.assertIsNone(cw.fit_tare_and_per_tree([(6.5, 3)] * 5))  # one distinct count


class TestReportAndExit(unittest.TestCase):
    def test_fixture_no_change_exit_0(self):
        code, _out, err = _run(["--samples", FIXTURE])
        self.assertEqual(code, 0)
        self.assertIn("no change needed", err)

    def test_fixture_json_report_shape(self):
        code, out, _err = _run(["--samples", FIXTURE, "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["boxes"]["small"]["calibrated_basis"], "verified")
        self.assertEqual(report["boxes"]["large"]["calibrated_basis"], "unverified")
        self.assertFalse(report["needs_refresh"])

    def test_shifted_median_triggers_refresh_exit_3(self):
        path = _write({"small": [9.0] * 13})
        code, _out, err = _run(["--samples", path])
        self.assertEqual(code, 3)
        self.assertIn("refresh PR recommended", err)
        self.assertIn("WEIGHT_CALIBRATION", err)

    def test_bad_input_exit_2(self):
        path = _write([1, 2, 3])
        code, _out, err = _run(["--samples", path])
        self.assertEqual(code, 2)
        self.assertIn("bad --samples input", err)

    def test_fragment_lists_only_verified(self):
        report = cw.build_report(cw.load_samples(FIXTURE))
        frag = cw.calibration_fragment(report)
        self.assertIn('"small"', frag)
        self.assertNotIn('"large"', frag)


if __name__ == "__main__":
    unittest.main()
