from odoo import Command, models
from odoo.addons.grove_schedule_f import schedule_f_lines
from odoo.exceptions import UserError

CATCH_ALL_NAMES = ("Product Sales", "Expenses")
SALE_JOURNAL_ACCOUNT = "4020"
PURCHASE_JOURNAL_ACCOUNT = "6320"


class ResCompany(models.Model):
    _inherit = "res.company"

    def _grove_account(self, code):
        """Return this company's account with the given code or raise UserError."""
        self.ensure_one()
        account = (
            self.env["account.account"]
            .with_company(self)
            .search([("company_ids", "in", self.id), ("code", "=", code)], limit=1)
        )
        if not account:
            raise UserError(f"Schedule F account {code} does not exist for company {self.name}.")
        return account

    def _grove_schedule_f_tag(self, line, created):
        """Return the shared 'Schedule F {line}' tag, creating it on demand."""
        Tag = self.env["account.account.tag"]
        name = f"Schedule F {line}"
        tag = Tag.search([("name", "=", name), ("applicability", "=", "accounts")], limit=1)
        if not tag:
            tag = Tag.create({"name": name, "applicability": "accounts"})
            created.append(name)
        return tag

    def _grove_protected_account_ids(self):
        """Accounts that must not be archived: journal defaults and company properties."""
        self.ensure_one()
        journals = self.env["account.journal"].search([("company_id", "=", self.id)])
        accounts = journals.default_account_id | journals.suspense_account_id
        for fname in (
            "account_journal_suspense_account_id",
            "account_journal_payment_debit_account_id",
            "account_journal_payment_credit_account_id",
            "transfer_account_id",
        ):
            if fname in self._fields:
                accounts |= self[fname]
        return set(accounts.ids)

    def grove_schedule_f_setup(self):
        """Create the Schedule F accounts and tags for this company (idempotent).

        Returns a dict with accounts_created, accounts_existing, tags_created, deprecated.
        """
        self.ensure_one()
        Account = self.env["account.account"].with_company(self)
        created_tags = []
        created = existing = 0
        for spec in schedule_f_lines.ACCOUNTS:
            line = spec["line"]
            vals = {"name": spec["name"], "grove_schedule_f_line": line or False}
            account = Account.search([("company_ids", "in", self.id), ("code", "=", spec["code"])], limit=1)
            if account:
                existing += 1
                account.write(vals)
            else:
                account = Account.create(
                    {
                        **vals,
                        "code": spec["code"],
                        "account_type": spec["account_type"],
                        "company_ids": [Command.link(self.id)],
                    }
                )
                created += 1
            if line:
                tag = self._grove_schedule_f_tag(line, created_tags)
                if tag not in account.tag_ids:
                    account.tag_ids = [Command.link(tag.id)]

        # Repoint journal defaults before deprecating the generic catch-alls.
        Journal = self.env["account.journal"]
        for jtype, code in (("sale", SALE_JOURNAL_ACCOUNT), ("purchase", PURCHASE_JOURNAL_ACCOUNT)):
            Journal.search([("company_id", "=", self.id), ("type", "=", jtype)]).write(
                {"default_account_id": self._grove_account(code).id}
            )

        protected = self._grove_protected_account_ids()
        catch_alls = Account.search(
            [("company_ids", "in", self.id), ("name", "in", list(CATCH_ALL_NAMES)), ("active", "=", True)]
        ).filtered(lambda a: a.id not in protected)
        catch_alls.write({"active": False})  # Odoo 19 has no `deprecated`; archiving is its equivalent
        return {
            "accounts_created": created,
            "accounts_existing": existing,
            "tags_created": len(created_tags),
            "deprecated": sorted(catch_alls.mapped("code")),
        }
