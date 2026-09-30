"""Regression: writing the two Html content fields must not raise (GOL-2677).

Odoo 19's ``mail.tracking.value._create_tracking_values`` raises
``NotImplementedError`` for ``html`` column types. When ``description_ecommerce``
and ``website_description`` carried ``tracking=True`` (see product_template.py),
every write to either field on a mail-thread record — the content drafter, the
enrichment path, and plain admin edits alike — exploded at flush time. These
tests write both fields (via create and via a subsequent write) and force a
flush; a re-introduction of ``tracking=True`` on either Html field would make
them raise again. DB tests — Odoo runner only.
"""

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestHtmlFieldTracking(TransactionCase):
    def test_html_fields_not_tracked(self):
        """The two storefront Html fields must not opt into chatter tracking."""
        fields_ = self.env["product.template"]._fields
        self.assertFalse(
            fields_["description_ecommerce"].tracking,
            "description_ecommerce must not set tracking=True (html tracking "
            "raises NotImplementedError at flush — GOL-2677)",
        )
        self.assertFalse(
            fields_["website_description"].tracking,
            "website_description must not set tracking=True (html tracking "
            "raises NotImplementedError at flush — GOL-2677)",
        )

    def test_write_html_fields_does_not_raise(self):
        """Writing both Html fields on a product.template must flush cleanly."""
        tmpl = self.env["product.template"].create({"name": "PawPaw", "type": "consu"})
        tmpl.flush_recordset()

        tmpl.write(
            {
                "description_ecommerce": "<p>Native understory fruit tree.</p>",
                "website_description": "<p>Water weekly the first season.</p>",
            }
        )
        # Force the precommit flush that runs the mail-thread tracking machinery;
        # this is exactly where the NotImplementedError surfaced before the fix.
        tmpl.flush_recordset()

        self.assertIn("understory", tmpl.description_ecommerce)
        self.assertIn("Water weekly", tmpl.website_description)
