"""Promo-code + automatic volume-tier discount resolution for headless checkout
(GOL-2431).

House rules ratified by Josh (2026-09-22 — do not re-open):

* Promo codes and volume tiers both come from Odoo ``sale_loyalty``
  (``loyalty.program``); Stripe promotion codes are never used. The Odoo-computed
  discount rides to Stripe as a one-time coupon on the Checkout Session (the
  existing ``stripe_gateway.create_coupon`` path — see ``_build_stripe_line_items``).
* Volume tiers are automatic (``trigger='auto'``) programs of the tenant company:
  5+ qualifying trees → 10% off, 10+ → 20%. Thresholds/percentages live in the
  Odoo program so marketing can change them without a deploy.
* **Best single discount wins**: a cart gets EITHER the volume tier OR the promo
  code, whichever saves more — never both. The response says which applied.
* A "tree" is any nursery plant product unit; a phantom-BOM bundle counts as its
  component tree count (Remembrance Grove = 5). Supplies / gift cards / services
  never count and are never discounted.
* Same gate as codes: discounts apply to ships-now carts only. A deposit/preorder
  cart gets neither — the caller enforces that with ``_cart_has_preorder`` BEFORE
  calling in, so this module never sees a deposit cart.

The Odoo-touching helpers take a live ``sale.order`` and are exercised by the
Odoo test runner (``test_promotions.py``); the pure string helpers are unit-
testable by file path.
"""

import logging
import math
from datetime import date as _date

_logger = logging.getLogger(__name__)

# The synthetic shipping product's default_code (mirrors main.SHIPPING_PRODUCT_CODE;
# duplicated to keep this module importable by main without a cycle).
SHIPPING_PRODUCT_CODE = "GROVE-SHIP"


# ── qualifying "tree" counting ───────────────────────────────────────────────


def is_qualifying_plant(product):
    """A nursery plant unit: a consumable product categorised under the Plants
    root (``grove_headless.categ_plants``). Supplies, gift cards and services are
    never plants; bundles are exempt (they count via their components, below)."""
    tmpl = product.product_tmpl_id
    if tmpl.type != "consu":
        return False
    env = product.env
    plants_root = env.ref("grove_headless.categ_plants", raise_if_not_found=False)
    categ = tmpl.categ_id
    if not plants_root or not categ or not categ.parent_path or not plants_root.parent_path:
        return False
    # parent_path is the materialised root→node id path; a descendant's path is
    # prefixed by its ancestor's (and the root's own path prefixes itself), so
    # this is "under Plants, inclusive" — the same test the publish gate uses.
    return categ.parent_path.startswith(plants_root.parent_path)


def _bundle_tree_count(env, variant):
    """Component tree count of one unit of a phantom-BOM bundle: the total
    qualifying-plant component units in its exploded BOM. 0 when ``variant`` is
    not a bundle (no phantom BOM) — the caller then treats it as a standalone
    line. Best-effort: a BOM/registry hiccup logs and counts 0 rather than
    breaking checkout."""
    if "mrp.bom" not in env.registry:
        return 0
    try:
        bom = env["mrp.bom"].sudo()._bom_find(variant, bom_type="phantom").get(variant)
        if not bom:
            return 0
        _boms, exploded = bom.explode(variant, 1)
    except Exception:  # noqa: BLE001 — never let a bundle BOM gap break the count
        _logger.warning("GOL-2431: could not explode bundle BOM for variant %s", variant.id, exc_info=True)
        return 0
    total = 0
    for bom_line, line_data in exploded:
        if is_qualifying_plant(bom_line.product_id):
            total += int(line_data.get("qty") or bom_line.product_qty or 0)
    return total


def qualifying_tree_count(order):
    """Total qualifying trees in ``order``: standalone plant units at face value,
    each bundle unit expanded to its component tree count. Reward, shipping and
    display lines never count."""
    total = 0
    for line in order.order_line:
        if line.display_type or not line.product_id or line.reward_id:
            continue
        qty = int(line.product_uom_qty or 0)
        if qty <= 0:
            continue
        variant = line.product_id
        if variant.default_code == SHIPPING_PRODUCT_CODE:
            continue
        per_unit = _bundle_tree_count(order.env, variant)
        if per_unit:
            total += per_unit * qty
        elif is_qualifying_plant(variant):
            total += qty
    return total


def variant_tree_count(variant):
    """Qualifying-tree count for ONE unit of ``variant`` — the per-variant field
    the product-detail payload exposes (``variants[].tree_count``, GOL-2439) so
    the storefront can sum ``tree_count × qty`` for the volume-tier nudge. Same
    rule as ``qualifying_tree_count``: a phantom-BOM bundle expands to its
    component tree count (Remembrance Grove = 5), a standalone nursery plant is
    1, and supplies, gift cards and services are 0."""
    per_unit = _bundle_tree_count(variant.env, variant)
    if per_unit:
        return per_unit
    return 1 if is_qualifying_plant(variant) else 0


# ── automatic volume-tier feed (GET /promotions/auto) ────────────────────────


def _program_currently_valid(program, today):
    """Active, in-date, and available to this company. ``date_from``/``date_to``
    bound the window; ``company_id`` False means all companies."""
    if not program.active:
        return False
    if program.date_from and program.date_from > today:
        return False
    if program.date_to and program.date_to < today:
        return False
    return True


def _auto_programs(env, company, today):
    """Automatic (no-code) loyalty programs available to ``company`` and valid on
    ``today``."""
    programs = (
        env["loyalty.program"]
        .sudo()
        .search(
            [
                ("trigger", "=", "auto"),
                ("company_id", "in", [company.id, False]),
            ]
        )
    )
    return programs.filtered(lambda p: _program_currently_valid(p, today))


def _points_per_unit(program):
    """Points a single qualifying unit earns under ``program``'s rule. Defaults
    to 1 (the "1 point per unit" QA shape) when unset or non-unit."""
    rule = program.rule_ids[:1]
    if not rule:
        return 1.0
    amount = rule.reward_point_amount or 1.0
    return amount or 1.0


def _reward_min_units(program, reward):
    """Minimum qualifying units needed to claim ``reward`` — its required points
    divided by the per-unit point award, rounded up."""
    per_unit = _points_per_unit(program)
    if per_unit <= 0:
        return 0
    return int(math.ceil((reward.required_points or 0) / per_unit))


def auto_tier_feed(env, company, today):
    """``[{min_qty, percent, label}]`` for the storefront volume-tier nudge
    ("Add 2 more trees to unlock 10% off"), derived from the tenant's automatic
    percent-discount programs, ascending by threshold. Only percent-off order
    rewards surface; a fixed or free-product reward is not a "% off N trees"
    tier and is skipped."""
    tiers = []
    for program in _auto_programs(env, company, today):
        for reward in program.reward_ids:
            if reward.reward_type != "discount" or reward.discount_mode != "percent":
                continue
            tiers.append(
                {
                    "min_qty": _reward_min_units(program, reward),
                    "percent": reward.discount,
                    "label": reward.description or program.name,
                }
            )
    tiers.sort(key=lambda t: (t["min_qty"], t["percent"]))
    return tiers


# ── discount value helpers ───────────────────────────────────────────────────


def reward_magnitude(order):
    """Positive untaxed dollars the currently-applied reward line(s) knock off
    the order — the number that becomes the Stripe coupon (untaxed; the reward's
    own negative tax nets the tax line separately, see ``_build_stripe_line_items``)."""
    reward_lines = order.order_line.filtered(lambda ln: ln.reward_id and not ln.display_type)
    return round(-sum(reward_lines.mapped("price_subtotal")), 2)


def goods_subtotal(order):
    """Untaxed subtotal of the real goods lines (excludes shipping, reward and
    display lines) — the pre-discount base the shortfall messages talk about."""
    total = 0.0
    for line in order.order_line:
        if line.display_type or not line.product_id or line.reward_id:
            continue
        if line.product_id.default_code == SHIPPING_PRODUCT_CODE:
            continue
        total += line.price_subtotal
    return round(total, 2)


def _analytic_value(order, reward):
    """A cheap, monotonic dollar estimate of a discount reward's worth, used only
    to RANK candidates (the winning amount is always measured on the real order).
    Percent-off scales with the untaxed order base; a fixed per-order discount is
    its face amount capped at the order total."""
    base = order.amount_untaxed
    if reward.discount_mode == "percent":
        return round((reward.discount or 0.0) / 100.0 * base, 2)
    # per_order / per_point fixed amount
    return round(min(reward.discount or 0.0, order.amount_total), 2)


# ── promo-code failure explanation (coupon-specific messages) ─────────────────


def _rule_valid_product_names(rule):
    """Human product names a rule restricts to (unique template names). Empty
    when the rule covers the whole catalog."""
    names = []
    for tmpl in rule.product_ids.mapped("product_tmpl_id"):
        if tmpl.name and tmpl.name not in names:
            names.append(tmpl.name)
    if not names and rule.product_category_id:
        names.append(rule.product_category_id.name)
    tag = getattr(rule, "product_tag_id", False)
    if not names and tag:
        names.append(tag.name)
    return names


def _rule_is_catalog_wide(rule):
    """True when the rule places no product restriction (so it applies to any
    tree). A raw ``product_domain`` other than the empty list is treated as a
    restriction we can't summarise, so it is NOT catalog-wide."""
    domain = (rule.product_domain or "").strip()
    has_domain = domain not in ("", "[]")
    return not (rule.product_ids or rule.product_category_id or getattr(rule, "product_tag_id", False) or has_domain)


def describe_products(rule):
    """The "(apple, pear or plum)" fragment for a rule. Catalog-wide → "any tree".
    ≤4 names → comma list with "or" before the last. >4 → first three + "and N
    others"."""
    if _rule_is_catalog_wide(rule):
        return "any tree"
    names = _rule_valid_product_names(rule)
    if not names:
        return "any tree"
    if len(names) == 1:
        return names[0]
    if len(names) <= 4:
        return ", ".join(names[:-1]) + " or " + names[-1]
    return ", ".join(names[:3]) + f" and {len(names) - 3} others"


def _rule_matches_product(rule, product):
    """Does ``product`` count toward ``rule``'s minimum? Catalog-wide rules match
    every qualifying plant; otherwise match by explicit product, category
    (inclusive of descendants) or tag."""
    if _rule_is_catalog_wide(rule):
        return is_qualifying_plant(product)
    if rule.product_ids and product in rule.product_ids:
        return True
    categ = rule.product_category_id
    if categ and product.categ_id and product.categ_id.parent_path and categ.parent_path:
        if product.categ_id.parent_path.startswith(categ.parent_path):
            return True
    tag = getattr(rule, "product_tag_id", False)
    if tag and tag in (product.all_product_tag_ids | product.additional_product_tag_ids):
        return True
    return False


def _rule_cart_totals(rule, order):
    """(qualifying units, qualifying untaxed subtotal) in ``order`` for ``rule``."""
    qty = 0
    amount = 0.0
    for line in order.order_line:
        if line.display_type or not line.product_id or line.reward_id:
            continue
        if line.product_id.default_code == SHIPPING_PRODUCT_CODE:
            continue
        if _rule_matches_product(rule, line.product_id):
            qty += int(line.product_uom_qty or 0)
            amount += line.price_subtotal
    return qty, round(amount, 2)


def _fmt_money(value):
    """ "$75" for a whole number, "$74.50" otherwise."""
    value = round(value, 2)
    if value == int(value):
        return f"${int(value)}"
    return f"${value:.2f}"


def _fmt_date(d):
    """ "Sep 30" — month abbrev + non-padded day (portable, no %-d)."""
    return f"{d.strftime('%b')} {d.day}"


def explain_code_failure(env, order, code, generic, today):
    """Turn ``sale_loyalty``'s generic rejection into a coupon-specific message.

    Reads the program matching ``code`` and reports the concrete reason:
    expired / used-up / wrong-store first (short, specific), else the unmet rule
    (a qualifying-quantity or subtotal shortfall). Falls back to ``generic`` when
    no matching program is found (an unknown code) or the reason isn't one we
    model — we never invent a reason. ``code`` is the shopper's input; ``today``
    is injected for deterministic expiry tests.
    """
    program = _program_for_code(env, code)
    if not program:
        return generic

    shown = code.strip().upper()

    if program.company_id and order.company_id and program.company_id != order.company_id:
        return "This code isn't available in this store."
    if program.date_to and program.date_to < today:
        return f"This code expired on {_fmt_date(program.date_to)}."
    if getattr(program, "limit_usage", False):
        used = program.total_order_count or 0
        cap = program.max_usage or 0
        if cap and used >= cap:
            return "This code has already been used."

    rule = program.rule_ids[:1]
    if rule:
        have_qty, have_amount = _rule_cart_totals(rule, order)
        min_qty = int(rule.minimum_qty or 0)
        if min_qty and have_qty < min_qty:
            need = min_qty - have_qty
            noun = "tree" if min_qty == 1 else "trees"
            return (
                f"{shown} needs {min_qty} qualifying {noun} ({describe_products(rule)}); "
                f"you have {have_qty}. Add {need} more."
            )
        min_amount = rule.minimum_amount or 0.0
        if min_amount and have_amount < min_amount:
            need = round(min_amount - have_amount, 2)
            return f"{shown} needs a {_fmt_money(min_amount)} subtotal; add {_fmt_money(need)} more."

    return generic


def _program_for_code(env, code):
    """The ``loyalty.program`` a code belongs to, or an empty recordset. Matches a
    ``with_code`` promotion rule first, then a coupon card code (case-insensitive)."""
    code = (code or "").strip()
    if not code:
        return env["loyalty.program"].sudo()
    rule = env["loyalty.rule"].sudo().search([("mode", "=", "with_code"), ("code", "=ilike", code)], limit=1)
    if rule:
        return rule.program_id
    card = env["loyalty.card"].sudo().search([("code", "=ilike", code)], limit=1)
    if card:
        return card.program_id
    return env["loyalty.program"].sudo()


# ── best-single-discount orchestration ───────────────────────────────────────


class _Rollback(Exception):
    """Sentinel raised to unwind a measurement savepoint (see ``_measure``)."""

    def __init__(self, value):
        self.value = value


def _measure(order, apply_callable):
    """Run ``apply_callable()`` inside a DB savepoint, read the resulting reward
    magnitude, then roll the savepoint back so the live order is untouched.

    Returns ``(magnitude, error)`` where ``error`` is the raw string the callable
    returned (or ``None`` on success). The order recordset cache is invalidated on
    both the inner and outer path so a later read reflects the rolled-back state
    rather than the reward lines the savepoint wrote and discarded. This is the
    "try a discount, keep the number, keep no side effects" primitive behind both
    the preview endpoint and best-single-discount selection (GOL-2431)."""
    try:
        with order.env.cr.savepoint():
            error = apply_callable()
            order.invalidate_recordset()
            magnitude = 0.0 if error else reward_magnitude(order)
            raise _Rollback((magnitude, error))
    except _Rollback as rb:
        order.invalidate_recordset()
        return rb.value


def _goods_tax_ids(order):
    """Union of the tax records the real goods lines carry — the taxes a single
    normalized discount line mirrors so the discount reduces the SAME taxable
    base as the goods (GOL-2450). Empty for an out-of-state (de-taxed) cart, so
    the discount stays untaxed and no WV tax line is emitted downstream. Real
    nursery carts are homogeneous (one WV tax); a mixed-tax cart just gets the
    union, which still collapses to a single line."""
    taxes = order.env["account.tax"]
    for line in order.order_line:
        if line.display_type or not line.product_id or line.reward_id:
            continue
        if line.product_id.default_code == SHIPPING_PRODUCT_CODE:
            continue
        taxes |= line.tax_ids
    return taxes


def _discount_line_name(reward, code):
    """The single discount line's label (GOL-2450). A code-applied reward reads
    ``Discount (CODE)``; an automatic volume tier reads ``Volume discount N%``
    (a non-percent tier, which we never configure, falls back to
    ``Volume discount``)."""
    if code:
        return f"Discount ({code.strip().upper()})"
    if reward.discount_mode == "percent":
        return f"Volume discount {_fmt_percent(reward.discount or 0.0)}"
    return "Volume discount"


def normalize_reward_line(order, reward, code=None):
    """Collapse the reward line(s) sale_loyalty just wrote into ONE headless
    discount line, at pre-tax FACE value, mirroring the goods' taxes (GOL-2450).

    ``sale_loyalty`` renders a fixed-amount reward tax-INCLUSIVE and splits it
    into one negative line per tax group present in the cart — Josh saw "$10 on
    your order" listed twice. The headless Review & pay summary must instead show
    a SINGLE ``Discount`` line at face value, pre-tax, with WV tax then computed
    on the discounted base. We rewrite the reward lines in place:

    * keep exactly ONE line (unlink the rest), still tagged with the original
      ``reward_id``/``coupon_id`` so loyalty bookkeeping and every downstream
      ``reward_id`` filter (magnitude, Stripe coupon) still recognise it;
    * set ``price_unit`` to the negative pre-tax face — ``-amount`` for a fixed
      reward, ``-percent x goods-subtotal`` for a percent tier (Josh: "$10 code
      -> -$10.00; a 10% tier -> -10% of the goods subtotal");
    * mirror the goods' taxes onto it so Odoo taxes ``(goods - discount +
      shipping)`` at the WV rate, and nothing out of state;
    * label it ``Discount (CODE)`` / ``Volume discount N%``.

    Never raises: a loyalty/ORM hiccup logs and leaves sale_loyalty's own reward
    lines intact (the pre-GOL-2450 behaviour) rather than breaking checkout."""
    try:
        reward_lines = order.order_line.filtered(lambda ln: ln.reward_id and not ln.display_type)
        if not reward_lines:
            return
        subtotal = goods_subtotal(order)
        if reward.discount_mode == "percent":
            amount = round((reward.discount or 0.0) / 100.0 * subtotal, 2)
        else:
            # Fixed per-order/per-point face, capped at the goods subtotal so the
            # discount never drives the taxable base negative.
            amount = round(min(reward.discount or 0.0, subtotal), 2)
        survivor = reward_lines[:1]
        extras = reward_lines - survivor
        # Rewrite the survivor BEFORE unlinking the extras: writing a line after
        # sibling lines were unlinked makes sale_stock's write recompute browse a
        # now-deleted line (MissingError). Capture the mirrored taxes first for
        # the same reason.
        tax_ids = _goods_tax_ids(order).ids
        survivor.write(
            {
                "name": _discount_line_name(reward, code),
                "product_uom_qty": 1.0,
                "price_unit": -amount,
                "tax_ids": [(6, 0, tax_ids)],
            }
        )
        if extras:
            extras.unlink()
        order.invalidate_recordset(["order_line", "amount_untaxed", "amount_tax", "amount_total"])
    except Exception:  # noqa: BLE001 — a discount-shaping gap must never break checkout
        _logger.warning("GOL-2450: could not normalize reward line to a single discount", exc_info=True)


def apply_code_reward(order, code):
    """Register + apply a promo/loyalty code's discount reward(s) to ``order``.

    Returns ``None`` on success (a reward line now exists), or a raw shopper-
    facing error string when the code is unknown, ineligible, expired or already
    applied — the caller refines it with :func:`explain_code_failure`. Mirrors the
    ``website_sale_loyalty`` coupon flow; a reward needing a product choice we
    can't make headless is rejected rather than guessed at (GOL-2088)."""
    result = order._try_apply_code(code)
    if not isinstance(result, dict):
        return "This promo code can't be applied to your cart."
    if result.get("error"):
        return result["error"]
    applied = False
    discount_reward = None
    for coupon, rewards in result.items():
        for reward in rewards:
            if reward.multi_product:
                return "This promo needs a product choice we can't make at checkout — please contact us to redeem it."
            status = order._apply_program_reward(reward, coupon)
            if isinstance(status, dict) and status.get("error"):
                return status["error"]
            applied = True
            if reward.reward_type == "discount":
                discount_reward = reward
    if not applied:
        return "This code isn't valid for the items in your cart."
    # Collapse sale_loyalty's per-tax-group split into one pre-tax discount line
    # (GOL-2450). One code = one discount reward in our programs.
    if discount_reward is not None:
        normalize_reward_line(order, discount_reward, code=code)
    return None


def _apply_reward(order, pair):
    """Apply a single ``(reward, coupon)`` pair; returns an error string or None."""
    reward, coupon = pair
    status = order._apply_program_reward(reward, coupon)
    if isinstance(status, dict) and status.get("error"):
        return status["error"]
    # Automatic volume tier: same single pre-tax discount line as a code (GOL-2450).
    if reward.reward_type == "discount":
        normalize_reward_line(order, reward, code=None)
    return None


def _claimable_auto_rewards(order):
    """``(reward, coupon)`` pairs claimable right now from the order's AUTOMATIC
    programs. Registers points first via ``_update_programs_and_rewards()``. A
    loyalty-engine hiccup logs and yields no tier rather than breaking checkout."""
    try:
        order._update_programs_and_rewards()
        claimable = order._get_claimable_rewards()
    except Exception:  # noqa: BLE001 — a loyalty gap must never break checkout
        _logger.warning("GOL-2431: automatic-reward computation failed", exc_info=True)
        return []
    pairs = []
    for coupon, rewards in claimable.items():
        program = coupon.program_id
        if not program or program.trigger != "auto":
            continue
        for reward in rewards:
            if reward.reward_type == "discount" and not reward.multi_product:
                pairs.append((reward, coupon))
    return pairs


def _best_auto_reward(order):
    """The single highest-value claimable automatic discount reward, or ``None``.

    "Best single discount wins" among tiers too: when both the 5+ and the 10+
    reward are claimable we keep only the richer one — lower tiers are never
    stacked. Ranked by an analytic estimate (all tiers share the same order base,
    so the ranking is exact even though the estimate isn't)."""
    pairs = _claimable_auto_rewards(order)
    if not pairs:
        return None
    return max(pairs, key=lambda rc: _analytic_value(order, rc[0]))


def _fmt_percent(value):
    """ "20%" for a whole percent, "12.5%" otherwise."""
    value = round(value, 2)
    return f"{int(value)}%" if value == int(value) else f"{value:g}%"


def _applied_message(applied, order, code, tier_value, code_value, reward=None):
    """The shopper-facing note on which discount applied and why (GOL-2431)."""
    shown = code.upper() if code else None
    if applied == "tier":
        pct = _fmt_percent(reward.discount) if reward is not None and reward.discount_mode == "percent" else None
        label = f"{pct} volume discount" if pct else "volume discount"
        if code and code_value > 0:
            return f"Your {label} is worth more than {shown}, so we applied that."
        return f"{label[:1].upper()}{label[1:]} applied."
    if applied == "code":
        if tier_value > 0:
            return f"{shown} is worth more than your volume discount, so we applied it."
        return f"{shown} applied."
    return None


def _tier_descriptor(pair):
    """``{min_qty, percent}`` for an applied automatic volume tier (GOL-2439) —
    the numbers the storefront turns into "Volume discount (10% for 5+ trees)".
    Only a percent-off reward carries a percent; a non-percent reward has no
    "% for N+ trees" reading and yields ``None`` (the field is then omitted)."""
    reward, coupon = pair
    if reward.discount_mode != "percent":
        return None
    program = coupon.program_id
    return {"min_qty": _reward_min_units(program, reward), "percent": reward.discount}


def _result(ok, applied, code, discount_amount, subtotal_after, message, tier=None):
    result = {
        "ok": ok,
        "applied": applied,
        "code": code.upper() if code else None,
        "discount_amount": round(discount_amount, 2),
        "subtotal_after": round(subtotal_after, 2),
        "message": message,
    }
    # Optional volume-tier descriptor: present only when a tier applied, so the
    # storefront can label the summary row without re-deriving the threshold.
    if tier is not None:
        result["tier"] = tier
    return result


def resolve_discounts(order, code=None, today=None):
    """Apply the best single discount to ``order`` and return the outcome.

    Best single discount wins: the automatic volume tier and the promo code are
    each MEASURED (applied in a savepoint, valued, rolled back), and then only the
    larger is applied for real — the other's lines are never written. A tie
    favours the code (an explicit shopper action). Returns::

        {ok, applied: "code"|"tier"|None, code, discount_amount, subtotal_after,
         message, tier?: {min_qty, percent}}

    ``tier`` is present only when ``applied == "tier"`` (and the reward is a
    percent-off) — the storefront labels the summary row "Volume discount (10%
    for 5+ trees)" from it.

    Mutates ``order`` by adding at most one reward line. The caller has already
    refused deposit/preorder carts (same predicate as the promo gate), so no
    deposit cart reaches here. ``today`` is injected for deterministic tests."""
    env = order.env
    if today is None:
        today = _date.today()
    code = (code or "").strip() or None
    subtotal = goods_subtotal(order)

    tier_pair = _best_auto_reward(order)
    tier_value = 0.0
    tier = None
    if tier_pair:
        tier_value, _tier_err = _measure(order, lambda: _apply_reward(order, tier_pair))

    code_value = 0.0
    code_error = None
    if code:
        code_value, raw_err = _measure(order, lambda: apply_code_reward(order, code))
        if raw_err:
            code_error = explain_code_failure(env, order, code, raw_err, today)

    if code_value > 0 and code_value >= tier_value:
        err = apply_code_reward(order, code)
        if err:  # defensive: the measurement said it would apply cleanly
            return _result(False, None, code, 0.0, subtotal, explain_code_failure(env, order, code, err, today))
        applied = "code"
        message = _applied_message("code", order, code, tier_value, code_value)
    elif tier_value > 0:
        _apply_reward(order, tier_pair)
        applied = "tier"
        tier = _tier_descriptor(tier_pair)
        message = _applied_message("tier", order, code, tier_value, code_value, reward=tier_pair[0])
    else:
        applied = None
        message = code_error  # None when no code was supplied

    order.invalidate_recordset(["amount_untaxed", "amount_tax", "amount_total"])
    discount_amount = reward_magnitude(order) if applied else 0.0
    return _result(True, applied, code, discount_amount, round(subtotal - discount_amount, 2), message, tier=tier)
