"""Regression: html content fields must not be tracked (GOL-2677). DB test — Odoo runner only.

Odoo 19's mail.tracking.value._create_tracking_values raises NotImplementedError for
html column types. A `fields.Html(tracking=True)` therefore explodes at flush on every
write to the field on a mail.thread record — which broke both grove-content-drafter and
manual admin edits of description_ecommerce / website_description. This test writes both
html fields and forces a flush; it fails with NotImplementedError if tracking creeps back.
"""

from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestHtmlFieldTracking(GroveTaxFixtureMixin, TransactionCase):
    def _tmpl(self):
        return self.env["product.template"].create({"name": "Test PawPaw", "type": "consu"})

    def test_html_content_fields_not_tracked(self):
        """description_ecommerce / website_description are html and MUST NOT set tracking=True."""
        for fname in ("description_ecommerce", "website_description"):
            field = self.env["product.template"]._fields[fname]
            self.assertEqual(field.type, "html", f"{fname} should be an html field")
            # Odoo 19 only sets the ``tracking`` attribute on a field when tracking is
            # enabled, so a clean (untracked) html field has no such attribute — read it
            # defensively rather than touching ``field.tracking`` directly.
            self.assertFalse(
                getattr(field, "tracking", False),
                f"{fname} must not be tracked — Odoo 19 mail.tracking.value cannot track html "
                "(NotImplementedError at flush). See GOL-2677.",
            )

    def test_write_html_fields_flushes_without_raising(self):
        """Writing both html fields then flushing must not raise (the GOL-2677 prod failure)."""
        tmpl = self._tmpl()
        # Force the write onto an already-persisted record so mail tracking (which only
        # runs on write, not create) would fire if the fields were tracked.
        self.env.flush_all()
        tmpl.write(
            {
                "description_ecommerce": "<p>A sweet custard-apple relative native to the eastern US.</p>",
                "website_description": "<p>Plant in part shade for the first two years, then full sun.</p>",
            }
        )
        # Precommit flush is where _create_tracking_values would have raised.
        self.env.flush_all()
        self.assertIn("custard-apple", tmpl.description_ecommerce)
        self.assertIn("part shade", tmpl.website_description)
