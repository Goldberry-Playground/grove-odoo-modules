"""Pure tests for storefront capture routing (consult / wholesale / visit /
state-waitlist leads). No Odoo runtime: the module is loaded by path."""

import importlib.util
import os
import unittest

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "models", "capture_leads.py")
_spec = importlib.util.spec_from_file_location("grove_capture_leads", _MODULE_PATH)
cl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cl)


class TestValidateLead(unittest.TestCase):
    def test_minimal_valid(self):
        clean, err = cl.validate_lead({"email": "a@b.co", "kind": "wholesale"})
        self.assertIsNone(err)
        self.assertEqual(clean["email"], "a@b.co")
        self.assertEqual(clean["kind"], "wholesale")
        self.assertEqual(clean["name"], "")

    def test_email_required_and_shape(self):
        self.assertEqual(cl.validate_lead({"kind": "wholesale"})[1], "email is required")
        self.assertEqual(
            cl.validate_lead({"email": "nope", "kind": "wholesale"})[1], "email is not a valid email address"
        )

    def test_unknown_kind(self):
        _clean, err = cl.validate_lead({"email": "a@b.co", "kind": "spam"})
        self.assertIn("kind must be one of", err)

    def test_state_waitlist_needs_state(self):
        self.assertEqual(
            cl.validate_lead({"email": "a@b.co", "kind": "state_waitlist"})[1], "state is required for state_waitlist"
        )
        clean, err = cl.validate_lead({"email": "a@b.co", "kind": "state_waitlist", "state": " ca "})
        self.assertIsNone(err)
        self.assertEqual(clean["state"], "CA")

    def test_lengths_enforced(self):
        _clean, err = cl.validate_lead({"email": "a@b.co", "kind": "wholesale", "message": "x" * (cl.MAX_MESSAGE + 1)})
        self.assertIn("message", err)

    def test_product_id_must_be_int(self):
        self.assertEqual(
            cl.validate_lead({"email": "a@b.co", "kind": "food_forest_consult", "product_id": "abc"})[1],
            "product_id must be an integer",
        )
        clean, _err = cl.validate_lead({"email": "a@b.co", "kind": "food_forest_consult", "product_id": "134"})
        self.assertEqual(clean["product_id"], 134)


class TestLeadText(unittest.TestCase):
    def test_title_uses_kind_label_and_context(self):
        self.assertEqual(cl.lead_title("wholesale", "Ada", "a@b.co"), "Wholesale inquiry: Ada")
        self.assertEqual(cl.lead_title("wholesale", "", "a@b.co"), "Wholesale inquiry: a@b.co")
        self.assertEqual(
            cl.lead_title("food_forest_consult", "Ada", "a@b.co", product_name="Centennial Food Forest (100 Trees)"),
            "Food forest consult: Ada (Centennial Food Forest (100 Trees))",
        )
        self.assertEqual(cl.lead_title("state_waitlist", "Ada", "a@b.co", state="CA"), "New-state waitlist: Ada (CA)")

    def test_tag_names(self):
        self.assertEqual(cl.lead_tag_names("farm_visit", "upick"), ["Farm visit request", "farm-visit:upick"])
        self.assertEqual(cl.lead_tag_names("wholesale", ""), ["Wholesale inquiry"])

    def test_description_lists_fields(self):
        text = cl.lead_description(
            {"kind": "farm_visit", "message": "Can we bring 20 kids?", "label": "upick", "phone": "304", "state": ""},
            source="goldberry /visit/upick",
        )
        self.assertIn("Can we bring 20 kids?", text)
        self.assertIn("Form: upick", text)
        self.assertIn("Phone: 304", text)
        self.assertIn("Captured from: goldberry /visit/upick", text)


class TestStorefrontUrl(unittest.TestCase):
    def test_builds_pdp_url(self):
        self.assertEqual(
            cl.storefront_product_url("https://atthegrovenursery.com", 93), "https://atthegrovenursery.com/shop/93"
        )
        self.assertEqual(
            cl.storefront_product_url("https://atthegrovenursery.com/", 93), "https://atthegrovenursery.com/shop/93"
        )

    def test_missing_domain_returns_none(self):
        self.assertIsNone(cl.storefront_product_url("", 93))
        self.assertIsNone(cl.storefront_product_url(None, 93))

    def test_bare_host_gets_https(self):
        self.assertEqual(cl.storefront_product_url("goldberrygrove.farm", 7), "https://goldberrygrove.farm/shop/7")
