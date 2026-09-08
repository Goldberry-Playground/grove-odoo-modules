"""Re-link grove_headless data-file external IDs that were severed (GOL-2134).

## The incident

Prod became un-deployable on 2026-09-02: any ``--update=grove_headless`` crashes
at load time with::

    ParseError data/grove_taxes.xml:26 -- Tax names must be unique! WV State Sales Tax 6%

Root cause: ``ir.model.data`` holds **no** external-ID rows for the grove_headless
data-file records (the ``account.tax`` records still exist as live rows, but their
``grove_headless.tax_wv_state_6`` / ``tax_wv_municipal_1`` xmlids are gone --
almost certainly deleted by the Sep-2 GOL-1903 inventory port). When the XML
loader can't find the xmlid it tries to *create* a fresh ``account.tax`` with the
same name and trips the DB uniqueness constraint. For product.category /
product.attribute[.value] the same severance would instead silently **duplicate**
the records before the load reached the taxes and hard-failed.

## The fix

Migrations run in this order for a module update: **all** pre-migrate scripts for
pending versions -> module data load -> post-migrate scripts. So this pre-migrate
runs *before* ``load_data`` and stitches the missing ``ir.model.data`` rows back
onto the orphaned records by their natural key (name, plus parent/attribute/company
to disambiguate). Once the xmlid exists, the subsequent data load *updates* the
existing record instead of creating a duplicate, and the upgrade boots clean.

Safety properties:
  * **Idempotent** -- if the xmlid row already exists we skip it, so a re-run (or a
    prod that was never severed) is a no-op.
  * **Never creates business records** -- it only ever inserts an ir.model.data
    row pointing at a record that already exists. If no orphan matches (a genuinely
    fresh record) we leave it for the normal loader to create.
  * **Never guesses across duplicates** -- if a natural key matches more than one
    row we link the lowest id (the original) and log a warning for manual cleanup;
    we do not delete anything.

See GOL-2134. Root-cause hardening of the port that severs xmlids is tracked
separately so this class of outage cannot recur.
"""

import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

MODULE = "grove_headless"


def _ensure_xmlid(env, name, model, domain, noupdate=True):
    """Ensure ``grove_headless.<name>`` points at the single record matching ``domain``.

    Returns one of "linked" (already present), "relinked" (row created),
    "absent" (no orphan to link -- loader will create it), or "ambiguous".
    """
    IMD = env["ir.model.data"]
    if IMD.search_count([("module", "=", MODULE), ("name", "=", name)]):
        return "linked"

    # active_test=False so an (unexpectedly) archived orphan is still matched.
    matches = env[model].with_context(active_test=False).search(domain, order="id asc")
    if not matches:
        _logger.info("GOL-2134: no %s orphan for %s.%s -- leaving for loader", model, MODULE, name)
        return "absent"

    rec = matches[0]
    status = "relinked"
    if len(matches) > 1:
        status = "ambiguous"
        _logger.warning(
            "GOL-2134: %s.%s matched %d %s rows %s; linking lowest id %d, leaving the rest orphaned for manual cleanup",
            MODULE,
            name,
            len(matches),
            model,
            matches.ids,
            rec.id,
        )

    IMD.create(
        {
            "module": MODULE,
            "name": name,
            "model": model,
            "res_id": rec.id,
            "noupdate": noupdate,
        }
    )
    _logger.info("GOL-2134: relinked %s.%s -> %s#%d", MODULE, name, model, rec.id)
    return status


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    # base.main_company is a base xmlid (not severed by a grove_headless port);
    # used to scope the company-specific tax records precisely.
    main_company = env.ref("base.main_company", raise_if_not_found=False)
    main_company_id = main_company.id if main_company else None

    results = {}

    def link(name, model, domain, noupdate=True):
        results[name] = _ensure_xmlid(env, name, model, domain, noupdate=noupdate)

    # ── Taxes (the record that hard-crashes the load) ───────────────────────
    # data/grove_taxes.xml -- account.tax.group + two account.tax components.
    link("tax_group_wv_sales", "account.tax.group", [("name", "=", "WV Sales Tax")])

    tax_domain = [("type_tax_use", "=", "sale")]
    if main_company_id:
        tax_domain = [("company_id", "=", main_company_id)] + tax_domain
    link("tax_wv_state_6", "account.tax", [("name", "=", "WV State Sales Tax 6%")] + tax_domain)
    link("tax_wv_municipal_1", "account.tax", [("name", "=", "WV Municipal Tax 1%")] + tax_domain)

    # ── Companies + Websites ────────────────────────────────────────────────
    # data/grove_companies.xml -- base.main_company / base.main_partner /
    # website.default_website are base/website xmlids and are NOT severed by a
    # grove_headless port, so only the grove-owned records need relinking.
    link("company_ggg", "res.company", [("name", "=", "George George George Woodworking")])
    link("company_nursery", "res.company", [("name", "=", "At The Grove Nursery")])
    # Websites: DO NOT match on `domain`. Odoo normalizes website.domain on write
    # (the data-file's bare "woodworkingeorge.com" is stored as
    # "https://woodworkingeorge.com"), so an exact-match on the bare value never
    # finds the orphan -> the loader then tries to create a fresh website and
    # trips website_domain_unique, and the whole upgrade aborts (GOL-2192).
    # Relink by the owning company instead: each grove company owns exactly one
    # website (data/grove_companies.xml), so company_id is a unique, stable
    # natural key -- and one we just re-linked immediately above, so env.ref
    # resolves it even when the company xmlid was itself severed.
    for website_xmlid, company_xmlid in (("website_ggg", "company_ggg"), ("website_nursery", "company_nursery")):
        company = env.ref(f"{MODULE}.{company_xmlid}", raise_if_not_found=False)
        if company:
            link(website_xmlid, "website", [("company_id", "=", company.id)])
        else:
            # Company xmlid genuinely absent -> fresh DB; the website is fresh
            # too, so leave both for the normal loader to create.
            results[website_xmlid] = "absent"

    # ── Product categories ──────────────────────────────────────────────────
    # data/grove_product_categories.xml -- link parents first so sub-category
    # domains can resolve parent_id.
    top = {"categ_plants": "Plants", "categ_supplies": "Supplies", "categ_services": "Services"}
    for name, label in top.items():
        link(name, "product.category", [("name", "=", label), ("parent_id", "=", False)])

    plants = env.ref("grove_headless.categ_plants", raise_if_not_found=False)
    if plants:
        sub = {
            "categ_trees": "Trees",
            "categ_shrubs": "Shrubs",
            "categ_hedging": "Hedging",
            "categ_root_stock": "Root Stock",
            "categ_mixed": "Mixed",
        }
        for name, label in sub.items():
            link(name, "product.category", [("name", "=", label), ("parent_id", "=", plants.id)])

    # ── Product attributes + values ─────────────────────────────────────────
    # data/grove_product_attributes.xml -- attribute values disambiguate by
    # attribute_id ("Bare Root" exists under both Size and Container).
    link("attr_size", "product.attribute", [("name", "=", "Size")])
    link("attr_container", "product.attribute", [("name", "=", "Container")])

    attr_values = {
        "attr_size": {
            "attr_size_1gal": "1 gal",
            "attr_size_3gal": "3 gal",
            "attr_size_5gal": "5 gal",
            "attr_size_10gal": "10 gal",
            "attr_size_bare_root": "Bare Root",
        },
        "attr_container": {
            "attr_container_nursery_pot": "Nursery Pot",
            "attr_container_ceramic": "Ceramic Pot",
            "attr_container_burlap": "Burlap Ball",
            "attr_container_bare_root": "Bare Root",
        },
    }
    for attr_xmlid, values in attr_values.items():
        attr = env.ref(f"{MODULE}.{attr_xmlid}", raise_if_not_found=False)
        if not attr:
            _logger.warning("GOL-2134: %s.%s unresolved; skipping its values", MODULE, attr_xmlid)
            continue
        for name, label in values.items():
            link(
                name,
                "product.attribute.value",
                [("name", "=", label), ("attribute_id", "=", attr.id)],
            )

    relinked = sorted(k for k, v in results.items() if v in ("relinked", "ambiguous"))
    _logger.info(
        "GOL-2134 pre-migrate summary: %d relinked, %d already-linked, %d absent. Relinked: %s",
        sum(1 for v in results.values() if v in ("relinked", "ambiguous")),
        sum(1 for v in results.values() if v == "linked"),
        sum(1 for v in results.values() if v == "absent"),
        relinked or "(none -- nothing was severed)",
    )
