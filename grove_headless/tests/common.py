"""Shared fixtures for grove_headless Odoo-runtime (TransactionCase/HttpCase) tests.

Why GroveTaxFixtureMixin exists
===============================
The ``install-smoke-test`` CI job installs grove_headless into a *minimal*
``--without-demo`` Odoo 19 database that has the ``account`` module but **no
chart of accounts** (no ``l10n_*`` localisation is installed). In that
environment Odoo tears down the accounting objects grove_headless stands up at
install — journals, POS configs, payment methods and the WV ``account.tax``
rows — in a cascade right after "Modules loaded":

    unlink: deleted account.journal records with IDs: [2, 3, 1, 4]
    unlink: deleted account.tax records with IDs: [3, 4, 5, 1, 2]

That leaves the company's *default* sale tax dangling: ``ir.default`` on
``product.template.taxes_id`` and ``res.company.account_sale_tax_id`` still
point at a now-deleted ``account.tax`` row. Every subsequent
``product.template`` / ``product.product`` create then blows up while Odoo
precomputes ``taxes_id``:

    MissingError: Record does not exist or has been deleted. (Record: account.tax(5,))

QA and production both run a real chart of accounts, so the taxes persist there
and this only bites the minimal CI database.

The fix re-establishes a live WV sales-tax binding **for the test's current
company** inside the test transaction, then repoints that company's default at
it. We scope to a single company on purpose: in this chartless DB Odoo enforces
``account.tax`` name-uniqueness *globally* (the taxes have no ``country_id`` to
disambiguate them per-company), so the module's own multi-company
``setup_wv_sales_tax`` can only ever bind the first company it iterates and
skips the rest — including the main company the tests run against. Binding just
the current company sidesteps that collision and gives product creation a live
tax to resolve. The install-time purge is a module-loading step that does not
run during a test transaction, so the freshly-created tax survives the class.
"""


class GroveTaxFixtureMixin:
    """Re-establish a live WV sales-tax default so ``product.*`` creates resolve.

    Mix in *before* the concrete Odoo test base so this ``setUpClass`` runs
    first and delegates to it via ``super()``:

        class TestFoo(GroveTaxFixtureMixin, TransactionCase):
            ...
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Imported lazily so importing this fixture module never pulls the
        # install hooks in at test-collection time.
        from odoo.addons.grove_headless.hooks import _ensure_company_wv_taxes

        company = cls.env.company
        group = _ensure_company_wv_taxes(cls.env, company)
        # Authoritative default for new products in this company (the source of
        # the dangling id before the fix).
        cls.env["ir.default"].set("product.template", "taxes_id", group.ids, company_id=company.id)
        try:
            company.account_sale_tax_id = group.id
        except Exception:
            # Some builds restrict this field's domain to non-group taxes; the
            # ir.default above still governs product creation, so ignore.
            pass
