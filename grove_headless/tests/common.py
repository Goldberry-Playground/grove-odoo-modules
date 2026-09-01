"""Shared fixtures for grove_headless Odoo-runtime (TransactionCase/HttpCase) tests.

Why GroveTaxFixtureMixin exists
===============================
The ``install-smoke-test`` CI job installs grove_headless into a *minimal*
``--without-demo`` Odoo 19 database that has the ``account`` module but **no
chart of accounts** (no ``l10n_*`` localisation is installed). In that
environment Odoo purges the WV sales-tax records created at install time — they
have no chart template to anchor to — right after "Modules loaded":

    INFO odoo.models.unlink: User #1 deleted account.tax records with IDs: [3, 4, 5, 1, 2]

That leaves the company's *default* sale tax dangling: ``ir.default`` on
``product.template.taxes_id`` and ``res.company.account_sale_tax_id`` still
point at a now-deleted ``account.tax`` row. Every subsequent
``product.template`` / ``product.product`` create then blows up while Odoo
precomputes ``taxes_id``:

    MissingError: Record does not exist or has been deleted. (Record: account.tax(5,))

QA and production both run a real chart of accounts, so the taxes persist there
and this only bites the minimal CI database.

The fix re-runs the module's own (idempotent) WV tax binding *inside the test
transaction*. That re-creates a live WV group tax and repoints the company
default at it, so product creation resolves cleanly and the WV-tax assertions
hold. The install-time purge is a module-loading step that does not run during a
test transaction, so the freshly-created taxes survive for the life of the test
class. Where a valid default already exists (QA/prod, or a CI DB that grows a
chart later) it is a harmless idempotent no-op.
"""

from odoo.addons.grove_headless.hooks import setup_wv_sales_tax


class GroveTaxFixtureMixin:
    """Re-establish the WV sales-tax binding so ``product.*`` creates resolve.

    Mix in *before* the concrete Odoo test base so this ``setUpClass`` runs
    first and delegates to it via ``super()``:

        class TestFoo(GroveTaxFixtureMixin, TransactionCase):
            ...
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        setup_wv_sales_tax(cls.env)
