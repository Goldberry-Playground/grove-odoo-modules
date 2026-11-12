#!/usr/bin/env python3
"""Build grove_headless/data/zip_usda_zone.csv from the USDA 2023 PHZM.

Source: PRISM Climate Group ZIP-code hardiness dataset (USDA PHZM 2023).
Primary URL below; if it 404s, find the "zipcode" CSV link on
https://prism.oregonstate.edu/phzm/ and pass it as argv[1].

The PHZM zipcode file has columns: zipcode, zone (e.g. "6b"), trange,
zonetitle — no state column.  A secondary ZIP→state reference (from
github.com/scpike/us-state-county-zip) is fetched to filter down to the
31 green states.  A third source, the US Census 2023 ZCTA Gazetteer, supplies
each ZIP's point-internal lat/lon centroid (GOL-3575 C.7: the consult travel
estimator measures straight-line distance from the farm ZIP centroid).


Output rows: zip,zone,lat,lon — integer zone (half-zone letter stripped),
trimmed to the 31 green states; lat/lon are the ZCTA centroid (blank for the
few ZIPs the Gazetteer has no ZCTA for).

Run once per PHZM release: python3 scripts/build_zip_zone_matrix.py
"""

import csv
import io
import sys
import urllib.request
import zipfile

PRIMARY_URL = "https://prism.oregonstate.edu/phzm/data/2023/phzm_us_zipcode_2023.csv"
# pinned to a commit SHA so schema drift can't silently change the build
ZIP_STATE_URL = (
    "https://raw.githubusercontent.com/scpike/us-state-county-zip/8bd38a600ec137bb0162c0761da4ea3de3eb951f/geo-data.csv"
)
# US Census 2023 ZCTA Gazetteer — tab-separated: GEOID (ZCTA5), ..., INTPTLAT,
# INTPTLONG. Published yearly; the national file is a ~1 MB zip of one .txt.
GAZETTEER_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_zcta_national.zip"
# Must mirror shipping_zones.GREEN_STATES (this standalone script keeps its own
# pinned copy so it needs no addon import — update both together, GOL-2128).
GREEN = {
    "AL",
    "AR",
    "CT",
    "DC",
    "DE",
    "GA",
    "IA",
    "IL",
    "IN",
    "KY",
    "LA",
    "MA",
    "MD",
    "ME",
    "MI",
    "MN",
    "MO",
    "MS",
    "NC",
    "NH",
    "NJ",
    "NY",
    "OH",
    "PA",
    "RI",
    "SC",
    "TN",
    "VA",
    "VT",
    "WI",
    "WV",
}
OUT = "grove_headless/data/zip_usda_zone.csv"


def _fetch(url: str, label: str) -> str:
    print(f"fetching {label}…", file=sys.stderr)
    return urllib.request.urlopen(url, timeout=60).read().decode("utf-8", "replace")


def _zcta_centroids() -> dict[str, tuple[str, str]]:
    """``{zip5: (lat, lon)}`` from the Census ZCTA Gazetteer (point-internal)."""
    print("fetching ZCTA Gazetteer…", file=sys.stderr)
    raw = urllib.request.urlopen(GAZETTEER_URL, timeout=60).read()
    zf = zipfile.ZipFile(io.BytesIO(raw))
    out: dict[str, tuple[str, str]] = {}
    with zf.open(zf.namelist()[0]) as fh:
        reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8"), delimiter="\t")
        cols = {c.strip().lower(): c for c in reader.fieldnames}
        gid, lat_c, lon_c = cols["geoid"], cols["intptlat"], cols["intptlong"]
        for row in reader:
            zc = row[gid].strip().zfill(5)
            try:
                out[zc] = (f"{float(row[lat_c].strip()):.5f}", f"{float(row[lon_c].strip()):.5f}")
            except (ValueError, KeyError):
                continue
    return out


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

    # 3. Join each ZIP's centroid (blank when the Gazetteer has no ZCTA for it).
    centroids = _zcta_centroids()
    missing = sum(1 for zc, _ in rows if zc not in centroids)
    print(f"centroids: {len(centroids)} total, {missing}/{len(rows)} green ZIPs without one", file=sys.stderr)

    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["zip", "zone", "lat", "lon"])
        for zc, zone in rows:
            lat, lon = centroids.get(zc, ("", ""))
            w.writerow([zc, zone, lat, lon])
    print(f"wrote {len(rows)} rows to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
