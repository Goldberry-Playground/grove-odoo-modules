#!/usr/bin/env python3
"""Pirate Ship persisted-query drift watch (Train #4, gom#347 rollout step 4).

Live ZIP quotes call Pirate Ship's GraphQL endpoint with a *persisted query*:
the server only executes queries it already knows, identified by the SHA-256
of the query text (``extensions.persistedQuery.sha256Hash``). We pin that hash.
When Pirate Ship changes its ``RatesQuery`` the pinned hash is retired and every
live quote fails over to the ZIP-area table, so we want to learn about a change
*before* checkout does.

This script compares our pin against ``taciturnaxolotl/pirateship-api``
(MIT), whose ``src/rates-query.ts`` is a verbatim copy of Pirate Ship's query
plus its SHA-256 and ships its own ``sync-query`` drift check.

Where "our pin" comes from, in order (all that exist must agree):

1. Any ``RATES_QUERY_HASH = "<64 hex>"`` constant under ``grove_headless/`` or
   ``scripts/rate_check/`` (the client, once gom#347 step 1 lands).
2. ``scripts/ci/pirateship-rates-query.sha256`` (one line; the spec's pin, so
   this watch is live before the client exists).
3. ``--expected HASH`` overrides both (used by the forced-mismatch QA gate).

Exit codes (the workflow branches on these):

* 0  in sync
* 1  DRIFT: upstream hash differs from our pin
* 2  no local pin found (nothing to compare; not an alert)
* 3  upstream unreachable / unparseable / internally inconsistent

Stdlib only. Run ``python3 scripts/ci/pirateship_query_drift.py --help``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

UPSTREAM_REPO = "taciturnaxolotl/pirateship-api"
UPSTREAM_PATH = "src/rates-query.ts"
# ``HEAD`` resolves to the repository's default branch, whatever it is named.
UPSTREAM_RAW_URL = f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/HEAD/{UPSTREAM_PATH}"
UPSTREAM_HTML_URL = f"https://github.com/{UPSTREAM_REPO}/blob/HEAD/{UPSTREAM_PATH}"

PIN_FILE = Path("scripts/ci/pirateship-rates-query.sha256")
PIN_SCAN_DIRS = (Path("grove_headless"), Path("scripts/rate_check"))

_HEX64 = r"[0-9a-f]{64}"
# Upstream: `export const RATES_QUERY =\n    '...'` (single-quoted, JS escapes).
_RE_UPSTREAM_QUERY = re.compile(r"RATES_QUERY\s*=\s*'((?:[^'\\]|\\.)*)'", re.S)
_RE_UPSTREAM_HASH = re.compile(rf"RATES_QUERY_HASH\s*=\s*['\"]({_HEX64})['\"]")
# Ours: `RATES_QUERY_HASH = "..."` in Python (either quote style).
_RE_LOCAL_CONST = re.compile(rf"""^\s*RATES_QUERY_HASH\s*=\s*['"]({_HEX64})['"]""", re.M)

EXIT_OK, EXIT_DRIFT, EXIT_UNPINNED, EXIT_ERROR = 0, 1, 2, 3


class UpstreamError(RuntimeError):
    """Upstream could not be fetched or does not look like rates-query.ts."""


def _js_unescape(s: str) -> str:
    """Decode the JS single-quoted string literal body (``\\n``, ``\\'`` ...)."""
    return s.encode("utf-8").decode("unicode_escape")


def parse_upstream(text: str) -> tuple[str, str]:
    """Return ``(declared_hash, computed_hash)`` from rates-query.ts text.

    ``computed_hash`` is the SHA-256 of the unescaped ``RATES_QUERY`` literal,
    which is what Pirate Ship's server keys on. Both are returned so the caller
    can detect an upstream file that is itself inconsistent.
    """
    m_q = _RE_UPSTREAM_QUERY.search(text)
    m_h = _RE_UPSTREAM_HASH.search(text)
    if not m_q or not m_h:
        raise UpstreamError(f"{UPSTREAM_PATH} did not contain RATES_QUERY and RATES_QUERY_HASH")
    computed = hashlib.sha256(_js_unescape(m_q.group(1)).encode("utf-8")).hexdigest()
    return m_h.group(1), computed


def fetch_upstream(url: str = UPSTREAM_RAW_URL, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "grove-odoo-modules pirateship-query-drift"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpstreamError(f"could not fetch {url}: {exc}") from exc


def find_local_pins(root: Path) -> dict[str, str]:
    """Map ``relative path -> hash`` for every pin found under ``root``."""
    pins: dict[str, str] = {}
    for d in PIN_SCAN_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for py in sorted(base.rglob("*.py")):
            try:
                text = py.read_text(encoding="utf-8")
            except OSError:
                continue
            for m in _RE_LOCAL_CONST.finditer(text):
                pins[str(py.relative_to(root))] = m.group(1)
    pin_file = root / PIN_FILE
    if pin_file.is_file():
        raw = pin_file.read_text(encoding="utf-8").strip().lower()
        if re.fullmatch(_HEX64, raw):
            pins[str(PIN_FILE)] = raw
    return pins


def _emit_outputs(values: dict[str, str]) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        for k, v in values.items():
            fh.write(f"{k}={v}\n")


def _report(status: str, upstream: str | None, local: str | None, pins: dict[str, str], detail: str) -> str:
    lines = [f"### Pirate Ship persisted-query drift watch: **{status.upper()}**", ""]
    lines.append(f"- Upstream: [{UPSTREAM_REPO} `{UPSTREAM_PATH}`]({UPSTREAM_HTML_URL})")
    lines.append(f"- Upstream `RATES_QUERY_HASH`: `{upstream or 'n/a'}`")
    lines.append(f"- Our pin: `{local or 'n/a'}`")
    if pins:
        lines.append("- Pin locations:")
        lines.extend(f"  - `{p}` = `{h[:12]}…`" for p, h in sorted(pins.items()))
    lines.append("")
    lines.append(detail)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=Path("."), help="repo root (default: cwd)")
    ap.add_argument("--upstream-file", type=Path, help="read rates-query.ts from this file instead of fetching")
    ap.add_argument("--upstream-url", default=UPSTREAM_RAW_URL)
    ap.add_argument("--expected", help="compare against this hash instead of the repo pin (forced-mismatch test)")
    ap.add_argument("--out", type=Path, help="write the markdown report here")
    ap.add_argument("--json", action="store_true", help="print a JSON summary to stdout")
    args = ap.parse_args(argv)

    pins = find_local_pins(args.root)
    upstream_declared = upstream_computed = None
    status, detail, code = "ok", "", EXIT_OK

    try:
        if args.upstream_file:
            text = args.upstream_file.read_text(encoding="utf-8")
        else:
            text = fetch_upstream(args.upstream_url)
        upstream_declared, upstream_computed = parse_upstream(text)
        if upstream_declared != upstream_computed:
            raise UpstreamError(
                f"upstream is internally inconsistent: declares {upstream_declared} but its RATES_QUERY "
                f"hashes to {upstream_computed} (their sync-query is mid-update; re-run later)"
            )
    except UpstreamError as exc:
        status, detail, code = "error", f"Upstream check could not run: {exc}", EXIT_ERROR

    local: str | None = None
    if code == EXIT_OK:
        if args.expected:
            local = args.expected.strip().lower()
            if not re.fullmatch(_HEX64, local):
                status, detail, code = "error", f"--expected is not a 64-hex SHA-256: {args.expected!r}", EXIT_ERROR
        elif not pins:
            status, code = "unpinned", EXIT_UNPINNED
            detail = (
                f"No `RATES_QUERY_HASH` constant under {', '.join(str(d) for d in PIN_SCAN_DIRS)} and no "
                f"`{PIN_FILE}`; nothing to compare."
            )
        elif len(set(pins.values())) > 1:
            status, code = "drift", EXIT_DRIFT
            detail = "Our own pins DISAGREE with each other; fix the repo before trusting any live quote."
        else:
            local = next(iter(pins.values()))

    if code == EXIT_OK and local is not None:
        if local == upstream_computed:
            detail = "In sync: our pinned persisted-query hash matches Pirate Ship's current RatesQuery."
        else:
            status, code = "drift", EXIT_DRIFT
            detail = (
                "**Pirate Ship changed its RatesQuery.** Live quotes using the old hash will get "
                "`PersistedQueryNotFound` and fall back to the ZIP-area table until the pin is updated.\n\n"
                f"Fix (one-line PR): set every pin above to `{upstream_computed}` after confirming a keyless "
                "quote against pirateship.com/rates still returns the expected shape."
            )

    report = _report(status, upstream_declared, local, pins, detail)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
    summary = {"status": status, "upstream_hash": upstream_computed or "", "local_hash": local or "", "exit": code}
    _emit_outputs({k: str(v) for k, v in summary.items()})
    print(json.dumps(summary) if args.json else report, end="" if not args.json else "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
