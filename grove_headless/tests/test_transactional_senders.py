"""Tests for the transactional email sender identity (GOL-465).

Verifies that the post_init/upgrade hook stamps each Grove company's
``notifications@<domain>`` From address on ``res.company.email`` — the
fallback From Odoo uses for order/receipt/ship/back-in-stock mail — and that
the operation is idempotent and safe when a company is missing.

The SMTP relay itself (odoo.conf SMTP_* env) is infrastructure and is not
exercised here; this covers only the sender-identity wiring the module owns.
"""

from odoo.addons.grove_headless.hooks import (
    COMPANY_NOTIFICATION_SENDERS,
    setup_transactional_senders,
)
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestTransactionalSenders(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Build isolated companies matching the mapping's natural keys so the
        # test does not depend on the seeded grove_companies.xml records.
        Company = cls.env["res.company"]
        cls.companies = {
            name: Company.create({"name": name})
            for name in COMPANY_NOTIFICATION_SENDERS
        }

    def test_sets_notification_sender_per_company(self):
        setup_transactional_senders(self.env)
        for name, sender in COMPANY_NOTIFICATION_SENDERS.items():
            self.assertEqual(
                self.companies[name].email,
                sender,
                f"{name} should send transactional mail From {sender}",
            )

    def test_woodworking_domain_is_single_g(self):
        # Guards the live/registered zone: woodworkingeorge.com (single-g) is
        # the real domain; the double-g variant is not registered.
        self.assertEqual(
            COMPANY_NOTIFICATION_SENDERS["George George George Woodworking"],
            "notifications@woodworkingeorge.com",
        )

    def test_idempotent(self):
        setup_transactional_senders(self.env)
        before = {n: c.email for n, c in self.companies.items()}
        setup_transactional_senders(self.env)
        after = {n: c.email for n, c in self.companies.items()}
        self.assertEqual(before, after)

    def test_missing_company_does_not_raise(self):
        # A DB without one of the companies must not abort the hook/migration.
        self.companies["At The Grove Nursery"].unlink()
        setup_transactional_senders(self.env)  # should not raise
        self.assertEqual(
            self.companies["Goldberry Grove Farm"].email,
            COMPANY_NOTIFICATION_SENDERS["Goldberry Grove Farm"],
        )
