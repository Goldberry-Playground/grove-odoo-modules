{
    "name": "Grove Headless API",
    "version": "19.0.1.5.0",
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
        # mrp provides mrp.bom (Bills of Materials), required for Kit-type
        # BOMs that bundle multiple variants behind one storefront line item,
        # and for the variant→variant transformation in potting-up batches.
        "mrp",
        # stock provides stock.scrap + the warehouse menu we hang under.
        # Transitive via sale/mrp but listed explicitly because we use it
        # directly (stock.scrap.create, stock.group_stock_user ACLs).
        "stock",
    ],
    "data": [
        "security/ir.model.access.csv",
        "security/grove_security_rules.xml",
        "data/grove_companies.xml",
        "data/grove_product_categories.xml",
        "data/grove_product_attributes.xml",
        "data/grove_taxes.xml",
        "data/grove_sequences.xml",
        "views/product_template_views.xml",
        "views/potting_batch_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
    # Binds the WV sales tax to company defaults + existing products on
    # fresh install. The migrations/ script does the same on -u upgrade.
    "post_init_hook": "post_init_hook",
}
