"""Pure tests for the GOL-3785 template-id path of ``copy_product_images.py``
(no Odoo, no network).

The SKU path is I/O all the way down; what is new and silent-when-wrong is the
id selector: a typo in ``TEMPLATE_IDS`` must be a hard error (never a quietly
narrower set), ``bundles`` must expand to the five nursery bundle templates, and
the name guard must refuse an id that names a different product on the two
sides — ids are only trustworthy because QA was cut from prod.
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
        tid = next(v for f, _, v in domain if f == "id")
        name = self._names.get(tid)
        return [{"id": tid, "name": name}] if name else []


class TestResolveTemplateById(unittest.TestCase):
    def _resolve(self, src_names, dst_names, tid):
        buf = io.StringIO()
        with redirect_stdout(buf):
            label = mod.resolve_template_by_id(_FakeOdoo("SRC", src_names), _FakeOdoo("DST", dst_names), tid, 9, 9)
        return label, buf.getvalue()

    def test_same_name_both_sides_is_accepted_with_a_readable_label(self):
        label, out = self._resolve({132: "Mountain Mama Package"}, {132: "Mountain Mama Package"}, 132)
        self.assertEqual(label, "#132 Mountain Mama Package")
        self.assertEqual(out, "")

    def test_missing_on_either_side_is_skipped(self):
        label, out = self._resolve({132: "Mountain Mama Package"}, {}, 132)
        self.assertIsNone(label)
        self.assertIn("MISSING", out)
        self.assertIn("[DST]", out)

    def test_name_mismatch_is_refused(self):
        label, out = self._resolve({135: "At the Grove Food Forest (50)"}, {135: "Centennial Food Forest (100)"}, 135)
        self.assertIsNone(label)
        self.assertIn("MISMATCH", out)

    def test_e2e_fixture_is_blocked_by_name(self):
        label, out = self._resolve({900: "AAA QA E2E Potted"}, {900: "AAA QA E2E Potted"}, 900)
        self.assertIsNone(label)
        self.assertIn("BLOCKED", out)


if __name__ == "__main__":
    unittest.main()
