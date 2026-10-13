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

import html
import logging
import re

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


def _converge_root_wv_taxes(env, root):
    """Collapse duplicate same-named WV sale taxes in one hierarchy onto the root.

    GOL-2449 follow-up (prod incident 2026-09-23). Odoo 19 scopes
    ``account.tax`` name-uniqueness to the hierarchy root
    (``_constrains_name`` searches ``company_id child_of root``) and validates
    ``@api.constrains`` at **flush**, not at ``create``. So two records with the
    same name *can* both land in the table — a historical per-branch row (the
    2026-08-08 ``data/grove_taxes.xml`` seed, owned by the nursery) and the
    root-level row this hook creates — and then **every later flush raises**
    *"Tax names must be unique!"*. ``setup_wv_sales_tax`` swallows that per
    company, so prod upgraded to ``1 of 3`` companies bound while the other two
    silently kept the demo 15% default.

    A branch may *use* a root/ancestor company's tax (``check_company`` is
    ``parent_of``), so the correct steady state is exactly ONE record per name,
    owned by the **root**. Converge to that:

      * keep the OLDEST row — it carries the product and accounting history
        (on prod, 123 nursery templates point at it);
      * repoint anything that referenced a younger duplicate at the keeper;
      * drop the duplicates, archiving instead of deleting any that turn out to
        be referenced after all;
      * only then move the keeper to the root, so the constraint never sees two
        same-named rows in the subtree at once.

    Idempotent: a hierarchy already in the steady state is left untouched.
    """
    Tax = env["account.tax"].sudo()
    Template = env["product.template"].sudo()
    Company = env["res.company"].sudo()

    for name in (WV_STATE_NAME, WV_MUNI_NAME):
        dupes = Tax.search(
            [
                ("name", "=", name),
                ("company_id", "child_of", root.id),
                ("type_tax_use", "=", "sale"),
            ],
            order="id asc",
        )
        if not dupes:
            continue

        keeper, extras = dupes[0], dupes[1:]

        for extra in extras:
            # Repoint the places a duplicate can be referenced before removing it.
            for tmpl in Template.search([("taxes_id", "in", extra.ids)]):
                tmpl.taxes_id = [(3, extra.id, 0), (4, keeper.id, 0)]
            for comp in Company.search([("account_sale_tax_id", "=", extra.id)]):
                comp.account_sale_tax_id = keeper.id
            try:
                extra.unlink()
            except Exception as exc:  # still referenced somewhere we do not know
                extra.active = False
                _logger.warning(
                    "grove_headless: could not delete duplicate tax %s (id %s) for %s (%s) — archived instead",
                    name,
                    extra.id,
                    root.name,
                    exc,
                )

        # Deletions are flushed before this write, so the constraint sees one row.
        if keeper.company_id != root:
            env.flush_all()
            _logger.info(
                "grove_headless: moving tax %s (id %s) from %s to hierarchy root %s so every branch can share it",
                name,
                keeper.id,
                keeper.company_id.name,
                root.name,
            )
            keeper.company_id = root.id


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

    # Converge each hierarchy to ONE root-owned record per WV tax name FIRST.
    # Odoo validates the root-scoped name-uniqueness constraint at flush, so a
    # leftover per-branch duplicate makes companies below it fail with
    # "Tax names must be unique!" — the 2026-09-23 prod incident, where this
    # loop reported "bound for only 1 of 3". See _converge_root_wv_taxes.
    roots = env["res.company"].browse(sorted({(c.root_id or c).id for c in companies}))
    for root in roots:
        try:
            _converge_root_wv_taxes(env, root)
        except Exception as exc:  # never let convergence abort install/upgrade
            _logger.warning(
                "grove_headless: WV tax convergence FAILED for hierarchy root %s: %s",
                root.name,
                exc,
            )

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


# ── Department tree restructure (GOL-2744) ──────────────────────────────────
#
# The nursery groups its public-category tree into departments (Orchard, plus
# the coming-soon Mycoforestry / Forest farming / Seed & scion families) and a
# cross-cutting Guilds collection. See grove-sites
# docs/superpowers/specs/2026-09-30-nursery-shop-departments-design.md.
#
# Existing category IDs are kept: the orchard top-level categories are reparented
# under a new Orchard department root, category "Food Forest Packages" becomes
# the Guilds collection, and "Mycoforestry" becomes a coming-soon department. New
# coming-soon departments and their child categories are created. Idempotent, so
# it is safe from both the post_init hook (fresh install) and the -u migration.

# Current display names of the orchard top-level categories, reparented (by name)
# under the Orchard department. Match is case-insensitive.
_ORCHARD_CATEGORY_NAMES = (
    "Fruit Trees",
    "Nut Trees",
    "Berry & Nut Shrubs",
    "Fruiting Vines",
    "Native",
)

# Desired departments: slug -> (name, status, sequence, facets, [child names]).
# The Orchard root adopts the existing orchard categories above; the coming-soon
# departments' children are created fresh. Facets are drawn from the spec's
# allowlist (models/product_public_category.GROVE_FACET_ALLOWLIST).
_DEPARTMENT_SPEC = {
    "orchard-food-forest": {
        "name": "Orchard & food forest",
        "status": "live",
        "sequence": 10,
        "facets": "zone,layer,sun,uses,on_offer,ships",
        "adopt": _ORCHARD_CATEGORY_NAMES,
        "children": (),
    },
    "mycoforestry": {
        "name": "Mycoforestry",
        "status": "coming_soon",
        "sequence": 20,
        "facets": "host_tree,fungus,zone,ships",
        # Adopts the existing "Mycoforestry" category (id 7 on prod) as its root.
        "adopt_as_root": "Mycoforestry",
        "children": ("Truffle trees", "Porcini trees"),
    },
    "forest-farming": {
        "name": "Forest farming",
        "status": "coming_soon",
        "sequence": 30,
        "facets": "shade_level,years_to_harvest,uses,zone,ships",
        "children": ("Medicinal roots", "Woodland edibles"),
    },
    "seed-and-scion": {
        "name": "Seed & scion",
        "status": "coming_soon",
        "sequence": 40,
        "facets": "form,species,ships",
        "children": ("Scion wood", "Seed", "Rootstock"),
    },
}

# The existing "Food Forest Packages" category becomes the Guilds collection.
_GUILDS_SLUG = "guilds"
_GUILDS_NAME = "Guilds"
_GUILDS_SOURCE_NAME = "Food Forest Packages"


def _public_slugify(text):
    """slugify() without importing the controllers package at model-load time."""
    from .controllers.product_domain import slugify

    return slugify(text)


def _find_category_by_name(Category, name):
    """First public category matching ``name`` case-insensitively, else empty."""
    return Category.search([("name", "=ilike", name)], limit=1)


def _unique_public_slug(Category, base, keep_id=None):
    """Return ``base`` (or ``base-<n>``) not already used by another category."""
    if not base:
        return base
    slug = base
    n = 1
    while True:
        clash = Category.search([("grove_slug", "=", slug), ("id", "!=", keep_id or 0)], limit=1)
        if not clash:
            return slug
        n += 1
        slug = f"{base}-{n}"


def _backfill_public_slugs(Category):
    """Set grove_slug = slugify(current name) on every category still missing one.

    Run first so existing URLs (?cat=fruit-trees, …) are preserved before any
    rename below changes a name. Skips rows that already carry a slug.
    """
    for category in Category.search([("grove_slug", "in", (False, ""))]):
        base = _public_slugify(category.name)
        category.grove_slug = _unique_public_slug(Category, base, keep_id=category.id)


def _ensure_category(Category, slug, name, vals):
    """Upsert a public category by grove_slug; returns the record.

    Found -> writes the given vals (name/kind/status/etc.). Missing -> creates it
    with the slug. Never touches parent unless ``vals`` carries ``parent_id``.
    """
    record = Category.search([("grove_slug", "=", slug)], limit=1)
    write_vals = dict(vals)
    write_vals.setdefault("name", name)
    if record:
        record.write(write_vals)
        return record
    write_vals["grove_slug"] = slug
    return Category.create(write_vals)


def restructure_department_tree(env):
    """Build the department tree on ``product.public.category`` (GOL-2744).

    Idempotent and order-safe: backfills slugs, converts the Food Forest Packages
    category into the Guilds collection, stands up the four departments (adopting
    the existing orchard categories and the Mycoforestry category, keeping their
    IDs), and creates the coming-soon child categories.
    """
    Category = env["product.public.category"].sudo()

    # 1. Preserve existing URLs before any rename.
    _backfill_public_slugs(Category)

    # 2. Food Forest Packages -> Guilds collection (keep its id + products).
    guilds = Category.search([("grove_slug", "=", _GUILDS_SLUG)], limit=1)
    if not guilds:
        guilds = _find_category_by_name(Category, _GUILDS_SOURCE_NAME)
    if guilds:
        guilds.write(
            {
                "name": _GUILDS_NAME,
                "grove_slug": _GUILDS_SLUG,
                "grove_node_kind": "collection",
                "parent_id": False,
            }
        )
    else:
        guilds = _ensure_category(
            Category,
            _GUILDS_SLUG,
            _GUILDS_NAME,
            {"grove_node_kind": "collection", "parent_id": False},
        )

    # 3. Departments + their categories.
    for slug, spec in _DEPARTMENT_SPEC.items():
        dept_vals = {
            "grove_node_kind": "department",
            "grove_dept_status": spec["status"],
            "grove_facets": spec["facets"],
            "sequence": spec["sequence"],
            "parent_id": False,
        }
        # A department may adopt an existing category as its root (keeps id 7 for
        # Mycoforestry); otherwise it is upserted by slug.
        root = None
        adopt_root_name = spec.get("adopt_as_root")
        if adopt_root_name:
            root = _find_category_by_name(Category, adopt_root_name)
        if root:
            dept_vals["name"] = spec["name"]
            dept_vals["grove_slug"] = slug
            root.write(dept_vals)
        else:
            root = _ensure_category(Category, slug, spec["name"], dept_vals)

        # Reparent existing orchard categories under the department root.
        for adopt_name in spec.get("adopt", ()):
            child = _find_category_by_name(Category, adopt_name)
            if child and child.id != root.id:
                child.write({"parent_id": root.id, "grove_node_kind": "category"})

        # Create the coming-soon child categories.
        for child_name in spec.get("children", ()):
            child_slug = _unique_public_slug(Category, _public_slugify(child_name))
            existing = _find_category_by_name(Category, child_name)
            if existing:
                existing.write({"parent_id": root.id, "grove_node_kind": "category"})
            else:
                Category.create(
                    {
                        "name": child_name,
                        "grove_slug": child_slug,
                        "grove_node_kind": "category",
                        "parent_id": root.id,
                    }
                )

    _logger.info("grove_headless: department tree restructured (GOL-2744)")


# ── Guilds membership backfill (GOL-2882) ───────────────────────────────────
#
# The Guilds collection (ex "Food Forest Packages", prod category 6) should
# surface every live multi-tree bundle. tmpl 22 — the "(5-Tree Native Bundle)"
# memorial SKU ($47, published, sale_ok) — shipped with ``public_categ_ids=[]``,
# so it was omitted from /shop/guilds and from shop category filtering while its
# five siblings (132/133/134/135/140) were listed. CEO ruling (GOL-2882): YES,
# include it. Matched by its stable "(5-Tree Native Bundle)" name suffix rather
# than a raw id, so the fix survives the product's Remembrance→Chestnut rename
# and is safe to run on any DB (no-ops where the product/category are absent).
_GUILDS_BUNDLE_NAME_LIKE = "(5-Tree Native Bundle)"


def add_native_bundle_to_guilds(env):
    """Add the 5-Tree Native Bundle to the Guilds collection (GOL-2882).

    Idempotent and additive: resolves the Guilds category by slug (falling back
    to its source name) and the bundle by its "(5-Tree Native Bundle)" suffix,
    then links the product into that category without touching any other
    membership. No-ops cleanly when either record is absent (fresh install / QA
    before the catalog is seeded).
    """
    Category = env["product.public.category"].sudo()
    Template = env["product.template"].sudo()

    guilds = Category.search([("grove_slug", "=", _GUILDS_SLUG)], limit=1)
    if not guilds:
        guilds = _find_category_by_name(Category, _GUILDS_SOURCE_NAME)
    if not guilds:
        _logger.warning("grove_headless: Guilds category not found; skipping bundle add (GOL-2882)")
        return

    bundle = Template.search([("name", "ilike", _GUILDS_BUNDLE_NAME_LIKE)], limit=1)
    if not bundle:
        _logger.info("grove_headless: 5-Tree Native Bundle not found; nothing to add (GOL-2882)")
        return

    if guilds.id in bundle.public_categ_ids.ids:
        return
    bundle.write({"public_categ_ids": [(4, guilds.id)]})
    _logger.info("grove_headless: added %r to the Guilds collection (GOL-2882)", bundle.name)


# ── Approved department copy (GOL-3292 / GOL-2746) ──────────────────────────
#
# The GOL-2744 restructure built the department tree but left every
# grove_teaser = "" and every grove_coming_list empty. This loads Sora's
# approved teasers + "What's coming" lists (GOL-2746 comment 8df5c928) and the
# Guilds lede, matched by grove_slug.
#
# Each field is written ONLY when it is currently empty, so any edit later made
# in Odoo survives a re-run (the Train #3 ``-u grove_headless`` upgrade and
# every upgrade after it). Both fields are ``translate=True``; the copy is
# written in the default ``en_US`` context so it lands on the source term.
#
# The text is Sora's §1/§2 verbatim except punctuation: em dashes became
# colons, semicolons or commas (storefront no-em-dash rule, GOL-589; the
# storefront prints these fields as-is). grove_coming_list is one
# ``Name | detail`` item per line (parsed by grove_coming_items).
_DEPARTMENT_COPY = {
    "orchard-food-forest": {
        "teaser": (
            "Fruit, nut and native trees for the orchard or food forest you're "
            "putting in: the same hardy Appalachian stock we plant on our own hill."
        ),
    },
    "mycoforestry": {
        "teaser": (
            "Host trees that come with their fungal partner already on the roots: "
            "pecan truffle and porcini, on white oak, hybrid chestnut and American "
            "hazelnut. The tree is the crop you can count on; the mushrooms are the "
            "long game, and whether they show up depends on your soil, your pH and "
            "your patience. We're growing the first batch out now."
        ),
        "coming_list": "\n".join(
            (
                "White oak, pecan truffle | inoculated seedling: a shade tree now, truffle ground later",
                "White oak, porcini | the same oak with porcini instead; you pick which fungus rides on the roots",
                "Hybrid chestnut, porcini | nuts off the tree, porcini out of the ground under it",
                (
                    "American hazelnut, pecan truffle | bears young and stays shrub-sized, "
                    "the quickest way into a truffle planting"
                ),
                "Block plantings, ten trees and up | bulk rates for putting in a stand instead of one specimen tree",
            )
        ),
    },
    "forest-farming": {
        "teaser": (
            "The woodland crops that grow in the shade you already have: goldenseal, "
            "American ginseng, ramps, black cohosh. These are slow, three to five "
            "years for goldenseal and closer to a decade for wild-simulated ginseng, "
            "so what we'll sell is planting stock, not dug root. If you've got "
            "north-facing hardwood with trillium and bloodroot coming up under it, "
            "you've got the site for it."
        ),
        "coming_list": "\n".join(
            (
                "Goldenseal | root divisions: three to five years to a harvestable root",
                (
                    "American ginseng | shade-grown planting stock; seven to eleven years "
                    "wild-simulated, and harvest is state-regulated"
                ),
                "Ramps | two to three years to your first harvest from bulblets, five to seven from seed",
                "Black cohosh | root divisions: three to five years, the quickest of the four",
            )
        ),
    },
    "seed-and-scion": {
        "teaser": (
            "For the people who'd rather graft it themselves: dormant scion wood cut "
            "while the trees are still asleep, seed cleaned and stratified over "
            "winter, and rootstock to put it all on. Scion only travels well "
            "dormant, so this one opens for a short season rather than year-round; "
            "the notify list is how you'll hear it's open. Same stock as the rest of "
            "the nursery, sold a step earlier."
        ),
        "coming_list": "\n".join(
            (
                "Apple scion wood | dormant cuttings for grafting onto your own rootstock",
                "Pear scion wood | same idea, cut from the pears we grow here",
                "Fig cuttings | figs root straight from dormant wood, no grafting needed",
                "Chestnut and pawpaw seed | cold-stratified over winter, sold ready to plant",
                "Rootstock | bare-root stock to graft your own trees onto",
            )
        ),
    },
    "guilds": {
        "teaser": (
            "Sets we'd actually plant together: something to bloom early, something "
            "to feed you later, something to hold the soil. One box, and a plan for "
            "where each tree goes."
        ),
    },
}


def _load_copy_field(record, field, value):
    """Write ``value`` to ``field`` on ``record`` only when it is empty today.

    Returns True when it wrote, False when it left an existing value alone. The
    guard reads the stored value so a re-run never clobbers an Odoo-side edit.
    """
    if (record[field] or "").strip():
        return False
    record.write({field: value})
    return True


def load_department_copy(env):
    """Load the approved department teasers + coming lists (GOL-3292).

    Matches departments / the Guilds collection by grove_slug and fills
    grove_teaser / grove_coming_list from ``_DEPARTMENT_COPY``, writing each
    field only when it is currently empty. Idempotent: a second run is a no-op,
    and a teaser later edited in Odoo is never overwritten. No-ops cleanly for
    any slug missing from the DB (fresh install / QA before the tree is seeded).

    ``translate=True`` fields are written in the ``en_US`` context so the copy
    lands on the source language regardless of the upgrade user's language.
    """
    Category = env["product.public.category"].with_context(lang="en_US").sudo()
    for slug, copy in _DEPARTMENT_COPY.items():
        record = Category.search([("grove_slug", "=", slug)], limit=1)
        if not record:
            _logger.info("grove_headless: no category for slug %r; skipping copy (GOL-3292)", slug)
            continue
        wrote = False
        if copy.get("teaser"):
            wrote |= _load_copy_field(record, "grove_teaser", copy["teaser"])
        if copy.get("coming_list"):
            wrote |= _load_copy_field(record, "grove_coming_list", copy["coming_list"])
        if wrote:
            _logger.info("grove_headless: loaded approved copy for %r (GOL-3292)", slug)


# ── Guild card purpose lines (GOL-3304) ─────────────────────────────────────
#
# Sora's approved one-line guild descriptions (GOL-2746 comment 8df5c928,
# approved 2026-09-30; em dashes already swapped per GOL-589). Each line is
# prepended to the matched guild template's description_ecommerce as a NEW first
# <p>. The storefront Guilds-page card purpose line is
# ``guildPurpose(description_html)`` = the first sentence of the PDP description
# (grove-sites), and description_html is description_ecommerce
# (controllers/main.py), so this is what fixes the Guilds-page card copy —
# e.g. tmpl 140's factual error ("...redbud for pollination": redbud brings
# pollinators in, it does not pollinate the pears).
#
# Matched by product NAME, case-insensitive prefix (prod names carry a
# "(... Trees)" style suffix), NOT by raw product.template id: GOL-3292 Part B
# keyed the six lines on ids 22/132/133/140/135/134, which are not stable across
# QA and prod, so it was dropped and re-scoped here.
#
# The "Chestnut Grove" bundle (prod tmpl 22, the GOL-2750 native bundle) is the
# template Sora wrote the "Remembrance Grove" line for; confirmed against prod
# 2026-10-13 that tmpl 22 is this bundle before keying the row on its real
# catalog name rather than Sora's working title.
_GUILD_CARD_LINES = (
    (
        "Chestnut Grove",
        "Five natives to put in the ground in someone's memory: chestnut, "
        "persimmon, plum, serviceberry and mulberry, each one still feeding "
        "something long after the planting day.",
    ),
    (
        "Mountain Mama Package",
        "The Appalachian starter: two chestnuts, two pawpaws and a mountain "
        "laurel. The hillside's own plant list, in one box.",
    ),
    (
        "Pollinator Bundle",
        "Eastern redbud anchors it and blooms before almost anything else, with "
        "four supporting natives to keep the bees fed the rest of the season.",
    ),
    (
        "Heirloom Fruit Package",
        "Two pears, two persimmons, and a redbud that flowers ahead of them to "
        "bring the pollinators in before the fruit trees open.",
    ),
    (
        "At the Grove Food Forest",
        "Fifty seedlings at our bulk seedling rate, plus a half-hour consult on "
        "what to plant where; the honest starting point for a food forest.",
    ),
    (
        "Centennial Food Forest",
        "A hundred trees for the people planting something their grandkids will " "be the ones to harvest.",
    ),
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _visible_text(fragment):
    """Collapse an HTML fragment to its visible text for comparison.

    Strips tags, unescapes entities, turns ``&nbsp;`` into a space and collapses
    whitespace runs. Used to tell whether the first paragraph already reads the
    approved line, so a re-run is a no-op.
    """
    without_tags = _TAG_RE.sub(" ", fragment or "")
    unescaped = html.unescape(without_tags).replace("\xa0", " ")
    return _WS_RE.sub(" ", unescaped).strip()


def _first_paragraph_html(description):
    """Return the first ``<p>...</p>`` fragment of ``description``, or "" if none."""
    match = re.search(r"<p\b[^>]*>.*?</p>", description or "", re.IGNORECASE | re.DOTALL)
    return match.group(0) if match else ""


def _prepend_guild_card_line(record, line):
    """Prepend ``line`` as a new first ``<p>`` of description_ecommerce.

    Writes nothing (returns False) when the current first paragraph already reads
    the approved line, so the upgrade is idempotent. Returns True when it wrote.
    """
    current = record.description_ecommerce or ""
    if _visible_text(_first_paragraph_html(current)) == line:
        return False
    record.description_ecommerce = f"<p>{line}</p>{current}"
    return True


def load_guild_card_lines(env):
    """Load Sora's approved guild card purpose lines (GOL-3304).

    Prepends each approved line (``_GUILD_CARD_LINES``) to the matching guild
    template's description_ecommerce as a new first ``<p>``, matched by product
    name (case-insensitive prefix). Asserts exactly one template per name; a name
    that is absent is logged and skipped (QA may not carry every bundle, and a
    fresh install carries none) rather than failing the upgrade. Idempotent: a
    re-run whose first paragraph already reads the approved line writes nothing.

    description_ecommerce carries no ``translate=True`` here, and the write stamps
    no grove_facts_provenance, so the model's write override treats it as a human
    edit: it marks the field provenance ``human`` (protecting the editorial line)
    and never clears the Facts-reviewed sign-off or re-gates publishing.
    """
    Product = env["product.template"].sudo()
    for prefix, line in _GUILD_CARD_LINES:
        matches = Product.search([("name", "=ilike", prefix + "%")])
        if not matches:
            _logger.info("grove_headless: no guild template for %r; skipping card line (GOL-3304)", prefix)
            continue
        if len(matches) != 1:
            raise ValueError(
                f"grove_headless (GOL-3304): guild name prefix {prefix!r} matched "
                f"{len(matches)} templates ({matches.mapped('name')!r}); expected exactly one"
            )
        if _prepend_guild_card_line(matches, line):
            _logger.info("grove_headless: prepended approved card line to %r (GOL-3304)", matches.name)


def post_init_hook(env):
    """Run on fresh install of grove_headless."""
    setup_wv_sales_tax(env)
    setup_pos_configs(env)
    # NB: restructure_department_tree is deliberately NOT called here. It runs
    # only from the 19.0.1.56.0 migration, against a DB that already holds the
    # real public categories (prod ids 1-7). A fresh install has no categories to
    # restructure, and building empty department roots pre-emptively would then
    # collide with the categories a QA seed creates afterwards (the seed itself
    # calls restructure_department_tree once its categories exist — GOL-2744
    # follow-up on the QA seed).
