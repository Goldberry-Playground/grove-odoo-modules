"""Dedupe idempotency-ledger tables before their UNIQUE constraints are created.

GOL-1010. Odoo 19 dropped the `_sql_constraints` list attribute, so the
UNIQUE(event_id) / UNIQUE(delivery_id) constraints declared on grove.stripe.event
and grove.publish.event were **never created** (registry only warned). We move
those to `models.Constraint`, which Odoo applies during `_auto_init` — but that
apply runs AFTER this pre-migrate and silently no-ops (logs a warning, leaves the
constraint uncreated) if any duplicate rows already exist.

So collapse duplicates first, keeping the earliest row per key (lowest id):
  - grove.stripe.event : one row per Stripe event_id (idempotency guard)
  - grove.publish.event: one row per delivery_id (audit/replay ledger)

Idempotent — safe to re-run; on a clean table it deletes nothing.
"""

import logging

_logger = logging.getLogger(__name__)

# (table, unique column) — the ledger tables whose constraint we are (re)creating.
_DEDUPE = [
    ("grove_stripe_event", "event_id"),
    ("grove_publish_event", "delivery_id"),
]


def _table_exists(cr, table):
    cr.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    return cr.fetchone()[0] is not None


def migrate(cr, version):
    for table, column in _DEDUPE:
        # Table may not exist yet on a partial/fresh install — guard so the
        # upgrade can't hard-fail before the model is even created.
        if not _table_exists(cr, table):
            continue
        # Keep the lowest id per key; drop the rest. NULLs are never equal, so
        # rows with a NULL key (shouldn't happen — both columns are required) are
        # left untouched and remain permitted by a Postgres UNIQUE.
        cr.execute(
            f"""
            DELETE FROM {table} a
            USING {table} b
            WHERE a.{column} = b.{column}
              AND a.id > b.id
            """  # noqa: S608 — table/column are from the fixed _DEDUPE allow-list, not user input
        )
        if cr.rowcount:
            _logger.info(
                "GOL-1010: removed %s duplicate %s row(s) before creating UNIQUE(%s)",
                cr.rowcount,
                table,
                column,
            )
