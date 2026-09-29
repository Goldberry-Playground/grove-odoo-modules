#!/usr/bin/env python3
"""Rate-table freshness guard — is shipping_rates.json still trustworthy? (GOL-2641)

This guard is deliberately **independent of the quote source**. `rate_check.py`
answers "can we reach a rate source right now"; this answers the money question
"how long have we been billing customers off numbers nobody re-verified". The
two fail for different reasons and must not share an alarm: when the quote
source is down, rate-check is red EVERY day and carries no new information,
while this check stays green until a dated deadline and then goes red once —
a green->red transition a human can actually act on.

Freshness comes from the `_rates_verified_on` stamp that `rate_check.py` writes
on every successful full rewrite (`YYYY-MM-DD`, the probe date). Underscore keys
are invisible to the Odoo loader (`shipping_zones._load_rates` filters them), so
the stamp is pure provenance and can never move a published rate.

Verdicts (`assess`) and exit codes:

* `fresh`       -> 0. Age below the warn threshold.
* `aging`       -> 0 + `::warning::`. Past warn, not yet past fail; refresh soon.
* `stale`       -> 1 + `::error::`. Past fail. A carrier increase in this window
                   is very likely already under-billing every ship-to order.
* `unstamped`   -> 1 + `::error::`. A real published table with no stamp: we
                   cannot PROVE freshness, so we refuse to claim it (same
                   fail-safe rule as the GOL-1312 all-missing lapse guard).
* `provisional` -> 0. The `_provisional` launch placeholder is not yet real
                   pricing, so it cannot be stale (mirrors rate_check.py).

Default thresholds: warn at 14 days, fail at 28. A UPS/USPS general rate
increase is a few percent, which on a $20-$27 parcel clears the $1 drift
threshold `rate_check.compute_drift` uses — so a month of silence is a month of
plausible under-billing.
"""

import argparse
import json
import os
import sys
from datetime import date

RATES_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "grove_headless", "data", "shipping_rates.json")
STAMP_KEY = "_rates_verified_on"
WARN_AGE_DAYS = 14
FAIL_AGE_DAYS = 28


def verified_on(doc: dict):
    """The `_rates_verified_on` stamp as a date, or None when absent/unparsable.

    An unparsable stamp is treated as ABSENT rather than raising: a typo in the
    provenance field must not crash the guard into a green-by-exception, it must
    fall through to the `unstamped` fail-safe."""
    raw = doc.get(STAMP_KEY)
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def has_published_rates(doc: dict) -> bool:
    """True when the doc carries at least one real zone with at least one cell."""
    return any(bool(v) for k, v in doc.items() if not k.startswith("_"))


def age_days(doc: dict, today: date):
    """Days since the table was last verified, or None when unstamped.

    A stamp in the FUTURE clamps to 0 rather than going negative — a clock skew
    or a hand-typed date must not read as "extra fresh"."""
    stamp = verified_on(doc)
    if stamp is None:
        return None
    return max(0, (today - stamp).days)


def assess(doc: dict, today: date, warn_days: int = WARN_AGE_DAYS, fail_days: int = FAIL_AGE_DAYS) -> dict:
    """{verdict, age, stamp, exit_code, message} for a rates doc.

    Pure and date-injectable so the whole matrix is testable offline."""
    if doc.get("_provisional"):
        return {
            "verdict": "provisional",
            "age": None,
            "stamp": verified_on(doc),
            "exit_code": 0,
            "message": "rate table is the _provisional launch placeholder — not real pricing, cannot be stale",
        }
    age = age_days(doc, today)
    if age is None:
        if not has_published_rates(doc):
            return {
                "verdict": "provisional",
                "age": None,
                "stamp": None,
                "exit_code": 0,
                "message": "rate table holds no published rates — nothing to keep fresh",
            }
        return {
            "verdict": "unstamped",
            "age": None,
            "stamp": None,
            "exit_code": 1,
            "message": (
                f"rate table holds real published rates but carries no usable `{STAMP_KEY}` — "
                "freshness cannot be proven, so it is not assumed (GOL-2641)"
            ),
        }
    stamp = verified_on(doc)
    if age >= fail_days:
        return {
            "verdict": "stale",
            "age": age,
            "stamp": stamp,
            "exit_code": 1,
            "message": (
                f"shipping rates last verified {stamp.isoformat()} — {age} days ago, over the "
                f"{fail_days}-day limit. Every ship-to order is priced off unverified numbers; a "
                "carrier increase in this window is silently under-billing. Refresh the table "
                "(scripts/rate_check/RUNBOOK.md -> 'Refreshing the table without a quote source')"
            ),
        }
    if age >= warn_days:
        return {
            "verdict": "aging",
            "age": age,
            "stamp": stamp,
            "exit_code": 0,
            "message": (
                f"shipping rates last verified {stamp.isoformat()} — {age} days ago; hard limit is "
                f"{fail_days} days ({fail_days - age} left). Refresh before this goes red"
            ),
        }
    return {
        "verdict": "fresh",
        "age": age,
        "stamp": stamp,
        "exit_code": 0,
        "message": f"shipping rates verified {stamp.isoformat()} — {age} days ago (limit {fail_days})",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fail when shipping_rates.json is too old to trust.")
    ap.add_argument("--rates-file", default=RATES_PATH, help="rates JSON to check (default: the shipped table)")
    ap.add_argument("--warn-age-days", type=int, default=WARN_AGE_DAYS)
    ap.add_argument("--max-age-days", type=int, default=FAIL_AGE_DAYS, help="age at which the guard fails (exit 1)")
    ap.add_argument("--today", help="YYYY-MM-DD override for the reference date (testing)")
    args = ap.parse_args(argv)

    today = date.fromisoformat(args.today) if args.today else date.today()
    with open(args.rates_file, encoding="utf-8") as fh:
        doc = json.load(fh)

    result = assess(doc, today, warn_days=args.warn_age_days, fail_days=args.max_age_days)
    annotation = {"stale": "::error::", "unstamped": "::error::", "aging": "::warning::"}.get(result["verdict"], "")
    stream = sys.stderr if result["exit_code"] else sys.stdout
    print(f"{annotation}rate-table freshness [{result['verdict']}] {result['message']}", file=stream)
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
