#!/usr/bin/env python3
"""GOL-3074 data remediation: strip the stray stock '15%' tax (account.tax id 6)
from eight out-of-state orders so Odoo's totals match what the customer actually
kept.

Background
----------
Grove's only sales-tax nexus is WV, so every non-WV ship-to must be $0 tax. Eight
legacy orders carried the stock Odoo "15%" tax (account.tax id 6, company 1,
amount_type percent, amount 15) on their shipping and/or goods lines and the
customer was charged it. The code fix (controllers/main.py ``_apply_destination_tax``
now strips EVERY tax out of state, not only WV-named ones) stops this for new
checkouts and future settlements. This one-shot migration repairs the EXISTING
rows.

Josh already issued the Stripe refunds (tax-only partial refunds, 2026-10-05 23:39Z,
total $30.65) and each order already carries a "Refunded $X sales tax charged in
error ... GOL-3074" chatter note. The orders' lines still carry tax id 6 and
``amount_total`` still includes it, so this script removes tax id 6 from those
lines to make Odoo match the post-refund reality.

The refunded tax amount is the SOURCE OF TRUTH: the script asserts the tax it is
about to remove from each order equals the refunded amount (±$0.01) before writing,
and aborts the whole run if any order disagrees — so a shifted id or an unexpected
line can never silently mutate the wrong data.

⚠️ This script does NOT re-run settlement, confirm, or re-price anything. It only
removes tax id 6 from the order lines and recomputes totals. Do not run any
settlement on these orders (they are already paid/settled and refunded).

Usage
-----
    ODOO_URL=http://localhost:8069 ODOO_DB=odoo \\
    ODOO_USER=josh@goldberrygrove.farm ODOO_PASSWORD=<admin> \\
    DRY_RUN=1 python3 scripts/gol3074_strip_destination_tax.py   # plan only
    # DRY_RUN unset -> removes tax id 6 from the lines and posts a chatter note.

Idempotency: re-running is safe. An order whose lines no longer carry tax id 6 is
reported as already-remediated and skipped (no second chatter note).

Exit codes: 0 ok, 1 auth/validation failure (any mismatch aborts before any write).
"""

from __future__ import annotations

import os
import sys
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL", "http://localhost:8069")
ODOO_DB = os.getenv("ODOO_DB", "odoo")
ODOO_USER = os.getenv("ODOO_USER", "josh@goldberrygrove.farm")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
DRY_RUN = os.getenv("DRY_RUN") == "1"

# The stray tax to remove, with its expected identity so the run fails loudly if
# the Chart-of-Accounts ids have shifted since GOL-3074 was filed.
STRAY_TAX_ID = int(os.getenv("STRAY_TAX_ID", "6"))
STRAY_TAX_NAME = "15%"
STRAY_TAX_AMOUNT = 15.0

# order name -> refunded tax (the source of truth, from Josh's 2026-10-05 refund
# run). The script asserts the tax it removes equals this per order.
REFUNDED_TAX = {
    "S00223": 2.55,  # PA — shipping
    "S00232": 6.15,  # ME — shipping
    "S00235": 2.55,  # VA — shipping
    "S00241": 8.55,  # VA — shipping ($2.55) + Shagbark Hickory goods ($6.00)
    "S00245": 2.07,  # KY — shipping (settlement 2026-10-05)
    "S00250": 3.00,  # MD — shipping
    "S00253": 3.00,  # OH — shipping
    "S00303": 2.78,  # NC — shipping (settlement 2026-10-05)
}

CENT = 0.01


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def authenticate() -> tuple[xmlrpc.client.ServerProxy, int]:
    if not ODOO_PASSWORD:
        fail("ODOO_PASSWORD env var is required")
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        fail(f"Authentication failed for user {ODOO_USER} on db {ODOO_DB}")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    print(f"Authenticated as uid={uid} on db={ODOO_DB}")
    return models, uid


def call(models, uid, model, method, args, kwargs=None):
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kwargs or {})


def verify_stray_tax(models, uid) -> None:
    """Fail loudly unless account.tax id STRAY_TAX_ID is still the stock 15% tax."""
    rows = call(models, uid, "account.tax", "read", [[STRAY_TAX_ID]], {"fields": ["name", "amount", "amount_type"]})
    if not rows:
        fail(f"account.tax id {STRAY_TAX_ID} does not exist — ids have shifted; aborting")
    tax = rows[0]
    if tax["name"] != STRAY_TAX_NAME or abs(tax["amount"] - STRAY_TAX_AMOUNT) > CENT or tax["amount_type"] != "percent":
        fail(
            f"account.tax id {STRAY_TAX_ID} is {tax!r}, not the stock "
            f"'{STRAY_TAX_NAME}' {STRAY_TAX_AMOUNT}% percent tax — aborting rather than mutating the wrong data"
        )
    print(f"Confirmed stray tax id {STRAY_TAX_ID} = {tax['name']} ({tax['amount']}% {tax['amount_type']}).")


def plan_order(models, uid, name: str):
    """Return (order_id, [line_ids carrying the stray tax], removed_tax_amount) or
    (order_id, [], 0.0) if already remediated. Aborts on any surprise."""
    orders = call(
        models,
        uid,
        "sale.order",
        "search_read",
        [[["name", "=", name]]],
        {"fields": ["id", "amount_tax", "amount_total", "state"]},
    )
    if not orders:
        fail(f"order {name} not found")
    if len(orders) > 1:
        fail(f"order name {name} is ambiguous ({len(orders)} matches) — aborting")
    order = orders[0]
    order_id = order["id"]

    lines = call(
        models,
        uid,
        "sale.order.line",
        "search_read",
        [[["order_id", "=", order_id], ["tax_ids", "in", [STRAY_TAX_ID]]]],
        {"fields": ["id", "name", "price_subtotal", "tax_ids"]},
    )
    if not lines:
        print(f"  {name}: no line carries tax id {STRAY_TAX_ID} — already remediated, skipping.")
        return order_id, [], 0.0

    # The tax we will remove = STRAY_TAX_AMOUNT% of those lines' pre-tax subtotal.
    removed = round(sum(line["price_subtotal"] for line in lines) * STRAY_TAX_AMOUNT / 100.0, 2)
    expected = REFUNDED_TAX[name]
    if abs(removed - expected) > CENT:
        fail(
            f"{name}: tax id {STRAY_TAX_ID} on its lines totals ${removed:.2f}, but Josh refunded "
            f"${expected:.2f}. These must match (refund is the source of truth) — aborting."
        )
    print(
        f"  {name} (state={order['state']}, amount_tax=${order['amount_tax']:.2f}): "
        f"removing tax id {STRAY_TAX_ID} from {len(lines)} line(s), -${removed:.2f} tax."
    )
    return order_id, [line["id"] for line in lines], removed


def main() -> None:
    models, uid = authenticate()
    verify_stray_tax(models, uid)

    print(f"\n{'DRY RUN — ' if DRY_RUN else ''}Planning {len(REFUNDED_TAX)} orders...")
    plans = []
    total_remove = 0.0
    for name in REFUNDED_TAX:
        order_id, line_ids, removed = plan_order(models, uid, name)
        if line_ids:
            plans.append((name, order_id, line_ids, removed))
            total_remove += removed

    print(f"\n{len(plans)} order(s) need remediation, total tax to remove: ${round(total_remove, 2):.2f}")
    if not plans:
        print("Nothing to do — all orders already remediated.")
        return
    if DRY_RUN:
        print("DRY RUN — no writes made. Unset DRY_RUN to apply.")
        return

    for name, order_id, line_ids, removed in plans:
        # Remove ONLY the stray tax (command 3) — WV tax, if any, is untouched.
        call(models, uid, "sale.order.line", "write", [line_ids, {"tax_ids": [(3, STRAY_TAX_ID)]}])
        note = (
            f"GOL-3074 data fix: removed the stock '15%' tax (account.tax id {STRAY_TAX_ID}) from "
            f"{len(line_ids)} line(s) — ${removed:.2f} of tax that should never have applied to this "
            f"out-of-state order. The tax-only Stripe refund was already issued; this makes the Odoo "
            f"total match what the customer actually paid. Settlement was NOT re-run."
        )
        call(models, uid, "sale.order", "message_post", [[order_id]], {"body": note})
        print(f"  {name}: removed tax id {STRAY_TAX_ID} (-${removed:.2f}) and posted a chatter note.")

    print(f"\nDone. Remediated {len(plans)} order(s), removed ${round(total_remove, 2):.2f} of stray tax.")


if __name__ == "__main__":
    main()
