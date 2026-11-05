import calendar

from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError

INTERCOMPANY_ACCOUNT = "1350"
MIRROR_SOURCES = ("bank", "document")


class MirrorRun(models.Model):
    _name = "grove.mirror.run"
    _description = "Month mirror of nursery-tagged expenses"
    _order = "month desc"

    name = fields.Char(compute="_compute_name")
    month = fields.Date(required=True, help="First day of the mirrored month.")
    source_company_id = fields.Many2one("res.company", required=True)
    target_company_id = fields.Many2one("res.company", required=True)
    state = fields.Selection(
        [("draft", "Draft"), ("posted", "Posted"), ("reversed", "Reversed")], default="draft", required=True
    )
    source_move_id = fields.Many2one("account.move", readonly=True, copy=False)
    target_move_id = fields.Many2one("account.move", readonly=True, copy=False)
    summary = fields.Json(readonly=True, copy=False)
    is_stale = fields.Boolean(readonly=True, copy=False)

    @api.depends("month", "source_company_id", "target_company_id")
    def _compute_name(self):
        for run in self:
            month = run.month.strftime("%Y-%m") if run.month else ""
            run.name = f"{month} {run.source_company_id.name or ''} to {run.target_company_id.name or ''}"

    @api.model
    def _grove_collect(self, month, source_company):
        """Return {account code: debit-positive balance} of the month's nursery-tagged cash rows."""
        first = fields.Date.to_date(month).replace(day=1)
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        self.env.flush_all()  # the cash view is raw SQL: pending move-line writes must reach the database
        rows = (
            self.env["grove.schedulef.cashline"]
            .sudo()
            .search(
                [
                    ("company_id", "=", source_company.id),
                    ("enterprise", "=", "nursery"),
                    ("source", "in", MIRROR_SOURCES),
                    ("cash_date", ">=", first),
                    ("cash_date", "<=", last),
                ]
            )
        )
        totals = {}
        for row in rows:
            code = row.account_id.with_company(source_company).code
            totals[code] = totals.get(code, 0.0) + (row.amount if row.kind == "expense" else -row.amount)
        rounded = {code: round(value, 2) for code, value in totals.items()}
        return {code: value for code, value in rounded.items() if value}

    @api.model
    def _grove_check_access(self, source_company, target_company):
        allowed = self.env.user.company_ids
        if source_company not in allowed or target_company not in allowed:
            raise AccessError("You need access to both the source and the target company to run a mirror.")

    def _grove_validate_accounts(self, collect):
        """Fail with a clear message, before anything is created, if an account is missing on either side."""
        for company in (self.source_company_id, self.target_company_id):
            for code in [*collect, INTERCOMPANY_ACCOUNT]:
                company._grove_account(code)  # raises UserError naming the code and the company
            self._grove_mirror_journal(company)

    def _grove_nursery_analytic(self):
        self.ensure_one()
        account = self.env["account.analytic.account"].search(
            [
                ("name", "=", "Nursery"),
                ("plan_id.name", "=", "Enterprise"),
                ("company_id", "=", self.source_company_id.id),
            ],
            limit=1,
        )
        if not account:
            raise UserError(f"No Nursery enterprise analytic account for {self.source_company_id.name}.")
        return account

    def _grove_mirror_journal(self, company):
        journal = (
            self.env["account.journal"]
            .with_company(company)
            .search([("company_id", "=", company.id), ("code", "=", "MIRR")], limit=1)
        )
        if not journal:
            raise UserError(f"Run Schedule F setup for {company.name}: it has no MIRR journal.")
        return journal

    def _grove_create_move(self, company, collect, date, ref, sign, analytic=None):
        """sign=-1 builds the source move (reverses the balances), +1 the target move."""
        lines = []
        for code in sorted(collect):
            balance = collect[code]
            vals = {
                "account_id": company._grove_account(code).id,
                "name": ref,
                "debit": sign * balance if sign * balance > 0 else 0.0,
                "credit": -sign * balance if sign * balance < 0 else 0.0,
            }
            if analytic:
                vals["analytic_distribution"] = {str(analytic.id): 100}
            lines.append(Command.create(vals))
        total = round(sum(collect.values()), 2)
        lines.append(
            Command.create(
                {
                    "account_id": company._grove_account(INTERCOMPANY_ACCOUNT).id,
                    "name": ref,
                    "debit": -sign * total if -sign * total > 0 else 0.0,
                    "credit": sign * total if sign * total > 0 else 0.0,
                }
            )
        )
        move = (
            self.env["account.move"]
            .with_company(company)
            .create(
                {
                    "move_type": "entry",
                    "journal_id": self._grove_mirror_journal(company).id,
                    "date": date,
                    "ref": ref,
                    "company_id": company.id,
                    "line_ids": lines,
                }
            )
        )
        move.action_post()
        return move

    def _grove_execute(self):
        self.ensure_one()
        collect = self._grove_collect(self.month, self.source_company_id)
        summary = [{"code": code, "amount": collect[code]} for code in sorted(collect)]
        vals = {"state": "posted", "summary": summary, "is_stale": False}
        if collect:
            self._grove_validate_accounts(collect)
            last = self.month.replace(day=calendar.monthrange(self.month.year, self.month.month)[1])
            ref = f"MIRROR {self.month:%Y-%m} nursery"
            analytic = self._grove_nursery_analytic()
            vals["source_move_id"] = self._grove_create_move(
                self.source_company_id, collect, last, ref, sign=-1, analytic=analytic
            ).id
            vals["target_move_id"] = self._grove_create_move(self.target_company_id, collect, last, ref, sign=1).id
        self.write(vals)

    def _grove_check_unique(self, month, source_company, exclude=None):
        domain = [("month", "=", month), ("source_company_id", "=", source_company.id), ("state", "!=", "reversed")]
        if exclude:
            domain.append(("id", "!=", exclude.id))
        if self.search_count(domain):
            raise UserError(f"A mirror for {month:%Y-%m} already exists (reverse it first).")

    @api.model
    def grove_run_month(self, month, source_company, target_company):
        self._grove_check_access(source_company, target_company)
        month = fields.Date.to_date(month).replace(day=1)
        self._grove_check_unique(month, source_company)
        run = self.create(
            {"month": month, "source_company_id": source_company.id, "target_company_id": target_company.id}
        )
        run._grove_execute()
        return run

    def action_run(self):
        for run in self:
            if run.state != "draft":
                raise UserError("Only a draft mirror can be run.")
            self._grove_check_access(run.source_company_id, run.target_company_id)
            run.month = run.month.replace(day=1)
            self._grove_check_unique(run.month, run.source_company_id, exclude=run)
            run._grove_execute()

    def action_reverse(self):
        for run in self:
            if run.state != "posted":
                raise UserError("Only a posted mirror can be reversed.")
            for move in (run.source_move_id | run.target_move_id).filtered(lambda m: not m.reversal_move_ids):
                move.with_company(move.company_id)._reverse_moves(cancel=True)
            run.state = "reversed"

    def action_check(self):
        for run in self:
            current = run._grove_collect(run.month, run.source_company_id)
            booked = {line["code"]: line["amount"] for line in run.summary or []}
            run.is_stale = current != booked
