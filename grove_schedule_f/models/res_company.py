from odoo import Command, fields, models
from odoo.addons.grove_schedule_f import schedule_f_lines
from odoo.exceptions import UserError

CATCH_ALL_NAMES = ("Product Sales", "Expenses")
SALE_JOURNAL_ACCOUNT = "4020"
PURCHASE_JOURNAL_ACCOUNT = "6320"


class ResCompany(models.Model):
    _inherit = "res.company"

    grove_owns_shared_bank = fields.Boolean(string="Owns the shared bank", default=False)

    def _grove_ensure_enterprise_plan(self):
        """Find or create the 'Enterprise' analytic plan and this company's Farm/Nursery/Personal accounts.

        Odoo 19 analytic plans carry no company, so the plan is shared by name; the accounts are per company.
        """
        self.ensure_one()
        Plan = self.env["account.analytic.plan"]
        plan = Plan.search([("name", "=", "Enterprise")], limit=1)
        if not plan:
            plan = Plan.with_company(self).create({"name": "Enterprise", "default_applicability": "optional"})
        Account = self.env["account.analytic.account"].with_context(active_test=False)
        existing = set(Account.search([("plan_id", "=", plan.id), ("company_id", "=", self.id)]).mapped("name"))
        for name in ("Farm", "Nursery", "Personal"):
            if name not in existing:
                self.env["account.analytic.account"].create({"name": name, "plan_id": plan.id, "company_id": self.id})
        return plan

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
        """Accounts that must not be archived because something live still points at them.

        Journal defaults and suspense accounts, payment-method outstanding accounts, company properties, product
        category and product template income/expense accounts, and fiscal-position account mappings.
        """
        self.ensure_one()
        journals = self.env["account.journal"].search([("company_id", "=", self.id)])
        accounts = journals.default_account_id | journals.suspense_account_id
        method_lines = journals.inbound_payment_method_line_ids | journals.outbound_payment_method_line_ids
        accounts |= method_lines.payment_account_id
        for fname in (
            "account_journal_suspense_account_id",
            "account_journal_payment_debit_account_id",
            "account_journal_payment_credit_account_id",
            "transfer_account_id",
            "income_currency_exchange_account_id",
            "expense_currency_exchange_account_id",
            "account_journal_early_pay_discount_gain_account_id",
            "account_journal_early_pay_discount_loss_account_id",
            "default_cash_difference_income_account_id",
            "default_cash_difference_expense_account_id",
            "income_account_id",
            "expense_account_id",
        ):
            if fname in self._fields:
                accounts |= self[fname]
        for category in self.env["product.category"].with_company(self).search([]):
            accounts |= category.property_account_income_categ_id | category.property_account_expense_categ_id
        templates = self.env["product.template"].with_company(self).with_context(active_test=False).search([])
        accounts |= templates.property_account_income_id | templates.property_account_expense_id
        mappings = self.env["account.fiscal.position.account"].search([("position_id.company_id", "=", self.id)])
        accounts |= mappings.account_src_id | mappings.account_dest_id
        return set(accounts.ids)

    def _grove_repoint_company_defaults(self):
        """Point the company's default income/expense accounts at 4020/6320 unless already a Schedule F account.

        Chart templates set them to the generic catch-alls, which would otherwise keep those accounts protected.
        Returns the number of fields changed.
        """
        self.ensure_one()
        mapped_codes = {spec["code"] for spec in schedule_f_lines.ACCOUNTS}
        vals = {}
        targets = (("income_account_id", SALE_JOURNAL_ACCOUNT), ("expense_account_id", PURCHASE_JOURNAL_ACCOUNT))
        for fname, code in targets:
            if fname in self._fields and self[fname].code not in mapped_codes:
                vals[fname] = self._grove_account(code).id
        if vals:
            self.write(vals)
        return len(vals)

    def _grove_repoint_categories(self):
        """Point product-category income/expense defaults at 4020/6320 unless already a Schedule F account.

        Returns the number of categories changed.
        """
        self.ensure_one()
        mapped_codes = {spec["code"] for spec in schedule_f_lines.ACCOUNTS}
        targets = {
            "property_account_income_categ_id": self._grove_account(SALE_JOURNAL_ACCOUNT),
            "property_account_expense_categ_id": self._grove_account(PURCHASE_JOURNAL_ACCOUNT),
        }
        changed = 0
        for category in self.env["product.category"].with_company(self).search([]):
            vals = {fname: target.id for fname, target in targets.items() if category[fname].code not in mapped_codes}
            if vals:
                category.write(vals)
                changed += 1
        return changed

    def _grove_ensure_mirror_journal(self):
        """Find or create this company's intercompany mirror journal (code MIRR). Returns True when created."""
        self.ensure_one()
        Journal = self.env["account.journal"]
        journal = Journal.search([("company_id", "=", self.id), ("code", "=", "MIRR")], limit=1)
        if journal:
            if not journal.grove_is_mirror:
                journal.grove_is_mirror = True
            return False
        Journal.with_company(self).create(
            {
                "name": "Intercompany mirror",
                "code": "MIRR",
                "type": "general",
                "company_id": self.id,
                "grove_is_mirror": True,
            }
        )
        return True

    def grove_schedule_f_setup(self):
        """Create the Schedule F accounts and tags for this company (idempotent).

        Returns a dict with accounts_created, accounts_existing, tags_created, deprecated, categories_repointed,
        plan_created, mirror_journal_created.
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

        self._grove_repoint_company_defaults()
        categories_repointed = self._grove_repoint_categories()

        protected = self._grove_protected_account_ids()
        catch_alls = Account.search(
            [("company_ids", "in", self.id), ("name", "in", list(CATCH_ALL_NAMES)), ("active", "=", True)]
        ).filtered(lambda a: a.id not in protected)
        catch_alls.write({"active": False})  # Odoo 19 has no `deprecated`; archiving is its equivalent
        plan_created = False
        if self.grove_owns_shared_bank:
            plan_created = not self.env["account.analytic.plan"].search_count([("name", "=", "Enterprise")])
            self._grove_ensure_enterprise_plan()
        mirror_journal_created = self._grove_ensure_mirror_journal()
        return {
            "mirror_journal_created": mirror_journal_created,
            "plan_created": plan_created,
            "accounts_created": created,
            "accounts_existing": existing,
            "tags_created": len(created_tags),
            "deprecated": sorted(catch_alls.mapped("code")),
            "categories_repointed": categories_repointed,
        }
