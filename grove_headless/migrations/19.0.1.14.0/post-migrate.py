"""Initialize the guide-publishing gate (GATH-130).

Adds ``grove_guide_ready`` to product.template. Odoo creates the column on
``-u grove_headless``; a new Boolean column starts NULL, which Odoo already
reads as False, so no product is accidentally "guide approved" on upgrade. We
still normalize NULL -> False in one deterministic pass so the flag is a real
stored boolean (searchable, groupable) rather than a mix of NULL/False — the
storefront guide gate reads this on every detail request.

Idempotent — safe to re-run.
"""


def migrate(cr, version):
    # Column may not exist yet if the field failed to register; guard so the
    # upgrade can't hard-fail on a fresh/partial install.
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'product_template' AND column_name = 'grove_guide_ready'
        """
    )
    if not cr.fetchone():
        return
    cr.execute("UPDATE product_template SET grove_guide_ready = FALSE WHERE grove_guide_ready IS NULL")
