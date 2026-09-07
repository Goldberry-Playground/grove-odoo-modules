"""GOL-2134: the 19.0.1.31.0 pre-migrate re-links severed data-file xmlids.

Simulates the Sep-2 severance (delete the ``ir.model.data`` row while leaving the
business record live) and asserts the pre-migrate stitches the xmlid back onto the
*same* record instead of leaving the loader to create a duplicate — the exact
failure that made prod un-deployable.

Assertions go through ir.model.data DB searches rather than ``env.ref`` so they
are unaffected by the xmlid ormcache after an unlink.
"""

import importlib.util
import os

from odoo.addons.grove_headless.tests.common import GroveTaxFixtureMixin
from odoo.tests import TransactionCase, tagged


def _load_premigrate():
    path = os.path.join(os.path.dirname(__file__), "..", "migrations", "19.0.1.31.0", "pre-migrate.py")
    spec = importlib.util.spec_from_file_location("gol2134_premigrate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@tagged("post_install", "-at_install")
class TestRelinkSeveredXmlids(GroveTaxFixtureMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.premigrate = _load_premigrate()
        self.IMD = self.env["ir.model.data"]
        self._ensure_tax_xmlids()

    def _ensure_tax_xmlids(self):
        """Build the healthy pre-severance tax state deterministically.

        The chartless install-smoke-test DB cascade-deletes the WV account.tax
        rows (and, with them, their ir.model.data xmlids) right after "Modules
        loaded" — see GroveTaxFixtureMixin's docstring — so these tests cannot
        rely on install having left `grove_headless.tax_wv_state_6` behind.
        GroveTaxFixtureMixin re-provisions the taxes in-transaction; here we
        pin them to base.main_company and give BOTH component taxes their
        data-file xmlids (find-or-create, so a chart-ful DB where the rows
        survived install is untouched). Both must be linked, not just the one
        `test_relinks_severed_tax_without_duplicating` severs: the mixin's
        taxes are otherwise orphans matching the pre-migrate's natural keys,
        and `test_noop_when_nothing_severed` would see migrate() "helpfully"
        relink them and fail its no-change assertion.
        """
        from odoo.addons.grove_headless.hooks import _ensure_company_wv_taxes

        company = self.env.ref("base.main_company")
        _ensure_company_wv_taxes(self.env, company)
        for xmlid_name, tax_name in (
            ("tax_wv_state_6", "WV State Sales Tax 6%"),
            ("tax_wv_municipal_1", "WV Municipal Tax 1%"),
        ):
            if self._row(xmlid_name):
                continue
            tax = self.env["account.tax"].search(
                [
                    ("name", "=", tax_name),
                    ("company_id", "=", company.id),
                    ("type_tax_use", "=", "sale"),
                ],
                limit=1,
            )
            self.assertTrue(tax, f"fixture: {tax_name} must exist for base.main_company")
            self.IMD.create(
                {
                    "module": "grove_headless",
                    "name": xmlid_name,
                    "model": "account.tax",
                    "res_id": tax.id,
                    "noupdate": True,
                }
            )

    def _row(self, name):
        return self.IMD.search([("module", "=", "grove_headless"), ("name", "=", name)])

    def _sever(self, name):
        """Delete the ir.model.data row for grove_headless.<name>; return its res_id."""
        row = self._row(name)
        self.assertTrue(row, f"fixture: grove_headless.{name} should exist pre-severance")
        res_id = row.res_id
        row.unlink()
        self.assertFalse(self._row(name), "row should be gone after severance")
        return res_id

    def test_relinks_severed_tax_without_duplicating(self):
        original = self._sever("tax_wv_state_6")
        Tax = self.env["account.tax"].with_context(active_test=False)
        before = Tax.search_count([("name", "=", "WV State Sales Tax 6%")])

        self.premigrate.migrate(self.cr, "19.0.1.29.0")

        row = self._row("tax_wv_state_6")
        self.assertTrue(row, "xmlid should be relinked")
        self.assertEqual(row.res_id, original, "relinked to the original tax, not a new one")
        self.assertEqual(row.model, "account.tax")
        self.assertTrue(row.noupdate, "relinked row must keep noupdate=1 to match the data file")
        self.assertEqual(
            Tax.search_count([("name", "=", "WV State Sales Tax 6%")]),
            before,
            "no duplicate account.tax created",
        )

    def test_relinks_attribute_value_by_attribute(self):
        # 'Bare Root' exists under BOTH Size and Container — the domain must
        # disambiguate by attribute_id or it would link the wrong value.
        original = self._sever("attr_size_bare_root")
        size_attr = self.env.ref("grove_headless.attr_size")

        self.premigrate.migrate(self.cr, "19.0.1.29.0")

        row = self._row("attr_size_bare_root")
        self.assertTrue(row, "attribute value xmlid should be relinked")
        self.assertEqual(row.res_id, original, "relinked to the original Size/Bare Root value")
        value = self.env["product.attribute.value"].browse(row.res_id)
        self.assertEqual(value.attribute_id, size_attr, "must link the Size value, not Container")

    def test_noop_when_nothing_severed(self):
        before = set(self._row_ids())
        self.premigrate.migrate(self.cr, "19.0.1.29.0")
        self.assertEqual(set(self._row_ids()), before, "healthy DB: no xmlid rows added or removed")

    def _row_ids(self):
        return self.IMD.search([("module", "=", "grove_headless")]).ids
