{
    "name": "Grove Headless API",
    "version": "19.0.1.89.0",
    "category": "Website",
    "summary": "JSON API endpoints for headless storefronts in the Grove ecosystem",
    "description": """
        Exposes clean REST-style JSON endpoints for product catalog, cart
        management, and health checks. Designed for multi-tenant usage with
        per-website / per-company isolation so each Grove brand (Goldberry,
        George GGG, Nursery) gets its own scoped data through the same API
        surface.

        Also pulls in `mrp` (Manufacturing) so kit BOMs work for bundled
        nursery products like starter crates — see scripts/seed_kit_boms.py.
    """,
    "author": "Gathering at the Grove",
    "website": "https://goldberrygrove.farm",
    "license": "LGPL-3",
    "depends": [
        "base",
        "account",
        "website_sale",
        "website",
        # sale_loyalty provides the promo/coupon engine (loyalty.program,
        # sale.order._try_apply_code, the reward-line fields on
        # sale.order.line). The headless checkout applies storefront promo
        # codes (e.g. FLATWOODS) through it — see controllers/main.py
        # _apply_promo_code (GOL-2088). Already present on prod via
        # website_sale_loyalty; listed explicitly because we call it directly.
        "sale_loyalty",
        # crm: storefront consult / wholesale / farm-visit / state-waitlist
        # requests become crm.lead records (2026-10-15 capture hotfix).
        "crm",
        # website_sale_stock: back-in-stock alerts ride its
        # product.product.stock_notification_partner_ids + hourly cron, which
        # grove_headless overrides for the headless multi-company storefronts.
        "website_sale_stock",
        # mrp provides mrp.bom (Bills of Materials), required for Kit-type
        # BOMs that bundle multiple variants behind one storefront line item,
        # and for the variant→variant transformation in potting-up batches.
        "mrp",
        # stock provides stock.scrap + the warehouse menu we hang under.
        # Transitive via sale/mrp but listed explicitly because we use it
        # directly (stock.scrap.create, stock.group_stock_user ACLs).
        "stock",
        # sale_stock provides stock.picking.sale_id, which the fulfilment
        # mirror (models/stock_picking.py) relates through. It auto-installs
        # alongside sale+stock, but the explicit dep guarantees load order.
        "sale_stock",
        # point_of_sale provides pos.config / pos.payment.method, used by the
        # post_init hook to stand up the Farmer's Market + Nursery Counter
        # in-person sales channels (GOL-13). See hooks.setup_pos_configs.
        "point_of_sale",
        # pos_sale provides pos.config.crm_team_id — the field the POS hook
        # writes to link each in-person channel to its sales team (GOL-13).
        # Without this dependency that field is absent on a minimal install and
        # _setup_company_pos aborts (silently skipping ALL POS wiring), which is
        # exactly what a chart-less CI DB exposed (GOL-2014).
        "pos_sale",
        # mass_mailing makes Odoo Email Marketing the list of record for every
        # Grove newsletter (Ghost is blog-only) — Josh "option C", 2026-10-19,
        # GOL-3454. Provides mailing.list / mailing.contact /
        # mailing.subscription and mail.blacklist, which the newsletter
        # subscribe/unsubscribe/preferences routes read and write. Adding it
        # here auto-installs it on the next `-u grove_headless`.
        "mass_mailing",
    ],
    "data": [
        "security/grove_groups.xml",
        "security/ir.model.access.csv",
        "security/grove_security_rules.xml",
        "data/grove_companies.xml",
        "data/grove_product_categories.xml",
        "data/grove_product_attributes.xml",
        "data/grove_taxes.xml",
        "data/grove_sequences.xml",
        "data/grove_farm_pickup.xml",
        "data/grove_config_params.xml",
        "data/consult_product.xml",
        "views/product_template_views.xml",
        "views/potting_batch_views.xml",
        "views/grove_publish_event_views.xml",
        "views/grove_email_log_views.xml",
        "views/fulfillment_views.xml",
        "views/grove_zone_views.xml",
        "report/bundle_substitution_report.xml",
        "views/label_batch_views.xml",
        "views/hand_label_views.xml",
        "views/label_reconcile_views.xml",
        "data/shipping_actions.xml",
        "data/settlement_cron.xml",
        "data/order_rollup_cron.xml",
        "data/listing_audit_cron.xml",
        "data/carrier_tracking_cron.xml",
        "data/enrich_job_cron.xml",
        "data/grove_dormancy_params.xml",
        "data/grove_zone_filters.xml",
        "data/restock_email_templates.xml",
        "data/mailing_lists.xml",
        "data/grove_seed_filters.xml",
        "data/printify_actions.xml",
        "data/printify_cron.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
    # Binds the WV sales tax to company defaults + existing products, and
    # stands up the in-person POS configs (Farmer's Market / Nursery Counter),
    # on fresh install. The migrations/ scripts do the same on -u upgrade.
    "post_init_hook": "post_init_hook",
}
