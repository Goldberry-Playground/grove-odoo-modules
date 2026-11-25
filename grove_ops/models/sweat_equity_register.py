# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Sweat-equity register (Task 7, GOL-4018, s11.4).

Approved hours by person, by company, by year, valued at the co-op's uniform
sweat-equity rate of $21/hour. Every member can read it (grant to
base.group_user in ir.model.access.csv). It is a read-only SQL view over the
approved timesheet lines — nothing posts to the general ledger.

"Approved" means the line's week sheet (OCA ``hr_timesheet_sheet``) is in the
``done`` state; the view joins through ``account_analytic_line.sheet_id``, the
field that module adds. Platform hours (Josh's app-build time) land on the
Platform analytic account under the farm company and show here as a farm-company
contribution, per s11.4.
"""

from odoo import fields, models, tools

# The co-op's uniform sweat-equity rate. The same value is pinned, for the
# bootstrap script's side, in scripts/setup_ops.py (SWEAT_EQUITY_RATE); the two
# live in different runtimes (XML-RPC script vs. installed module) and each is
# guarded by a pure test so they cannot silently drift apart.
GROVE_SWEAT_EQUITY_RATE = 21.00


class GroveSweatEquityRegister(models.Model):
    _name = "grove.sweat.equity.register"
    _description = "Sweat-equity register: approved hours by person, company, year"
    _auto = False
    _order = "year desc, employee_id, company_id"

    employee_id = fields.Many2one("hr.employee", string="Person", readonly=True)
    company_id = fields.Many2one("res.company", string="Company", readonly=True)
    year = fields.Integer(string="Year", readonly=True)
    hours = fields.Float(string="Approved hours", digits=(16, 2), readonly=True)
    rate = fields.Float(string="Rate ($/h)", digits=(16, 2), readonly=True)
    value = fields.Monetary(string="Contribution value", readonly=True, currency_field="currency_id")
    currency_id = fields.Many2one("res.currency", string="Currency", readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(
            """
            CREATE VIEW {table} AS (
                SELECT
                    MIN(aal.id)                        AS id,
                    aal.employee_id                    AS employee_id,
                    aal.company_id                     AS company_id,
                    EXTRACT(YEAR FROM aal.date)::int   AS year,
                    SUM(aal.unit_amount)               AS hours,
                    %(rate)s::numeric                  AS rate,
                    SUM(aal.unit_amount) * %(rate)s    AS value,
                    rc.currency_id                     AS currency_id
                FROM account_analytic_line aal
                JOIN hr_timesheet_sheet s
                    ON s.id = aal.sheet_id AND s.state = 'done'
                JOIN res_company rc
                    ON rc.id = aal.company_id
                WHERE aal.project_id IS NOT NULL
                  AND aal.employee_id IS NOT NULL
                GROUP BY
                    aal.employee_id,
                    aal.company_id,
                    EXTRACT(YEAR FROM aal.date),
                    rc.currency_id
            )
            """.format(table=self._table),
            {"rate": GROVE_SWEAT_EQUITY_RATE},
        )
