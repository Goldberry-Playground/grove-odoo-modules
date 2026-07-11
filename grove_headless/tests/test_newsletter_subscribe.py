"""Odoo-runtime tests for the newsletter opt-in endpoint (GOL-221).

Run via:
    odoo --addons-path=... --test-enable --stop-after-init -i grove_headless

The CI install-smoke-test job in .github/workflows/ci.yml runs them on every PR.
Two layers:
  * TransactionCase — the ORM-touching helpers (`_get_or_create_partner_categories`,
    `_log_newsletter_attribution`) against a real DB, no HTTP.
  * HttpCase — the full `/grove/api/v1/newsletter/subscribe` route end to end,
    exercising bearer auth, consent gating, upsert idempotency, and tagging.
"""

import json
from datetime import datetime, timedelta

from odoo.tests.common import HttpCase, TransactionCase, get_db_name, tagged

from ..controllers.main import _get_or_create_partner_categories, _log_newsletter_attribution


@tagged("grove_headless", "newsletter", "post_install", "-at_install")
class TestNewsletterHelpers(TransactionCase):
    def test_get_or_create_is_idempotent(self):
        """Calling twice with the same names creates each tag once and returns
        stable ids in the requested order."""
        names = ["newsletter", "brand:goldberry", "interest:fruit"]
        first = _get_or_create_partner_categories(self.env, names)
        second = _get_or_create_partner_categories(self.env, names)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)
        # No duplicate category records were created on the second call.
        for name in names:
            found = self.env["res.partner.category"].search([("name", "=", name)])
            self.assertEqual(len(found), 1, f"tag {name!r} should exist exactly once")

    def test_get_or_create_dedups_and_drops_blanks(self):
        ids = _get_or_create_partner_categories(self.env, ["newsletter", "newsletter", "", None])
        self.assertEqual(len(ids), 1)

    def test_log_attribution_posts_note(self):
        partner = self.env["res.partner"].create({"name": "Ada", "email": "ada@example.com"})
        before = len(partner.message_ids)
        _log_newsletter_attribution(partner, "homepage_footer", {"utm_source": "google"})
        partner.invalidate_recordset(["message_ids"])
        self.assertGreater(len(partner.message_ids), before)
        body = partner.message_ids[0].body
        self.assertIn("homepage_footer", body)
        self.assertIn("google", body)

    def test_log_attribution_noop_when_empty(self):
        partner = self.env["res.partner"].create({"name": "Bo", "email": "bo@example.com"})
        before = len(partner.message_ids)
        _log_newsletter_attribution(partner, None, None)
        partner.invalidate_recordset(["message_ids"])
        self.assertEqual(len(partner.message_ids), before)


@tagged("grove_headless", "newsletter", "post_install", "-at_install")
class TestNewsletterEndpoint(HttpCase):
    def setUp(self):
        super().setUp()
        self.company = self.env.ref("base.main_company")  # Goldberry (renamed)
        # Generate an API key for admin so the bearer-auth route accepts us.
        # Guarded: if the _generate signature differs across Odoo point
        # releases, skip rather than fail the whole suite.
        try:
            admin = self.env.ref("base.user_admin")
            self.api_key = (
                self.env["res.users.apikeys"]
                .with_user(admin)
                ._generate("rpc", "grove-newsletter-test", datetime.now() + timedelta(days=1))
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"could not mint an API key for bearer auth: {exc}")

    def _headers(self, **extra):
        headers = {
            "X-Odoo-Database": get_db_name(),
            "X-Grove-Tenant": "goldberry",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        headers.update(extra)
        return headers

    def _post(self, body, **extra):
        return self.url_open(
            "/grove/api/v1/newsletter/subscribe",
            data=json.dumps(body).encode(),
            headers=self._headers(**extra),
        )

    def test_subscribe_creates_and_tags_partner(self):
        resp = self._post(
            {
                "email": "newsub@example.com",
                "name": "New Sub",
                "brand": "goldberry",
                "interests": ["fruit", "nuts"],
                "source": "homepage_footer",
                "consent": True,
                "attribution": {"utm_source": "instagram"},
            }
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["created"])
        self.assertIn("newsletter", body["tags"])
        self.assertIn("brand:goldberry", body["tags"])
        self.assertIn("interest:fruit", body["tags"])
        self.assertIn("source:homepage_footer", body["tags"])

        partner = self.env["res.partner"].browse(body["partner_id"])
        self.assertEqual(partner.email, "newsub@example.com")
        self.assertEqual(partner.company_id, self.company)
        tag_names = set(partner.category_id.mapped("name"))
        self.assertTrue({"newsletter", "brand:goldberry", "interest:fruit"} <= tag_names)

    def test_resubscribe_reuses_partner_and_adds_tags(self):
        first = self._post({"email": "dupe@example.com", "consent": True, "brand": "goldberry"})
        self.assertEqual(first.status_code, 200)
        pid = first.json()["partner_id"]

        second = self._post({"email": "dupe@example.com", "consent": True, "interests": ["seeds"]})
        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertEqual(body["partner_id"], pid)  # same partner
        self.assertFalse(body["created"])
        partner = self.env["res.partner"].browse(pid)
        tag_names = set(partner.category_id.mapped("name"))
        # Tags from both calls accumulate.
        self.assertTrue({"newsletter", "brand:goldberry", "interest:seeds"} <= tag_names)

    def test_missing_consent_is_rejected(self):
        resp = self._post({"email": "noconsent@example.com"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("consent", resp.json()["error"])
        self.assertFalse(
            self.env["res.partner"].search([("email", "=", "noconsent@example.com")]),
            "a subscribe without consent must not create a partner",
        )

    def test_invalid_email_is_rejected(self):
        resp = self._post({"email": "not-an-email", "consent": True})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("email", resp.json()["error"])

    def test_missing_email_is_rejected(self):
        resp = self._post({"consent": True})
        self.assertEqual(resp.status_code, 400)

    def test_requires_bearer_auth(self):
        """No Authorization header → the bearer route rejects (not a 200)."""
        resp = self.url_open(
            "/grove/api/v1/newsletter/subscribe",
            data=json.dumps({"email": "x@example.com", "consent": True}).encode(),
            headers={
                "X-Odoo-Database": get_db_name(),
                "X-Grove-Tenant": "goldberry",
                "Content-Type": "application/json",
            },
        )
        self.assertNotEqual(resp.status_code, 200)
