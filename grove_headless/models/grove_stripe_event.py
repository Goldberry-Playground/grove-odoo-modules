from odoo import fields, models


class GroveStripeEvent(models.Model):
    """One row per Stripe webhook event we have accepted.

    The webhook is made idempotent by inserting the Stripe event id here under a
    UNIQUE constraint before doing any side effect: a duplicate delivery (Stripe
    retries until it sees a 2xx, and can deliver the same event more than once)
    hits the constraint and is answered 200 without re-running the handler. This
    is the durable dedupe key — never trust the network to deliver exactly once.
    """

    _name = "grove.stripe.event"
    _description = "Processed Stripe Webhook Event (idempotency ledger)"
    _log_access = True

    event_id = fields.Char(required=True, index=True, help="Stripe event id, e.g. evt_...")
    event_type = fields.Char(help="Stripe event type, e.g. checkout.session.completed")
    order_id = fields.Many2one("sale.order", ondelete="set null", help="Order this event was reconciled to, if any")
    notes = fields.Text(help="Outcome of processing (confirmed / refunded oversell / expired / ...)")

    # Odoo 19 dropped support for the `_sql_constraints` list attribute (it warns
    # and never creates the constraint). This is the idempotency guard for the
    # Stripe webhook — without the DB-level UNIQUE, a duplicate delivery could be
    # processed twice (double confirm / double oversell reconcile). Declared with
    # the modern `models.Constraint` so Odoo creates it on `-u grove_headless`.
    _event_id_uniq = models.Constraint(
        "unique(event_id)",
        "This Stripe event has already been recorded.",
    )
