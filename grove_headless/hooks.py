"""Install/upgrade hooks for grove_headless.

WV sales tax binding
====================
`data/grove_taxes.xml` historically *created* the WV state 6% + municipal 1%
tax records, but only for ``base.main_company`` and never bound them as the
default applied to sale orders. As a result orders fell back to the Chart of
Accounts default (15%) and the nursery / GGG companies had no WV tax at all.

This hook fixes the *binding* (the part XML data files cannot express):

1. Ensures the two component taxes + a combined "WV Sales Tax 7%" group tax
   exist **per company** (taxes are company-scoped in Odoo).
2. Sets each company's default sale tax (``ir.default`` on
   ``product.template.taxes_id`` — the authoritative default used when new
   products are created, including via the website/UI — plus a best-effort
   ``res.company.account_sale_tax_id``).
3. Retrofits existing sale-able products that still carry the wrong default
   tax so live orders start charging 7% immediately.

It is idempotent: re-running finds existing records by name + company and only
fills in what is missing. The same entry point is called from the
post-migration script so an ``-u grove_headless`` upgrade fixes an already
installed database.
"""

import logging

_logger = logging.getLogger(__name__)

WV_STATE_NAME = "WV State Sales Tax 6%"
WV_MUNI_NAME = "WV Municipal Tax 1%"
WV_GROUP_NAME = "WV Sales Tax 7%"


def _ensure_company_wv_taxes(env, company):
    """Find-or-create the WV component + group taxes for one company.

    Returns the combined group tax (amount_type='group') whose children are
    the 6% state and 1% municipal component taxes, so invoices keep the
    state/municipal split needed for WV quarterly filing.
    """
    Tax = env["account.tax"].with_company(company)

    def _find(name, amount_type):
        return Tax.search(
            [
                ("name", "=", name),
                ("company_id", "=", company.id),
                ("type_tax_use", "=", "sale"),
                ("amount_type", "=", amount_type),
            ],
            limit=1,
        )

    state = _find(WV_STATE_NAME, "percent")
    if not state:
        state = Tax.create(
            {
                "name": WV_STATE_NAME,
                "amount": 6.0,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "company_id": company.id,
                "description": "WV 6%",
            }
        )

    muni = _find(WV_MUNI_NAME, "percent")
    if not muni:
        muni = Tax.create(
            {
                "name": WV_MUNI_NAME,
                "amount": 1.0,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "company_id": company.id,
                "description": "Muni 1%",
            }
        )

    components = state | muni
    group = _find(WV_GROUP_NAME, "group")
    if not group:
        group = Tax.create(
            {
                "name": WV_GROUP_NAME,
                "amount_type": "group",
                "type_tax_use": "sale",
                "company_id": company.id,
                "description": "WV 7%",
                "children_tax_ids": [(6, 0, components.ids)],
            }
        )
    elif set(group.children_tax_ids.ids) != set(components.ids):
        group.children_tax_ids = [(6, 0, components.ids)]

    return group


def _retrofit_products(env, company, group):
    """Replace the wrong default sale tax on existing products with the WV group.

    Scope is deliberately conservative: only sale-able product templates that
    belong to this company (or are company-shared) and do **not** already carry
    the WV group tax. Any existing *sale* taxes are swapped for the group;
    purchase taxes are untouched. Logged so the change is auditable.
    """
    Template = env["product.template"].with_company(company)
    templates = Template.search(
        [
            ("sale_ok", "=", True),
            ("company_id", "in", [company.id, False]),
        ]
    )
    changed = 0
    for tmpl in templates:
        sale_taxes = tmpl.taxes_id
        if group in sale_taxes and len(sale_taxes) == 1:
            continue  # already correct
        # Keep any taxes scoped to *other* companies untouched; only replace
        # the taxes that apply in this company's context.
        foreign = sale_taxes.filtered(lambda t: t.company_id != company)
        tmpl.taxes_id = [(6, 0, (foreign | group).ids)]
        changed += 1
    if changed:
        _logger.info(
            "grove_headless: retrofitted WV sales tax onto %s product(s) for company %s",
            changed,
            company.name,
        )


def setup_wv_sales_tax(env):
    """Ensure every company charges WV 6% + 1% sales tax by default."""
    companies = env["res.company"].search([])
    for company in companies:
        try:
            group = _ensure_company_wv_taxes(env, company)
        except Exception as exc:  # never let tax setup abort install/upgrade
            _logger.warning(
                "grove_headless: skipped WV tax setup for company %s: %s",
                company.name,
                exc,
            )
            continue

        # Authoritative default for new products (UI + website + API).
        env["ir.default"].set(
            "product.template",
            "taxes_id",
            group.ids,
            company_id=company.id,
        )

        # Best-effort: the single-valued company default. Some Odoo builds
        # restrict this field's domain to non-group taxes; if so the ir.default
        # above still governs product creation, so we just log and move on.
        try:
            company.account_sale_tax_id = group.id
        except Exception as exc:
            _logger.info(
                "grove_headless: could not set account_sale_tax_id for %s (%s); "
                "ir.default taxes_id still applies",
                company.name,
                exc,
            )

        _retrofit_products(env, company, group)

    _logger.info("grove_headless: WV sales tax binding ensured for %s companies", len(companies))


def post_init_hook(env):
    """Run on fresh install of grove_headless."""
    setup_wv_sales_tax(env)
