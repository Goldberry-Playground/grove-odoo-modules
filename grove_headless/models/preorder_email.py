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

from .stripe_gateway import PREORDER_DEPOSIT

# Only the two dormant-bareroot mailing seasons carry a season word; anything
# else (unknown zone, peat & bagged) falls back to the season-less phrasing.
_SEASON_PHRASE = {"spring": "this spring", "fall": "this fall"}


def deposit_amount_label(amount=PREORDER_DEPOSIT):
    """"$10" for a whole-dollar deposit, "$10.50" otherwise (no trailing .00)."""
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
