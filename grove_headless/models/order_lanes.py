"""Pure helpers for the Discord order lanes and hold dates.

Stdlib only (no ``odoo`` import) so it is unit-testable under plain pytest,
like ``order_alerts`` and ``shipping_calendar``. Never does I/O.
"""

import math
import re
from datetime import date

_MONTHS = {
    name: idx
    for idx, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}

_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_NUMERIC_RE = re.compile(r"^(\d{1,2})/(\d{1,2})$")
_NAMED_RE = re.compile(r"^([A-Za-z]+)\.?\s+(\d{1,2})$")
_TRACKING_RE = re.compile(r"^(1Z[A-Z0-9]{16}|[0-9]{20,34})$")

MAX_LABEL_COST = 250.00


def lane_for(fulfillment, is_printify, ship_wave):
    """Lane an order belongs to: ``spring`` | ``individual`` | ``none``.

    ``none`` = not ours to ship from the nursery (pickup, or Printify POD).
    ``spring`` = a spring-wave order; every other shipped order is ``individual``."""
    if fulfillment != "ship" or is_printify:
        return "none"
    return "spring" if ship_wave == "spring" else "individual"


def parse_hold_date(text, today):
    """Parse staff input ("Oct 25", "10/25", "2027-03-01") into a date.

    A month/day without a year means this year, or next year if that date has
    already passed. A full ISO date in the past is an error. Raises ValueError."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Enter a date like Oct 25, 10/25 or 2027-03-01.")

    iso = _ISO_RE.match(raw)
    if iso:
        try:
            parsed = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            raise ValueError(f"{raw!r} is not a real date.") from None
        if parsed < today:
            raise ValueError(f"{raw} is in the past.")
        return parsed

    numeric = _NUMERIC_RE.match(raw)
    named = _NAMED_RE.match(raw)
    if numeric:
        month, day = int(numeric.group(1)), int(numeric.group(2))
    elif named and named.group(1).lower() in _MONTHS:
        month, day = _MONTHS[named.group(1).lower()], int(named.group(2))
    else:
        raise ValueError("Enter a date like Oct 25, 10/25 or 2027-03-01.")

    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    raise ValueError(f"{raw!r} is not a real date.")


def suggest_hold_date(today, dormancy_start, zone_start):
    """Default hold date: the later of dormancy start and the zone's ship
    start (when known), never earlier than today."""
    return max(today, dormancy_start, zone_start or dormancy_start)


def clean_tracking(text):
    """Strip all whitespace from a pasted tracking number."""
    return re.sub(r"\s+", "", text or "")


def parse_tracking(text):
    """Check a typed tracking number and return it in upper case, without spaces.

    A UPS number is 1Z and 18 characters in all; anything else must be the 20 to
    34 digits of a USPS number. Stricter than ``shippo_client.is_valid_tracking``
    so a typo is caught before it reaches a customer. Raises ValueError."""
    typed = clean_tracking(text)
    if not typed:
        raise ValueError("Enter the tracking number.")
    number = typed.upper()
    if not _TRACKING_RE.match(number):
        raise ValueError(f"{typed} doesn't look like a UPS or USPS tracking number. Check it and try again.")
    return number


def parse_label_cost(text):
    """Parse a label cost like "$12.40" into a positive float no higher than
    ``MAX_LABEL_COST``, because a dropped decimal would otherwise be billed to the
    customer off-session. Raises ValueError."""
    raw = re.sub(r"[\s,$]", "", str(text or ""))
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("Enter the label cost as a number, like 12.40.") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Label cost must be greater than zero.")
    if value > MAX_LABEL_COST:
        raise ValueError(
            f"${value:,.2f} is over the ${MAX_LABEL_COST:.2f} limit for a label. Double-check the decimal."
        )
    return value
