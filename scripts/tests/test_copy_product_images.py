"""Pure tests for the GOL-3785 template-id path of ``copy_product_images.py``
(no Odoo, no network).

The SKU path is I/O all the way down; what is new and silent-when-wrong is the
id selector: a typo in ``TEMPLATE_IDS`` must be a hard error (never a quietly
narrower set), ``bundles`` must expand to the five nursery bundle templates, and
the name guard must refuse an id that names a different product on the two
sides. GOL-3793: QA no longer shares prod's ids, so a source id whose name is
absent at that id on DEST falls back to exactly one DEST template of that name.
"""

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stdout

_PATH = os.path.join(os.path.dirname(__file__), "..", "copy_product_images.py")
_spec = importlib.util.spec_from_file_location("copy_product_images", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class TestParseTemplateIds(unittest.TestCase):
    def test_empty_is_empty(self):
        self.assertEqual(mod.parse_template_ids(""), [])
        self.assertEqual(mod.parse_template_ids(" , "), [])

    def test_bundles_keyword_expands_to_the_five_bundle_templates(self):
        self.assertEqual(mod.parse_template_ids("bundles"), [22, 132, 134, 135, 140])
        self.assertEqual(mod.parse_template_ids("BUNDLES"), list(mod.BUNDLE_TEMPLATE_IDS))

    def test_literal_ids_keep_order_and_drop_duplicates(self):
        self.assertEqual(mod.parse_template_ids("132, 22,132,7"), [132, 22, 7])

    def test_keyword_and_literals_mix_without_duplicates(self):
        self.assertEqual(mod.parse_template_ids("7,bundles,132"), [7, 22, 132, 134, 135, 140])

    def test_non_integer_is_a_hard_error(self):
        for bad in ("abc", "22;132", "-1", "0", "1.5", "bundle"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                mod.parse_template_ids(bad)


class TestSameTemplateName(unittest.TestCase):
    def test_case_and_whitespace_are_forgiven(self):
        self.assertTrue(mod.same_template_name("Mountain Mama Package", "mountain  mama package "))

    def test_punctuation_and_numbers_are_not(self):
        self.assertFalse(mod.same_template_name("At the Grove Food Forest (50)", "At the Grove Food Forest (100)"))
        self.assertFalse(mod.same_template_name("Chestnut Grove", "Chestnut Grove (5-Tree Native Bundle)"))

    def test_missing_names_never_match(self):
        self.assertFalse(mod.same_template_name(None, "x"))
        self.assertFalse(mod.same_template_name("", ""))


class _FakeOdoo:
    """Just enough of the XML-RPC handle for resolve_template_by_id."""

    def __init__(self, label, names):
        self.label = label
        self._names = names

    def call(self, model, method, args, kwargs=None):
        assert (model, method) == ("product.template", "search_read")
        domain = args[0]
        tid = next((v for f, _, v in domain if f == "id"), None)
        if tid is not None:
            name = self._names.get(tid)
            return [{"id": tid, "name": name}] if name else []
        needle = next(v for f, op, v in domain if f == "name" and op == "ilike").casefold()
        return [{"id": i, "name": n} for i, n in self._names.items() if needle in n.casefold()]


class TestResolveTemplateById(unittest.TestCase):
    def _resolve(self, src_names, dst_names, tid):
        buf = io.StringIO()
        with redirect_stdout(buf):
            got = mod.resolve_template_by_id(_FakeOdoo("SRC", src_names), _FakeOdoo("DST", dst_names), tid, 9, 9)
        return got, buf.getvalue()

    def test_same_name_both_sides_is_accepted_with_a_readable_label(self):
        got, out = self._resolve({132: "Mountain Mama Package"}, {132: "Mountain Mama Package"}, 132)
        self.assertEqual(got, ("#132 Mountain Mama Package", 132))
        self.assertEqual(out, "")

    def test_missing_on_source_is_skipped(self):
        got, out = self._resolve({}, {132: "Mountain Mama Package"}, 132)
        self.assertIsNone(got)
        self.assertIn("MISSING", out)
        self.assertIn("[SRC]", out)

    def test_missing_by_id_and_name_on_dest_is_skipped(self):
        got, out = self._resolve({132: "Mountain Mama Package"}, {}, 132)
        self.assertIsNone(got)
        self.assertIn("MISSING", out)
        self.assertIn("[DST]", out)

    def test_different_dest_id_is_found_by_exact_name(self):
        # GOL-3793: 132 on prod, 350 on QA; QA's 132 does not exist.
        got, out = self._resolve({132: "Mountain Mama Package"}, {350: "mountain mama package"}, 132)
        self.assertEqual(got, ("#132 Mountain Mama Package", 350))
        self.assertIn("MATCHED", out)
        self.assertIn("350", out)

    def test_name_fallback_wins_over_a_different_product_at_the_same_id(self):
        got, _ = self._resolve({132: "Mountain Mama Package"}, {132: "Sticker", 350: "Mountain Mama Package"}, 132)
        self.assertEqual(got, ("#132 Mountain Mama Package", 350))

    def test_name_fallback_needs_the_whole_name(self):
        # A substring hit ("Chestnut Grove Canvas Tote") is not the bundle.
        got, out = self._resolve({22: "Chestnut Grove"}, {176: "Chestnut Grove Canvas Tote"}, 22)
        self.assertIsNone(got)
        self.assertIn("MISSING", out)

    def test_two_dest_templates_of_the_name_are_refused(self):
        got, out = self._resolve(
            {132: "Mountain Mama Package"}, {350: "Mountain Mama Package", 351: "Mountain Mama Package"}, 132
        )
        self.assertIsNone(got)
        self.assertIn("AMBIGUOUS", out)
        self.assertIn("[350, 351]", out)

    def test_name_mismatch_is_refused(self):
        got, out = self._resolve({135: "At the Grove Food Forest (50)"}, {135: "Centennial Food Forest (100)"}, 135)
        self.assertIsNone(got)
        self.assertIn("MISMATCH", out)

    def test_e2e_fixture_is_blocked_by_name(self):
        got, out = self._resolve({900: "AAA QA E2E Potted"}, {900: "AAA QA E2E Potted"}, 900)
        self.assertIsNone(got)
        self.assertIn("BLOCKED", out)


if __name__ == "__main__":
    unittest.main()
