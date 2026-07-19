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
                "grove_headless: could not set account_sale_tax_id for %s (%s); ir.default taxes_id still applies",
                company.name,
                exc,
            )

        _retrofit_products(env, company, group)

    _logger.info("grove_headless: WV sales tax binding ensured for %s companies", len(companies))


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
# Payment methods are wired to the seeded journals. WV 7% tax is NOT set on the
# POS config directly: POS lines inherit each product's ``taxes_id``, which the
# WV tax binding above already defaults to the "WV Sales Tax 7%" group — so a
# market sale is taxed 7% the same way a web order is. This keeps a single
# source of truth for the tax and avoids a second place to forget to update.
#
# Idempotent: everything is found-or-created by natural key (journal code /
# record name + company), so re-running (fresh install, ``-u`` upgrade, or the
# run-now scripts/setup_pos.py) only fills in what is missing.

POS_COMPANY_NAME = "Goldberry Grove Farm"

# ── Transactional email sender identity (GOL-465) ───────────────────────────
# Each company sends its order/receipt/ship/back-in-stock mail From a dedicated
# `notifications@<domain>` address so transactional reputation is isolated from
# human + marketing mail. The domains match data/grove_companies.xml (and the
# live, registered zones — note the Woodworking domain is single-g
# `woodworkingeorge.com`; the double-g variant is not registered). The actual
# SMTP relay host/creds are supplied out-of-band via odoo.conf env
# (SMTP_SERVER/SMTP_USER/SMTP_PASSWORD, FROM_FILTER left empty so Odoo keeps
# each company's real From) once the ESP + `send.<domain>` DKIM exist.
COMPANY_NOTIFICATION_SENDERS = {
    "Goldberry Grove Farm": "notifications@goldberrygrove.farm",
    "George George George Woodworking": "notifications@woodworkingeorge.com",
    "At The Grove Nursery": "notifications@atthegrovenursery.com",
}

# (journal code, journal name, journal type) for the payment journals the POS
# settles to. Mirrors scripts/seed_payment_journals.py so the module is
# self-sufficient in a fresh DB where that seed script has not been run.
POS_JOURNAL_SPECS = [
    ("CSH1", "Cash", "cash"),
    ("CARD", "Card", "bank"),
    ("CHCK", "Check", "bank"),
]

# (payment method label, journal code) — cash-vs-bank is derived by Odoo from
# the linked journal's type, so we only bind the journal.
POS_PAYMENT_METHOD_SPECS = [
    ("Cash", "CSH1"),
    ("Card", "CARD"),
    ("Check", "CHCK"),
]

# (pos.config name, crm.team name) for the two in-person channels.
POS_CONFIG_SPECS = [
    ("Farmer's Market", "Farmer's Market"),
    ("Nursery Counter", "Direct to Nursery"),
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
    """Stand up both in-person POS channels for a single company. Idempotent."""
    journals = {code: _ensure_journal(env, company, code, name, jtype) for code, name, jtype in POS_JOURNAL_SPECS}

    methods = env["pos.payment.method"]
    for label, code in POS_PAYMENT_METHOD_SPECS:
        methods |= _ensure_payment_method(env, company, label, journals[code])

    configs = env["pos.config"]
    for config_name, team_name in POS_CONFIG_SPECS:
        team = _ensure_sales_team(env, company, team_name)
        configs |= _ensure_pos_config(env, company, config_name, methods, team)

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


def setup_transactional_senders(env):
    """Set each company's transactional From address to notifications@<domain>.

    Runs on fresh install (post_init_hook) and on ``-u grove_headless`` upgrade
    (migration). ``res.company.email`` is the fallback From that Odoo stamps on
    business documents (order confirmations, receipts, shipping/back-in-stock
    notifications) when the mail template resolves the company as the sender, so
    setting it per company keeps transactional reputation on the dedicated
    notification domain. Idempotent: only writes when the value differs, so
    re-running (or a user later customizing it) is safe and quiet.

    This only wires the *sender identity* — the SMTP relay itself is configured
    via odoo.conf env (SMTP_SERVER/SMTP_USER/SMTP_PASSWORD, empty FROM_FILTER).
    """
    Company = env["res.company"]
    for name, sender in COMPANY_NOTIFICATION_SENDERS.items():
        company = Company.search([("name", "=", name)], limit=1)
        if not company:
            _logger.warning(
                "grove_headless: transactional sender skipped — company %r not found",
                name,
            )
            continue
        if company.email != sender:
            company.email = sender
            _logger.info(
                "grove_headless: set %s transactional sender From to %s",
                name,
                sender,
            )


def post_init_hook(env):
    """Run on fresh install of grove_headless."""
    setup_wv_sales_tax(env)
    setup_pos_configs(env)
    setup_transactional_senders(env)
