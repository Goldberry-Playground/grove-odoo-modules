"""Tests for the pure newsletter tag-name builder (GOL-221).

``models/newsletter.py`` is pure Python, so these are plain ``unittest`` cases
with no DB — they run both under Odoo's ``--test-enable`` runner and standalone
(``python3 -m pytest``). The module is loaded by file path so importing it never
drags in the Odoo addon package (mirrors ``test_shipping_zones.py``).
"""

import importlib.util
import os
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "newsletter.py")
_spec = importlib.util.spec_from_file_location("grove_newsletter", _MODULE_PATH)
nl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nl)


class TestNewsletterTagNames(unittest.TestCase):
    def test_marker_tag_always_present(self):
        """Every opt-in carries the `newsletter` marker, even with no brand."""
        self.assertEqual(nl.newsletter_tag_names(None, None), ["newsletter"])
        self.assertEqual(nl.newsletter_tag_names("", []), ["newsletter"])

    def test_brand_is_namespaced_and_lowercased(self):
        self.assertEqual(
            nl.newsletter_tag_names("Goldberry", []),
            ["newsletter", "brand:goldberry"],
        )
        # Leading/trailing whitespace is trimmed.
        self.assertEqual(
            nl.newsletter_tag_names("  nursery  ", []),
            ["newsletter", "brand:nursery"],
        )

    def test_interests_namespaced_deduped_and_ordered(self):
        result = nl.newsletter_tag_names("ggg", ["Fruit", "fruit", "  Nuts  ", ""])
        self.assertEqual(
            result,
            ["newsletter", "brand:ggg", "interest:fruit", "interest:nuts"],
        )

    def test_non_string_entries_are_dropped(self):
        """A caller with a valid API key can post junk types — ignore them
        rather than raising."""
        result = nl.newsletter_tag_names(5, ["seeds", 7, None, {"x": 1}])
        self.assertEqual(result, ["newsletter", "interest:seeds"])

    def test_interests_accepts_tuple(self):
        result = nl.newsletter_tag_names(None, ("herbs",))
        self.assertEqual(result, ["newsletter", "interest:herbs"])

    def test_no_duplicate_when_interest_matches_marker_namespace(self):
        """Duplicate interests collapse; distinct interests are preserved."""
        result = nl.newsletter_tag_names("goldberry", ["a", "b", "a", "B"])
        self.assertEqual(
            result,
            ["newsletter", "brand:goldberry", "interest:a", "interest:b"],
        )


if __name__ == "__main__":
    unittest.main()
