import base64

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged("grove_schedule_f", "post_install", "-at_install")
class TestImportDedupe(TransactionCase):
    """Re-importing a CSV with a unique_import_id column must not duplicate statement lines."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env["res.company"].create({"name": "Dedupe Co", "country_id": cls.env.ref("base.us").id})
        cls.env["account.chart.template"].try_loading("generic_coa", cls.company, install_demo=False)
        cls.journal = (
            cls.env["account.journal"]
            .with_company(cls.company)
            .create({"name": "Dedupe Bank", "code": "GDDB", "type": "bank", "company_id": cls.company.id})
        )
        # Same column spec as scripts/import_bank_csv.py mapping_vals() (script is not imported).
        cls.mapping = cls.env["account.statement.import.sheet.mapping"].create(
            {
                "name": "Dedupe CSV",
                "float_thousands_sep": "comma",
                "float_decimal_sep": "dot",
                "file_encoding": "utf-8",
                "delimiter": "comma",
                "quotechar": '"',
                "timestamp_format": "%m/%d/%Y",
                "timestamp_column": "Date",
                "amount_type": "simple_value",
                "amount_column": "Amount",
                "description_column": "Description",
                "transaction_id_column": "unique_import_id",
            }
        )

    def _import(self):
        data = (
            "Date,Amount,Description,unique_import_id\n"
            "03/05/2026,-12.50,COFFEE,TX-DEDUPE-1\n"
            "03/06/2026,100.00,DEPOSIT,TX-DEDUPE-2\n"
        ).encode()
        wizard = (
            self.env["account.statement.import"]
            .with_company(self.company)
            .with_context(
                journal_id=self.journal.id,
                active_model="account.journal",
                active_id=self.journal.id,
                allowed_company_ids=[self.company.id],
            )
            .create(
                {
                    "statement_file": base64.b64encode(data),
                    "statement_filename": "bank-2026-03.csv",
                    "sheet_mapping_id": self.mapping.id,
                }
            )
        )
        wizard.import_file_button()

    def _lines(self):
        return self.env["account.bank.statement.line"].search(
            [("journal_id", "=", self.journal.id), ("unique_import_id", "like", "TX-DEDUPE-")]
        )

    def test_reimport_creates_no_duplicates(self):
        self._import()
        self.assertEqual(len(self._lines()), 2)
        # The wizard refuses a file whose transactions are all already imported; nothing is added.
        with self.assertRaises(UserError), self.cr.savepoint():
            self._import()
        self.assertEqual(len(self._lines()), 2)
