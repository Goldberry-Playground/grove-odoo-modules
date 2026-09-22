"""Install/upgrade hooks for grove_headless.

WV sales tax binding (GOL-2449)
===============================
`data/grove_taxes.xml` historically *created* the WV state 6% + municipal 1%
tax records, but only for ``base.main_company`` and never bound them as the
default applied to sale orders. As a result orders fell back to the Chart of
Accounts default (Odoo's demo **15%**) and the nursery / GGG companies had no
WV tax at all. A test checkout on prod (2026-09-22) still showed 15% on the
shipping line and on Square-imported products, because the old hook tried to
create a combined ``amount_type='group'`` tax — which fails on Odoo 19 — and
*swallowed* that failure, so the whole binding silently never took effect.

Josh's ruling (2026-09-22, final): the web/POS tax is **6% WV state only**, on
goods AND shipping, for everything shipped to WV; **no 1% municipal** anywhere
on the web path. Grove's sole sales-tax nexus is WV, so any out-of-state
shipment has the tax stripped (see controllers.main._apply_destination_tax).

This hook fixes the *binding* (the part XML data files cannot express):

1. Ensures the 6% state tax exists **per company** (taxes are company-scoped).
   The 1% municipal record is kept for the books but is never bound.
2. Sets each company's default sale tax to the state tax (``ir.default`` on
   ``product.template.taxes_id`` — the authoritative default for new products,
   including via the website/UI — plus ``res.company.account_sale_tax_id``).
3. Retrofits existing sale-able products that carry a tax from ANOTHER company
   (the prod defect: 52 nursery products carried company-1's demo 15%) or the
   demo 15%, replacing it with the company's WV 6% state tax.
4. Ensures the GROVE-SHIP service product carries exactly the state tax.

It is idempotent and logs at WARNING (never swallowed to a no-op) when a
company cannot be bound. The same entry point runs from the post-migration
script so an ``-u grove_headless`` upgrade converges an already installed DB.
"""

import logging

_logger = logging.getLogger(__name__)

WV_STATE_NAME = "WV State Sales Tax 6%"
WV_MUNI_NAME = "WV Municipal Tax 1%"
# Legacy combined 6%+1% group tax. GOL-2449: no longer created or bound — the web
# path is 6% state only. The name is retained so _apply_destination_tax still
# strips any legacy "7%" line left on an old order when it ships out of state.
WV_GROUP_NAME = "WV Sales Tax 7%"
# The WV records we consider "correct" to leave on a product during retrofit.
WV_KEEP_NAMES = frozenset({WV_STATE_NAME, WV_MUNI_NAME})
# The service product the shipping charge rides on (mirrors controllers.main).
SHIPPING_PRODUCT_CODE = "GROVE-SHIP"


def _accessible_companies(company):
    """Return ``company`` plus its ancestor companies (root-inclusive).

    This is the set of companies whose taxes ``company`` may use: ``account.tax``
    declares ``_check_company_domain = check_company_domain_parent_of``, so a tax
    owned by a parent/root company is valid on a branch's products and orders.
    """
    chain = company
    parent = company.parent_id
    while parent:
        chain |= parent
        parent = parent.parent_id
    return chain


def _ensure_company_wv_taxes(env, company):
    """Find-or-reuse the WV **state** 6% sale tax usable by one company, return it.

    GOL-2449: the three Grove businesses are *branch* companies of a single root
    (``base.main_company`` — see ``data/grove_companies.xml``). Odoo 19 scopes
    ``account.tax`` name-uniqueness to the company-hierarchy **root**
    (``account.tax._constrains_name`` searches ``company_id child_of root``), so a
    per-branch ``"WV State Sales Tax 6%"`` cannot be created — it collides with
    the root's record and raises *"Tax names must be unique!"*. That collision is
    exactly why the old hook's per-company ``create`` blew up for the nursery/GGG
    branches, and its swallowed failure left 2 of 3 companies on the demo 15%
    default on prod.

    A branch may *use* a root/ancestor company's tax (check_company is
    ``parent_of``), so we reuse the WV record already accessible to the company
    (its own or an ancestor's) and only create one — on the branch **root**, so
    it satisfies the root-scoped constraint and is shared by every branch — when
    the hierarchy has none (e.g. the chartless CI DB where install purges the
    XML-seeded rows).

    The web/POS default is 6% state only; the 1% municipal record is kept for the
    books but never bound and never combined into a group tax.
    """
    Tax = env["account.tax"].with_company(company)
    root = company.root_id or company

    def _find(name):
        return Tax.search(
            [
                ("name", "=", name),
                # A tax on the company or any ancestor is usable here — reusing it
                # avoids the root-scoped "Tax names must be unique!" collision.
                ("company_id", "parent_of", company.id),
                ("type_tax_use", "=", "sale"),
                ("amount_type", "=", "percent"),
            ],
            limit=1,
        )

    state = _find(WV_STATE_NAME)
    if not state:
        state = Tax.with_company(root).create(
            {
                "name": WV_STATE_NAME,
                "amount": 6.0,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "company_id": root.id,
                "description": "WV 6%",
            }
        )

    # Keep the municipal record for the books, but do NOT bind it anywhere.
    if not _find(WV_MUNI_NAME):
        Tax.with_company(root).create(
            {
                "name": WV_MUNI_NAME,
                "amount": 1.0,
                "amount_type": "percent",
                "type_tax_use": "sale",
                "company_id": root.id,
                "description": "Muni 1%",
            }
        )

    return state


def _get_company_wv_state_tax(env, company):
    """Return the company's WV 6% state sale tax, creating it if missing.

    Thin, intent-revealing wrapper over ``_ensure_company_wv_taxes`` for callers
    (the shipping line) that only need the single state tax record.
    """
    return _ensure_company_wv_taxes(env, company)


def _retrofit_products(env, company, state):
    """Strip wrong sale taxes from existing products and ensure the WV 6% state tax.

    "Wrong" = any sale tax that is NOT a WV record accessible to this company.
    That covers the prod defect two ways: the Odoo demo ``"15%"`` (a non-WV name,
    stripped) and any tax owned by a company OUTSIDE this branch's hierarchy
    (unusable here, stripped). A WV record owned by the company **or one of its
    ancestors** is legitimately usable by a branch (check_company is ``parent_of``)
    and is kept — so re-running is idempotent even though ``state`` may be the
    shared root-company record. Products already carrying only such WV record(s)
    are left untouched. Purchase taxes are never touched. Logged so the change is
    auditable.
    """
    Template = env["product.template"].with_company(company)
    templates = Template.search(
        [
            ("sale_ok", "=", True),
            ("company_id", "in", [company.id, False]),
        ]
    )
    accessible = _accessible_companies(company)
    changed = 0
    for tmpl in templates:
        sale_taxes = tmpl.taxes_id
        keep = sale_taxes.filtered(lambda t: t.name in WV_KEEP_NAMES and t.company_id in accessible)
        desired = keep | state
        if set(desired.ids) == set(sale_taxes.ids):
            continue  # already correct
        tmpl.taxes_id = [(6, 0, desired.ids)]
        changed += 1
    if changed:
        _logger.info(
            "grove_headless: retrofitted WV 6%% state sales tax onto %s product(s) for company %s",
            changed,
            company.name,
        )


def _retrofit_shipping_product(env, company, state):
    """Ensure the GROVE-SHIP service product carries exactly the WV 6% state tax.

    The shipping SKU is created lazily at first checkout, so it may not exist yet
    (then this is a no-op). When it does, its sale tax is forced to the state tax
    so a WV shipment is taxed 6% on shipping regardless of how the product was
    created — mirroring the per-line force in ``_apply_shipping_line``.
    """
    product = (
        env["product.product"]
        .sudo()
        .with_company(company)
        .search(
            [
                ("default_code", "=", SHIPPING_PRODUCT_CODE),
                ("company_id", "in", [company.id, False]),
            ],
            limit=1,
        )
    )
    if product and set(product.taxes_id.ids) != {state.id}:
        product.taxes_id = [(6, 0, state.ids)]
        _logger.info(
            "grove_headless: reset %s tax to WV 6%% state for company %s",
            SHIPPING_PRODUCT_CODE,
            company.name,
        )


def setup_wv_sales_tax(env):
    """Ensure every company charges the WV 6% state sales tax by default."""
    companies = env["res.company"].search([])
    bound = 0
    for company in companies:
        try:
            state = _ensure_company_wv_taxes(env, company)

            # Authoritative default for new products (UI + website + API).
            env["ir.default"].set(
                "product.template",
                "taxes_id",
                state.ids,
                company_id=company.id,
            )

            # The single-valued company default. A branch may point at a root
            # company's tax (check_company is parent_of), so this is expected to
            # succeed — but if it ever cannot, the ir.default above still governs
            # product creation, so log at WARNING and carry on rather than abort.
            try:
                company.account_sale_tax_id = state.id
            except Exception as exc:
                _logger.warning(
                    "grove_headless: could not set account_sale_tax_id for %s (%s); ir.default taxes_id still applies",
                    company.name,
                    exc,
                )

            _retrofit_products(env, company, state)
            _retrofit_shipping_product(env, company, state)
            bound += 1
        except Exception as exc:  # never let tax setup abort install/upgrade
            _logger.warning(
                "grove_headless: WV tax setup FAILED for company %s: %s — company keeps its previous default sale tax",
                company.name,
                exc,
            )
            continue

    # Count actual successes, not the loop length: a swallowed per-company
    # failure (the GOL-2449 defect) must never read as "all companies bound".
    _logger.info(
        "grove_headless: WV 6%% state sales tax bound for %s of %s companies",
        bound,
        len(companies),
    )
    if bound < len(companies):
        _logger.warning(
            "grove_headless: WV 6%% state sales tax bound for only %s of %s companies — see WARNINGs above",
            bound,
            len(companies),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Point of Sale configuration (GOL-13)
# ─────────────────────────────────────────────────────────────────────────────
#
# Stands up the two in-person sales channels so market + nursery walk-in sales
# can be rung up:
#
#   * "Farmer's Market"  → crm.team "Farmer's Market"
#   * "Nursery Counter"  → crm.team "Direct to Nursery"
#
# Both live in the Goldberry Grove Farm company because that is where the seeded
# payment journals (CSH1/CARD/CHCK) and both sales teams live — in-person retail
# bookkeeping is consolidated under the farm company and differentiated by sales
# team/channel. (If nursery-counter revenue should instead post into the At The
# Grove Nursery company, that is a one-way accounting decision that also needs
# nursery-company journals seeded — out of scope here.)
#
# Payment methods are wired to the seeded journals. WV tax is NOT set on the
# POS config directly: POS lines inherit each product's ``taxes_id``, which the
# WV tax binding above already defaults to the "WV State Sales Tax 6%" tax — so a
# market sale is taxed 6% the same way a web order is. This keeps a single
# source of truth for the tax and avoids a second place to forget to update.
#
# Idempotent: everything is found-or-created by natural key (journal code /
# record name + company), so re-running (fresh install, ``-u`` upgrade, or the
# run-now scripts/setup_pos.py) only fills in what is missing.

POS_COMPANY_NAME = "Goldberry Grove Farm"

# Bank payment journals shared by every in-person channel — Card/Check settle to
# one shared bank journal each. Cash is deliberately NOT here: Odoo 19 forbids
# two POS configs sharing a cash payment method AND forbids two cash methods
# sharing a cash journal (pos.config._check_payment_method_ids_journal), so each
# channel owns its own cash journal + method (see POS_CONFIG_SPECS).
POS_BANK_JOURNAL_SPECS = [
    ("CARD", "Card", "bank"),
    ("CHCK", "Check", "bank"),
]

# (payment method label, bank journal code) for the shared non-cash methods.
POS_BANK_METHOD_SPECS = [
    ("Card", "CARD"),
    ("Check", "CHCK"),
]

# One in-person channel per row. Each carries a DEDICATED cash journal + cash
# method (the Odoo 19 constraint above), plus the shared bank methods.
#   (pos.config name, crm.team name, cash journal code, cash journal/method label)
POS_CONFIG_SPECS = [
    ("Farmer's Market", "Farmer's Market", "CSH1", "Cash (Farmer's Market)"),
    ("Nursery Counter", "Direct to Nursery", "CSH2", "Cash (Nursery Counter)"),
]


def _ensure_journal(env, company, code, name, jtype):
    """Find-or-create a payment journal by code within one company."""
    Journal = env["account.journal"].with_company(company)
    journal = Journal.search(
        [("code", "=", code), ("company_id", "=", company.id)],
        limit=1,
    )
    if not journal:
        journal = Journal.create(
            {
                "name": name,
                "code": code,
                "type": jtype,
                "company_id": company.id,
            }
        )
    return journal


def _ensure_sales_team(env, company, name):
    """Find-or-create a crm.team (sales channel) by name within one company."""
    Team = env["crm.team"].with_company(company)
    team = Team.search(
        [("name", "=", name), ("company_id", "=", company.id)],
        limit=1,
    )
    if not team:
        team = Team.create({"name": name, "company_id": company.id})
    return team


def _ensure_payment_method(env, company, label, journal):
    """Find-or-create a pos.payment.method bound to a journal within a company."""
    Method = env["pos.payment.method"].with_company(company)
    method = Method.search(
        [("name", "=", label), ("company_id", "=", company.id)],
        limit=1,
    )
    if not method:
        method = Method.create(
            {
                "name": label,
                "company_id": company.id,
                "journal_id": journal.id,
            }
        )
    elif method.journal_id != journal:
        method.journal_id = journal.id
    return method


def _ensure_pos_config(env, company, name, payment_methods, team):
    """Find-or-create a pos.config for one in-person channel.

    On create, Odoo fills the operational defaults (POS journal, picking type,
    pricelist). We then bind the payment methods and the sales team. Idempotent
    re-runs re-assert those two links without disturbing user customization of
    the rest of the config.
    """
    Config = env["pos.config"].with_company(company)
    config = Config.search(
        [("name", "=", name), ("company_id", "=", company.id)],
        limit=1,
    )
    if not config:
        config = Config.create({"name": name, "company_id": company.id})
    config.write(
        {
            "payment_method_ids": [(6, 0, payment_methods.ids)],
            "crm_team_id": team.id,
        }
    )
    return config


def _setup_company_pos(env, company):
    """Stand up both in-person POS channels for a single company. Idempotent.

    Bank methods (Card/Check) are shared across both channels; cash is per-channel
    — a dedicated cash journal + cash method each — because Odoo 19 rejects a cash
    payment method (or its journal) being reused by a second POS config.
    """
    bank_journals = {
        code: _ensure_journal(env, company, code, name, jtype) for code, name, jtype in POS_BANK_JOURNAL_SPECS
    }
    bank_methods = env["pos.payment.method"]
    for label, code in POS_BANK_METHOD_SPECS:
        bank_methods |= _ensure_payment_method(env, company, label, bank_journals[code])

    configs = env["pos.config"]
    methods = bank_methods
    for config_name, team_name, cash_code, cash_label in POS_CONFIG_SPECS:
        team = _ensure_sales_team(env, company, team_name)
        cash_journal = _ensure_journal(env, company, cash_code, cash_label, "cash")
        cash_method = _ensure_payment_method(env, company, cash_label, cash_journal)
        methods |= cash_method
        configs |= _ensure_pos_config(env, company, config_name, cash_method | bank_methods, team)

    _logger.info(
        "grove_headless: POS ready for company %s — %s config(s), %s payment method(s)",
        company.name,
        len(configs),
        len(methods),
    )
    return configs


def setup_pos_configs(env):
    """Configure the in-person POS channels on the farm company.

    Runs on fresh install (post_init_hook) and on ``-u grove_headless`` upgrade
    (migration). Targets the Goldberry Grove Farm company where the seeded
    journals + sales teams live. Wrapped so a POS/accounting hiccup (e.g. a
    company without a chart of accounts in a minimal DB) can never abort the
    module install/upgrade — the run-now scripts/setup_pos.py covers that path.
    """
    company = env["res.company"].search([("name", "=", POS_COMPANY_NAME)], limit=1)
    if not company:
        _logger.warning(
            "grove_headless: POS setup skipped — company %r not found",
            POS_COMPANY_NAME,
        )
        return
    try:
        _setup_company_pos(env, company)
    except Exception as exc:  # never let POS setup abort install/upgrade
        _logger.warning(
            "grove_headless: skipped POS setup for company %s: %s",
            company.name,
            exc,
        )


def post_init_hook(env):
    """Run on fresh install of grove_headless."""
    setup_wv_sales_tax(env)
    setup_pos_configs(env)
