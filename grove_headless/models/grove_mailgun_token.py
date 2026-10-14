"""Single-use ledger for Mailgun webhook signature tokens (GOL-2903).

Mailgun's webhook signature is ``HMAC-SHA256(key, "{timestamp}{token}")``. It
does NOT cover the request body, so a captured signature triple could be
re-sent with different ``event-data`` for as long as its timestamp is fresh.
Mailgun's documented defence is to remember each token and refuse a second use.

:meth:`claim` inserts the token under a UNIQUE constraint before the webhook
touches any ``grove.email.log`` row; a repeated token is refused. Rows whose
signed timestamp has aged out of ``mailgun_gateway.SIG_TOLERANCE`` are pruned on
every claim: past that window the freshness check in ``verify_signature``
already rejects the token, so the ledger only ever holds the live window.
"""

import time

import psycopg2
from odoo import api, fields, models

from . import mailgun_gateway


class GroveMailgunToken(models.Model):
    _name = "grove.mailgun.token"
    _description = "Mailgun Webhook Token (replay ledger)"
    _log_access = True

    token = fields.Char(required=True, index=True)
    # The signature's own timestamp (epoch seconds), not the receive time: the
    # freshness window in verify_signature is measured against it, so pruning on
    # it keeps every token that could still pass that check.
    signed_at = fields.Integer(required=True, index=True)

    # Odoo 19 ignores `_sql_constraints`; models.Constraint creates the UNIQUE.
    _token_uniq = models.Constraint("unique(token)", "This Mailgun webhook token has already been used.")

    @api.model
    def prune(self, now=None, tolerance=mailgun_gateway.SIG_TOLERANCE):
        """Delete tokens whose signed timestamp is outside the freshness window."""
        if now is None:
            now = time.time()
        stale = self.sudo().search([("signed_at", "<", int(now - tolerance))])
        stale.unlink()
        return len(stale)

    @api.model
    def claim(self, token, timestamp, now=None):
        """Record ``token`` as used. Returns True on first use, False on a replay.

        Call only after ``verify_signature`` passed (so ``timestamp`` is an int
        inside the window). The INSERT is flushed inside a savepoint so a
        concurrent duplicate collides here, not at commit.
        """
        self.prune(now=now)
        Token = self.sudo()
        if Token.search_count([("token", "=", token)]):
            return False
        try:
            with self.env.cr.savepoint():
                Token.create({"token": token, "signed_at": int(timestamp)})
                self.env.flush_all()
        except psycopg2.IntegrityError:
            return False
        return True
