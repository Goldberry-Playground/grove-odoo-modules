from odoo import fields, models, tools

# Sign convention for every source: income credits and expense debits are positive,
# so refunds come out negative.
_KIND = "CASE WHEN a.account_type IN ('income', 'income_other') THEN 'income' ELSE 'expense' END"
_SIGN = "CASE WHEN a.account_type IN ('income', 'income_other') THEN -1 ELSE 1 END"
_LINE_FILTER = """
    m.state = 'posted'
    AND (l.display_type IS NULL OR l.display_type = 'product')
    AND a.grove_schedule_f_line IS NOT NULL
"""


class ScheduleFCashLine(models.Model):
    _name = "grove.schedulef.cashline"
    _description = "Schedule F cash line"
    _auto = False
    _order = "cash_date, id"

    company_id = fields.Many2one("res.company", readonly=True)
    account_id = fields.Many2one("account.account", readonly=True)
    schedule_f_line = fields.Char(string="Schedule F line", readonly=True)
    enterprise = fields.Char(readonly=True)
    kind = fields.Char(readonly=True)
    cash_date = fields.Date(readonly=True)
    amount = fields.Float(readonly=True)
    source = fields.Char(readonly=True)
    move_id = fields.Many2one("account.move", readonly=True)
    move_line_id = fields.Many2one("account.move.line", readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(
            f"""
            CREATE OR REPLACE VIEW {self._table} AS (
                SELECT row_number() OVER (ORDER BY u.source, u.move_line_id, u.partial_id) AS id,
                       u.company_id, u.account_id, u.schedule_f_line, u.enterprise, u.kind,
                       u.cash_date, u.amount, u.source, u.move_id, u.move_line_id
                FROM (
                    -- bank and cash journals count at the bank date
                    SELECT l.company_id, l.account_id, a.grove_schedule_f_line AS schedule_f_line,
                           l.grove_enterprise AS enterprise, {_KIND} AS kind,
                           m.date AS cash_date, {_SIGN} * l.balance AS amount,
                           'bank' AS source, m.id AS move_id, l.id AS move_line_id, 0 AS partial_id
                    FROM account_move_line l
                    JOIN account_move m ON m.id = l.move_id
                    JOIN account_journal j ON j.id = m.journal_id
                    JOIN account_account a ON a.id = l.account_id
                    WHERE {_LINE_FILTER} AND j.type IN ('bank', 'cash')

                    UNION ALL

                    -- intercompany mirror journals count at the entry date
                    SELECT l.company_id, l.account_id, a.grove_schedule_f_line,
                           l.grove_enterprise, {_KIND},
                           m.date, {_SIGN} * l.balance,
                           'mirror', m.id, l.id, 0
                    FROM account_move_line l
                    JOIN account_move m ON m.id = l.move_id
                    JOIN account_journal j ON j.id = m.journal_id
                    JOIN account_account a ON a.id = l.account_id
                    WHERE {_LINE_FILTER} AND j.grove_is_mirror IS TRUE AND j.type NOT IN ('bank', 'cash')
                      AND m.move_type = 'entry'

                    UNION ALL

                    -- invoices and bills count when paid, split across partial payments
                    SELECT l.company_id, l.account_id, a.grove_schedule_f_line,
                           l.grove_enterprise, {_KIND},
                           pm.date,
                           {_SIGN} * l.balance * p.amount / NULLIF(ABS(m.amount_total_signed), 0),
                           'document', m.id, l.id, p.id
                    FROM account_move_line l
                    JOIN account_move m ON m.id = l.move_id
                    JOIN account_account a ON a.id = l.account_id
                    JOIN account_move_line r ON r.move_id = m.id
                    JOIN account_account ra ON ra.id = r.account_id
                         AND ra.account_type IN ('asset_receivable', 'liability_payable')
                    JOIN account_partial_reconcile p ON p.debit_move_id = r.id OR p.credit_move_id = r.id
                    JOIN account_move_line ol
                         ON ol.id = CASE WHEN p.debit_move_id = r.id THEN p.credit_move_id ELSE p.debit_move_id END
                    JOIN account_move pm ON pm.id = ol.move_id
                    WHERE {_LINE_FILTER}
                      AND m.move_type IN ('in_invoice', 'in_refund', 'out_invoice', 'out_refund')
                      -- credit-note offsets carry no cash; only payment partials do
                      AND pm.move_type NOT IN ('in_invoice', 'in_refund', 'out_invoice', 'out_refund')
                ) u
            )
            """
        )
