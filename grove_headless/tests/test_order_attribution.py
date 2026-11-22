"""Pure tests for the storefront order-attribution sanitiser (GOL-4077).

``models/order_attribution.py`` is pure Python (no Odoo), so these are plain
``unittest`` cases with no DB — they run both under Odoo's ``--test-enable``
runner and standalone (``python3 -m pytest``). The module is loaded by file path
so importing it never drags in the Odoo addon package.

The sanitiser is the untrusted-input boundary: anyone with a storefront bearer
key can POST an ``attribution`` object, so every assertion here is really "junk
in the payload never reaches the order, and a good value lands lowercased".
"""

import importlib.util
import os
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "order_attribution.py")
_spec = importlib.util.spec_from_file_location("grove_order_attribution", _MODULE_PATH)
oa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oa)


class TestSanitizeAttribution(unittest.TestCase):
    def test_full_valid_payload_is_lowercased_and_mapped(self):
        vals = oa.sanitize_attribution(
            {
                "attribution": {
                    "utm_source": "Instagram",
                    "utm_medium": "Email",
                    "utm_campaign": "Fall-Sale-2026",
                    "utm_content": "Hero-CTA",
                    "utm_term": "Pawpaw",
                    "landing_page": "/shop/pawpaw?ref=home",
                    "referrer": "https://l.instagram.com/",
                    "ga_client_id": "GA1.2.1234567890.1699999999",
                    "ga_session_id": "1699999999",
                    "fbp": "fb.1.1699999999.1234567890",
                    "fbc": "fb.1.1699999999.AbCd_-123",
                    "marketing_tags_eligible": True,
                }
            }
        )
        # UTM values trimmed + lowercased so "Email"/"email" never split a report.
        self.assertEqual(vals["grove_utm_source"], "instagram")
        self.assertEqual(vals["grove_utm_medium"], "email")
        self.assertEqual(vals["grove_utm_campaign"], "fall-sale-2026")
        self.assertEqual(vals["grove_utm_content"], "hero-cta")
        self.assertEqual(vals["grove_utm_term"], "pawpaw")
        # URL-ish and id-ish values keep their original case.
        self.assertEqual(vals["grove_landing_page"], "/shop/pawpaw?ref=home")
        self.assertEqual(vals["grove_referrer"], "https://l.instagram.com/")
        self.assertEqual(vals["grove_ga_client_id"], "GA1.2.1234567890.1699999999")
        self.assertEqual(vals["grove_ga_session_id"], "1699999999")
        self.assertEqual(vals["grove_fbp"], "fb.1.1699999999.1234567890")
        self.assertEqual(vals["grove_fbc"], "fb.1.1699999999.AbCd_-123")
        self.assertIs(vals["grove_marketing_tags_eligible"], True)

    def test_missing_attribution_is_empty(self):
        self.assertEqual(oa.sanitize_attribution({}), {})
        self.assertEqual(oa.sanitize_attribution({"attribution": None}), {})
        self.assertEqual(oa.sanitize_attribution({"attribution": "nope"}), {})
        self.assertEqual(oa.sanitize_attribution({"attribution": []}), {})

    def test_whitespace_is_trimmed_and_blank_dropped(self):
        vals = oa.sanitize_attribution({"attribution": {"utm_source": "  google  ", "utm_medium": "   "}})
        self.assertEqual(vals, {"grove_utm_source": "google"})

    def test_unknown_keys_are_ignored_not_fatal(self):
        vals = oa.sanitize_attribution(
            {"attribution": {"utm_source": "google", "gclid": "x", "totally_unknown": {"nested": 1}}}
        )
        self.assertEqual(vals, {"grove_utm_source": "google"})

    def test_non_string_values_are_dropped(self):
        vals = oa.sanitize_attribution(
            {"attribution": {"utm_source": 5, "utm_medium": ["email"], "utm_campaign": None, "utm_content": "ok"}}
        )
        self.assertEqual(vals, {"grove_utm_content": "ok"})

    def test_over_length_values_are_dropped(self):
        long_campaign = "a" * 151  # cap is 150
        vals = oa.sanitize_attribution({"attribution": {"utm_campaign": long_campaign, "utm_source": "ok"}})
        self.assertEqual(vals, {"grove_utm_source": "ok"})

    def test_bad_charset_is_dropped_keeping_good_fields(self):
        # Angle brackets / control chars are not in the UTM charset → dropped.
        vals = oa.sanitize_attribution(
            {"attribution": {"utm_source": "<script>", "utm_medium": "email", "utm_campaign": "a\nb"}}
        )
        self.assertEqual(vals, {"grove_utm_medium": "email"})

    def test_landing_page_with_space_is_dropped(self):
        # A real path/query has no raw spaces; one signals junk.
        vals = oa.sanitize_attribution({"attribution": {"landing_page": "/shop/ pawpaw", "referrer": "/home"}})
        self.assertEqual(vals, {"grove_referrer": "/home"})

    def test_marketing_tags_eligible_only_accepts_real_bool(self):
        # A truthy string must not be read as consent.
        self.assertNotIn(
            "grove_marketing_tags_eligible",
            oa.sanitize_attribution({"attribution": {"marketing_tags_eligible": "true"}}),
        )
        self.assertIs(
            oa.sanitize_attribution({"attribution": {"marketing_tags_eligible": False}})[
                "grove_marketing_tags_eligible"
            ],
            False,
        )

    def test_spec_fields_all_exist_on_mirror_and_group_maps(self):
        spec_fields = {field for _k, field, _lc, _ml, _rx in oa.ATTRIBUTION_SPEC}
        # Every mirror source field is a real spec field.
        for field, _model, _m2o in oa.UTM_MIRROR:
            self.assertIn(field, spec_fields)
        # Every group-by target is a real spec field.
        for field in oa.GROUP_FIELDS.values():
            self.assertIn(field, spec_fields)


if __name__ == "__main__":
    unittest.main()
