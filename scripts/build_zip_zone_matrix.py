#!/usr/bin/env python3
"""Build grove_headless/data/zip_usda_zone.csv from the USDA 2023 PHZM.

Source: PRISM Climate Group ZIP-code hardiness dataset (USDA PHZM 2023).
Primary URL below; if it 404s, find the "zipcode" CSV link on
https://prism.oregonstate.edu/phzm/ and pass it as argv[1].

The PHZM zipcode file has columns: zipcode, zone (e.g. "6b"), trange,
zonetitle — no state column.  A secondary ZIP→state reference (from
github.com/scpike/us-state-county-zip) is fetched to filter down to the
21 green states.

Output rows: zip,zone (integer zone, half-zone letter stripped),
trimmed to the 21 green states.

Run once per PHZM release: python3 scripts/build_zip_zone_matrix.py
"""

import csv
import io
import sys
import urllib.request

PRIMARY_URL = "https://prism.oregonstate.edu/phzm/data/2023/phzm_us_zipcode_2023.csv"
# pinned to a commit SHA so schema drift can't silently change the build
ZIP_STATE_URL = (
    "https://raw.githubusercontent.com/scpike/us-state-county-zip/8bd38a600ec137bb0162c0761da4ea3de3eb951f/geo-data.csv"
)
GREEN = {
    "CT",
    "DE",
    "IL",
    "IN",
    "KY",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "NH",
    "NJ",
    "NY",
    "NC",
    "OH",
    "PA",
    "RI",
    "VT",
    "VA",
    "WV",
    "WI",
}
OUT = "grove_headless/data/zip_usda_zone.csv"


def _fetch(url: str, label: str) -> str:
    print(f"fetching {label}…", file=sys.stderr)
    return urllib.request.urlopen(url, timeout=60).read().decode("utf-8", "replace")


def main() -> int:
    phzm_url = sys.argv[1] if len(sys.argv) > 1 else PRIMARY_URL

    # 1. Build ZIP→state lookup from reference dataset.
    zip_state_raw = _fetch(ZIP_STATE_URL, "ZIP→state reference")
    zip_to_state: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(zip_state_raw)):
        zc = row.get("zipcode", "").strip().zfill(5)
        st = row.get("state_abbr", "").strip().upper()
        if zc and st:
            zip_to_state[zc] = st

    # 2. Fetch PHZM ZIP-code dataset.
    phzm_raw = _fetch(phzm_url, "PHZM dataset")
    reader = csv.DictReader(io.StringIO(phzm_raw))
    # Columns: zipcode, zone (e.g. "6b"), trange, zonetitle — detect flexibly.
    fields = {f.lower(): f for f in reader.fieldnames}
    zip_col = fields.get("zipcode") or fields.get("zip")
    zone_col = fields.get("zone")
    if not (zip_col and zone_col):
        print(f"unexpected PHZM columns: {reader.fieldnames}", file=sys.stderr)
        return 2

    rows = []
    for r in reader:
        zc = r[zip_col].strip().zfill(5)
        state = zip_to_state.get(zc, "")
        if state not in GREEN:
            continue
        digits = "".join(ch for ch in r[zone_col] if ch.isdigit())
        if not digits:
            continue
        rows.append((zc, int(digits)))
    rows.sort()

    if len(rows) < 10_000:
        print(
            f"ERROR: only {len(rows)} rows built — aborting (expected ~15k). "
            "Check the secondary dataset's column names / availability.",
            file=sys.stderr,
        )
        return 1

    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["zip", "zone"])
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
