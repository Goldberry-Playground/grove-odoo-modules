"""Preorder-deposit email copy (GOL-1666). Pure Python / stdlib so the ratified
voice unit-tests without an Odoo DB (mirrors stripe_gateway / shipping_calendar).

The dollar figure is derived from the single source of truth
(``stripe_gateway.PREORDER_DEPOSIT``) so the confirmation email, the pre-ship
email, the checkout ``kind: "deposit"`` line item, and the storefront copy can
never drift to different numbers.

Voice ratified by the board (GOL-1189 / GOL-1302, supersedes the old 25% copy):
a flat deposit per tree today, the balance settled when the tree ships. No em
dashes anywhere in customer-facing copy (house voice rule).
"""

from .stripe_gateway import PREORDER_DEPOSIT, SEED_DEPOSIT

# Only the two dormant-bareroot mailing seasons carry a season word; anything
# else (unknown zone, peat & bagged) falls back to the season-less phrasing.
_SEASON_PHRASE = {"spring": "this spring", "fall": "this fall"}


def deposit_amount_label(amount=PREORDER_DEPOSIT):
    """ "$10" for a whole-dollar deposit, "$10.50" otherwise (no trailing .00)."""
    amount = float(amount)
    if amount == int(amount):
        return f"${int(amount)}"
    return f"${amount:.2f}"


def confirmation_deposit_line(season=None, amount=PREORDER_DEPOSIT):
    """One-line preorder-deposit explainer for the order-confirmation email.

    Ratified voice, e.g.:
      "$10 deposit per tree today, balance when your tree ships this spring."
    Falls back to season-less "balance when your tree ships." when the
    destination zone (and therefore the ship season) is unknown.
    """
    label = deposit_amount_label(amount)
    when = _SEASON_PHRASE.get(season)
    tail = f"balance when your tree ships {when}" if when else "balance when your tree ships"
    return f"{label} deposit per tree today, {tail}."


def preship_balance_line(season=None, amount=PREORDER_DEPOSIT):
    """Balance reminder for the pre-ship ("your order has shipped") email.

    States the standing arrangement rather than asserting a specific completed
    charge, because off-session balance capture is a separate step: the deposit
    was taken at checkout and the balance settles on the saved card as the trees
    ship. Kept consistent with the confirmation promise above.
    """
    label = deposit_amount_label(amount)
    when = _SEASON_PHRASE.get(season, "as your trees ship")
    return (
        f"This was a preorder. You paid a {label} deposit per tree at checkout; "
        f"the remaining balance settles on your saved card {when}."
    )


# ── Seed pre-orders (GOL-3257 §8) ───────────────────────────────────────────
# Seeds use the SAME deposit + ship-time-settlement arrangement as the tree
# pre-orders, but the shopper pays one $1 deposit per order (not per tree), the
# balance is "the rest of the pack price, shipping and handling" (seeds are not
# quoted at checkout), and the thing we promise is a harvest year and a ship
# window, not a spring/fall tree wave. House voice rule still applies: no em
# dashes anywhere in customer-facing copy.


def _ship_window_phrase(ship_start=None, ship_end=None):
    """ "ships approx Oct 15 to Nov 15" from two dates, or "" when either is
    missing. Dates are anything with ``strftime`` (``datetime.date`` / Odoo
    Date); formatted as "Mon D" with no leading zero on the day."""
    if not ship_start or not ship_end:
        return ""

    def _fmt(d):
        return f"{d.strftime('%b')} {d.day}"

    return f"ships approx {_fmt(ship_start)} to {_fmt(ship_end)}"


def seed_confirmation_line(harvest_year=None, ship_start=None, ship_end=None, amount=SEED_DEPOSIT):
    """One-line seed-deposit explainer for the order-confirmation email (§8).

    e.g. "$1 deposit today for your fall 2026 harvest (ships approx Oct 15 to
    Nov 15). The rest of the pack price, shipping and handling are charged when
    it ships." Drops the harvest clause when the year is unknown and the ship
    window when the dates are missing, so a half-configured product still sends.
    """
    label = deposit_amount_label(amount)
    harvest = f" for your fall {int(harvest_year)} harvest" if harvest_year else ""
    window = _ship_window_phrase(ship_start, ship_end)
    window = f" ({window})" if window else ""
    return (
        f"{label} deposit today{harvest}{window}. The rest of the pack price, "
        f"shipping and handling are charged when it ships."
    )


def seed_preship_balance_line(harvest_year=None, amount=SEED_DEPOSIT):
    """Balance reminder for the seed pre-ship email (§8). States the standing
    arrangement rather than a completed charge (settlement is a separate step),
    matching ``preship_balance_line``'s honesty for trees."""
    label = deposit_amount_label(amount)
    harvest = f" from the fall {int(harvest_year)} harvest" if harvest_year else ""
    return (
        f"This was a seed pre-order{harvest}. You paid a {label} deposit at "
        f"checkout; the rest of the pack price, shipping and handling settle on "
        f"your saved card when it ships."
    )
