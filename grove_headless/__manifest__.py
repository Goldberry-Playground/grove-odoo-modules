{
    "name": "Grove Headless API",
    "version": "19.0.1.1.0",
    "category": "Website",
    "summary": "JSON API endpoints for headless storefronts in the Grove ecosystem",
    "description": """
        Exposes clean REST-style JSON endpoints for product catalog, cart
        management, and health checks. Designed for multi-tenant usage with
        per-website / per-company isolation so each Grove brand (Goldberry,
        George GGG, Nursery) gets its own scoped data through the same API
        surface.
    """,
    "author": "Gathering at the Grove",
    "website": "https://goldberrygrove.farm",
    "license": "LGPL-3",
    "depends": [
        "base",
        "account",
        "website_sale",
        "website",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/grove_companies.xml",
        "data/grove_product_categories.xml",
        "data/grove_product_attributes.xml",
        "data/grove_taxes.xml",
        "views/product_template_views.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
