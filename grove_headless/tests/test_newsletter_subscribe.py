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

from ..controllers.main import (
    _get_or_create_partner_categories,
    _log_newsletter_attribution,
    _newsletter_token,
)
from ..models.newsletter import resolve_newsletter_lists


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
class TestResolveNewsletterLists(TransactionCase):
    """Pure list-resolution logic (GOL-3454 §2) — no ORM, no HTTP."""

    def test_explicit_lists_win_and_dedupe(self):
        self.assertEqual(
            resolve_newsletter_lists("goldberry", requested=["nursery", "digest", "nursery"]),
            ["nursery", "digest"],
        )

    def test_explicit_lists_drop_unknown_and_blank(self):
        self.assertEqual(
            resolve_newsletter_lists("nursery", requested=["farm", "bogus", "", 7]),
            ["farm"],
        )

    def test_all_invalid_explicit_falls_back_to_tenant_default(self):
        # A payload of only junk must not subscribe the contact to nothing.
        self.assertEqual(resolve_newsletter_lists("nursery", requested=["bogus"]), ["nursery"])

    def test_tenant_default_without_multibrand(self):
        self.assertEqual(resolve_newsletter_lists("goldberry"), ["farm"])
        self.assertEqual(resolve_newsletter_lists("nursery"), ["nursery"])
        self.assertEqual(resolve_newsletter_lists("ggg"), ["digest"])

    def test_hub_and_unknown_tenant_ride_the_digest(self):
        self.assertEqual(resolve_newsletter_lists(None), ["digest"])
        self.assertEqual(resolve_newsletter_lists("hub"), ["digest"])

    def test_multibrand_adds_every_other_list_primary_first(self):
        result = resolve_newsletter_lists("goldberry", multibrand=True)
        self.assertEqual(result[0], "farm")
        self.assertEqual(set(result), {"nursery", "farm", "digest"})
        self.assertEqual(len(result), 3)


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


@tagged("grove_headless", "newsletter", "post_install", "-at_install")
class TestNewsletterListOfRecord(HttpCase):
    """The mailing.list subscription side of the subscribe route plus the
    one-click unsubscribe and per-list preferences routes (GOL-3454 Step 2)."""

    def setUp(self):
        super().setUp()
        try:
            admin = self.env.ref("base.user_admin")
            self.api_key = (
                self.env["res.users.apikeys"]
                .with_user(admin)
                ._generate("rpc", "grove-newsletter-lor-test", datetime.now() + timedelta(days=1))
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"could not mint an API key for bearer auth: {exc}")
        self.farm = self.env.ref("grove_headless.mailing_list_farm")
        self.nursery = self.env.ref("grove_headless.mailing_list_nursery")
        self.digest = self.env.ref("grove_headless.mailing_list_digest")

    def _headers(self, tenant="goldberry", **extra):
        headers = {
            "X-Odoo-Database": get_db_name(),
            "X-Grove-Tenant": tenant,
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        headers.update(extra)
        return headers

    def _subscribe(self, body, tenant="goldberry"):
        return self.url_open(
            "/grove/api/v1/newsletter/subscribe",
            data=json.dumps(body).encode(),
            headers=self._headers(tenant=tenant),
        )

    def _contact(self, email):
        return self.env["mailing.contact"].search([("email_normalized", "=", email.lower())], limit=1)

    def _active_list_ids(self, contact):
        return set(contact.subscription_ids.filtered(lambda s: not s.opt_out).mapped("list_id").ids)

    def test_subscribe_adds_to_tenant_default_list_and_welcomes(self):
        resp = self._subscribe({"email": "farmfan@example.com", "consent": True})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["lists"], ["farm"])
        self.assertTrue(body["welcomed"])
        contact = self._contact("farmfan@example.com")
        self.assertTrue(contact, "a mailing.contact must be created")
        self.assertEqual(self._active_list_ids(contact), {self.farm.id})

    def test_multibrand_subscribes_to_every_list(self):
        resp = self._subscribe({"email": "allbrands@example.com", "consent": True, "multibrand": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.json()["lists"]), {"farm", "nursery", "digest"})
        contact = self._contact("allbrands@example.com")
        self.assertEqual(self._active_list_ids(contact), {self.farm.id, self.nursery.id, self.digest.id})

    def test_explicit_lists_are_honored(self):
        resp = self._subscribe({"email": "picky@example.com", "consent": True, "lists": ["nursery", "digest"]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["lists"], ["nursery", "digest"])
        contact = self._contact("picky@example.com")
        self.assertEqual(self._active_list_ids(contact), {self.nursery.id, self.digest.id})

    def test_unknown_list_key_is_rejected(self):
        resp = self._subscribe({"email": "bad@example.com", "consent": True, "lists": ["farm", "zzz"]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("zzz", resp.json()["error"])
        self.assertFalse(self._contact("bad@example.com"), "a 400 must not create a contact")

    def test_resubscribe_within_window_sends_no_second_welcome(self):
        first = self._subscribe({"email": "repeat@example.com", "consent": True})
        self.assertTrue(first.json()["welcomed"])
        contact = self._contact("repeat@example.com")
        subs_before = len(contact.subscription_ids)

        second = self._subscribe({"email": "repeat@example.com", "consent": True})
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["welcomed"], "a repeat submit in-window must not re-welcome")
        contact.invalidate_recordset(["subscription_ids"])
        self.assertEqual(len(contact.subscription_ids), subs_before, "no duplicate subscription row")

    def test_one_click_unsubscribe_opts_out_everything_and_blacklists(self):
        self._subscribe({"email": "leaving@example.com", "consent": True, "multibrand": True})
        contact = self._contact("leaving@example.com")
        token = _newsletter_token(self.env, contact.id)

        resp = self.url_open(
            f"/grove/api/v1/newsletter/unsubscribe?c={contact.id}&t={token}",
            data=b"List-Unsubscribe=One-Click",
            headers={"X-Odoo-Database": get_db_name(), "X-Grove-Tenant": "goldberry"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["unsubscribed"])
        contact.invalidate_recordset(["subscription_ids"])
        self.assertEqual(self._active_list_ids(contact), set(), "every list opted out")
        self.assertTrue(
            self.env["mail.blacklist"].search([("email", "=", "leaving@example.com")]),
            "one-click unsubscribe blacklists the email",
        )

    def test_unsubscribe_with_bad_token_is_rejected(self):
        self._subscribe({"email": "safe@example.com", "consent": True})
        contact = self._contact("safe@example.com")
        resp = self.url_open(
            f"/grove/api/v1/newsletter/unsubscribe?c={contact.id}&t=forged",
            data=b"",
            headers={"X-Odoo-Database": get_db_name(), "X-Grove-Tenant": "goldberry"},
        )
        self.assertEqual(resp.status_code, 400)
        contact.invalidate_recordset(["subscription_ids"])
        self.assertEqual(self._active_list_ids(contact), {self.farm.id}, "a forged token changes nothing")

    def test_preferences_get_and_post_roundtrip(self):
        self._subscribe({"email": "prefs@example.com", "consent": True, "multibrand": True})
        contact = self._contact("prefs@example.com")
        token = _newsletter_token(self.env, contact.id)

        got = self.url_open(
            f"/grove/api/v1/newsletter/preferences?c={contact.id}&t={token}",
            headers={"X-Odoo-Database": get_db_name(), "X-Grove-Tenant": "goldberry"},
        )
        self.assertEqual(got.status_code, 200)
        state = got.json()
        self.assertEqual({row["key"] for row in state["lists"]}, {"nursery", "farm", "digest"})
        self.assertTrue(all(row["subscribed"] for row in state["lists"]))

        posted = self.url_open(
            "/grove/api/v1/newsletter/preferences",
            data=json.dumps({"c": contact.id, "t": token, "lists": {"nursery": False, "digest": False}}).encode(),
            headers={
                "X-Odoo-Database": get_db_name(),
                "X-Grove-Tenant": "goldberry",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(posted.status_code, 200)
        contact.invalidate_recordset(["subscription_ids"])
        self.assertEqual(self._active_list_ids(contact), {self.farm.id}, "only farm remains active")
        # Still has one active list → not blacklisted.
        self.assertFalse(self.env["mail.blacklist"].search([("email", "=", "prefs@example.com")]))
