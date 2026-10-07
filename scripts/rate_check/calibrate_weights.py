#!/usr/bin/env python3
"""Packed-weight model calibration — predicted vs. recorded shipped weight (GOL-3201).

The rate feed prices each box at an ESTIMATED ACTUAL billable weight, not the
full-capacity worst case (``shipping_boxes.representative_billable_lb`` /
``weight_basis``). This script reports how well that model tracks the weights we
actually shipped, and what the calibration SHOULD be — it is run before each
refresh PR and **never writes anything live** (same contract as
``rate_check.py`` / ``staleness.py``). A human reviews the printed report and,
if it recommends a change, edits ``WEIGHT_CALIBRATION`` in
``grove_headless/models/shipping_boxes.py`` in a reviewed PR.

Input (``--samples PATH``): recorded ``grove.label.batch.line.weight_lb`` per
box type. Two shapes are accepted so this can read either a hand-grouped export
or GOL-3200's golden-labels file directly:

  A. ``{"small": [6.5, 8.5, ...], "large": [...]}`` — box_id -> list of weights.
     A weight may be a bare number or ``{"weight_lb": 6.5, "count": 3}`` (the
     optional ``count`` is the packed tree count, which unlocks a tare/per-tree
     least-squares fit below).
  B. ``{"labels": [{"box_id": "small", "packed_weight_lb": 6.5, ...}, ...]}`` —
     the GOL-3200 golden-labels shape; rows are grouped by ``box_id`` on
     ``packed_weight_lb`` (``weight_lb`` also accepted).

Per box the report carries: sample count; observed min / median / mean / p90 /
max; the box's CURRENT published representative weight; the calibrated typical
billable weight (``ceil(median)``); the resulting ``weight_basis``; and, when
samples carry ``count``, a least-squares fit of ``tare_total`` + ``per_tree_lb``
with its residual (what ``PER_TREE_LB`` / tare should move to). A box with fewer
than ``MIN_CALIBRATION_SAMPLES`` samples, or in ``FORCE_CONSERVATIVE_BOXES``,
stays ``unverified`` and keeps the conservative weight no matter what the
samples say.

Exit codes (match the rate-check family):
  * 0 — no refresh needed: no VERIFIED box's calibrated weight differs from what
        we publish today (or there are no qualifying samples).
  * 3 — a refresh PR is recommended: at least one box would be published at a
        different billable weight (or newly (un)verified) if the calibration
        were applied. Prints a pasteable ``WEIGHT_CALIBRATION`` fragment.
  * 2 — bad input (missing/unreadable file, not an object, no usable samples).

No network, no DB, no secret: it reads a file and the pure box module.
"""

import argparse
import importlib.util as _ilu
import json
import math
import os
import sys

_SB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "grove_headless", "models", "shipping_boxes.py")
_spec = _ilu.spec_from_file_location("grove_shipping_boxes", _SB_PATH)
shipping_boxes = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(shipping_boxes)


class BadSamples(Exception):
    """Raised for structurally invalid --samples input (maps to exit code 2)."""


def _coerce_weight(entry):
    """One sample -> (weight_lb: float, count: int | None).

    Accepts a bare number or an object carrying ``weight_lb`` (or the golden
    file's ``packed_weight_lb``) and an optional integer ``count``. Rejects
    non-positive or non-numeric weights loudly — a zero/negative shipped weight
    is corrupt data, not a light box, and must not silently skew a median."""
    count = None
    if isinstance(entry, dict):
        raw = entry.get("weight_lb", entry.get("packed_weight_lb"))
        if "count" in entry and entry["count"] is not None:
            try:
                count = int(entry["count"])
            except (TypeError, ValueError):
                raise BadSamples(f"sample count {entry['count']!r} is not an integer")
            if count < 0:
                raise BadSamples(f"sample count {count} is negative")
    else:
        raw = entry
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise BadSamples(f"sample weight {raw!r} is not a number")
    weight = float(raw)
    if not weight > 0:
        raise BadSamples(f"sample weight {weight} is not positive")
    return weight, count


def load_samples(path: str) -> dict:
    """Parse ``--samples`` into ``{box_id: [(weight_lb, count|None), ...]}``.

    Supports both the grouped shape (box_id -> list) and the GOL-3200
    golden-labels shape (``{"labels": [...]}``). Keys that start with ``_`` are
    provenance/doc and ignored (same convention as the rates file)."""
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        raise BadSamples(f"samples file not found: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        raise BadSamples(f"cannot read samples file {path}: {exc}")
    if not isinstance(doc, dict):
        raise BadSamples("samples file must be a JSON object")

    grouped: dict[str, list] = {}
    if isinstance(doc.get("labels"), list):  # golden-labels shape
        for row in doc["labels"]:
            if not isinstance(row, dict):
                raise BadSamples("each label must be an object")
            box_id = row.get("box_id")
            if not box_id:
                raise BadSamples("a label is missing box_id")
            grouped.setdefault(box_id, []).append(_coerce_weight(row))
    else:  # grouped shape
        for box_id, entries in doc.items():
            if box_id.startswith("_"):
                continue
            if not isinstance(entries, list):
                raise BadSamples(f"samples for {box_id!r} must be a list")
            grouped[box_id] = [_coerce_weight(e) for e in entries]

    if not any(grouped.values()):
        raise BadSamples("no usable weight samples found")
    return grouped


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (e.g. pct=90 -> p90). Deterministic, no interp."""
    s = sorted(values)
    if not s:
        return 0.0
    rank = max(1, math.ceil(pct / 100.0 * len(s)))
    return s[min(rank, len(s)) - 1]


def observed_stats(weights: list[float]) -> dict:
    return {
        "n": len(weights),
        "min": round(min(weights), 2),
        "median": round(median(weights), 2),
        "mean": round(sum(weights) / len(weights), 2),
        "p90": round(percentile(weights, 90), 2),
        "max": round(max(weights), 2),
    }


def fit_tare_and_per_tree(samples: list) -> dict | None:
    """Least-squares fit of ``weight = tare_total + per_tree_lb * count``.

    Returns ``{tare_total_lb, per_tree_lb, rms_residual_lb, n}`` or ``None`` when
    too few counted samples (need >= 2 distinct counts to solve the two params).
    ``tare_total`` lumps carton + paper (the recorded weight cannot split them);
    the human decides how to apportion it across ``tare_lb`` / ``paper_lb``."""
    pts = [(c, w) for (w, c) in samples if c is not None]
    counts = {c for c, _ in pts}
    if len(pts) < 2 or len(counts) < 2:
        return None
    n = len(pts)
    sx = sum(c for c, _ in pts)
    sy = sum(w for _, w in pts)
    sxx = sum(c * c for c, _ in pts)
    sxy = sum(c * w for c, w in pts)
    denom = n * sxx - sx * sx
    if denom == 0:  # pragma: no cover — guarded by the distinct-count check
        return None
    per_tree = (n * sxy - sx * sy) / denom
    tare = (sy - per_tree * sx) / n
    resid = math.sqrt(sum((w - (tare + per_tree * c)) ** 2 for c, w in pts) / n)
    return {
        "tare_total_lb": round(tare, 2),
        "per_tree_lb": round(per_tree, 3),
        "rms_residual_lb": round(resid, 3),
        "n": n,
    }


def calibrate_box(box_id: str, samples: list) -> dict:
    """Calibration record for one box: observed stats, current vs. calibrated
    weight, resulting basis, and whether applying it would move a published
    weight or flip the basis (``moves``)."""
    weights = [w for (w, _) in samples]
    known = box_id in shipping_boxes.BOXES
    current_rep = shipping_boxes.representative_billable_lb(box_id) if known else None
    current_basis = shipping_boxes.weight_basis(box_id) if known else "unknown_box"

    n = len(weights)
    forced = box_id in shipping_boxes.FORCE_CONSERVATIVE_BOXES
    enough = n >= shipping_boxes.MIN_CALIBRATION_SAMPLES
    calibrated_basis = "verified" if (known and enough and not forced) else "unverified"

    typical = round(median(weights), 2)
    calibrated_billable = math.ceil(typical)
    if calibrated_basis == "unverified":
        # Unverified boxes stay on the conservative weight regardless of samples.
        calibrated_billable = shipping_boxes.conservative_billable_lb(box_id) if known else None

    moves = known and (calibrated_basis != current_basis or calibrated_billable != current_rep)
    rec = {
        "box_id": box_id,
        "known_box": known,
        "observed": observed_stats(weights),
        "current_published_lb": current_rep,
        "current_basis": current_basis,
        "typical_lb": typical,
        "calibrated_billable_lb": calibrated_billable,
        "calibrated_basis": calibrated_basis,
        "force_conservative": forced,
        "enough_samples": enough,
        "moves": moves,
    }
    fit = fit_tare_and_per_tree(samples)
    if fit is not None:
        rec["fit"] = fit
    return rec


def build_report(grouped: dict) -> dict:
    """Full calibration report keyed by box_id, plus ``needs_refresh``.

    ``needs_refresh`` is True when applying the calibration would change any
    KNOWN box's published billable weight or its basis — the actionable signal
    that a refresh PR to ``WEIGHT_CALIBRATION`` is warranted."""
    boxes = {box_id: calibrate_box(box_id, samples) for box_id, samples in grouped.items() if samples}
    needs_refresh = any(rec["moves"] for rec in boxes.values())
    return {"boxes": boxes, "needs_refresh": needs_refresh}


def calibration_fragment(report: dict) -> str:
    """A pasteable ``WEIGHT_CALIBRATION`` dict literal for the VERIFIED boxes —
    what a human copies into ``shipping_boxes.py`` in the refresh PR."""
    lines = ["WEIGHT_CALIBRATION = {"]
    for box_id, rec in sorted(report["boxes"].items()):
        if rec["calibrated_basis"] == "verified":
            lines.append(
                f'    "{box_id}": {{"samples": {rec["observed"]["n"]}, "typical_billable_lb": {rec["typical_lb"]}}},'
            )
    lines.append("}")
    return "\n".join(lines)


def _print_human(report: dict) -> None:
    for box_id, rec in sorted(report["boxes"].items()):
        o = rec["observed"]
        head = (
            f"{box_id}: n={o['n']} median={o['median']} mean={o['mean']} "
            f"p90={o['p90']} max={o['max']} -> {rec['calibrated_basis']}"
        )
        if not rec["known_box"]:
            head += "  (UNKNOWN box id — not in catalog)"
        print(head, file=sys.stderr)
        print(
            f"   published={rec['current_published_lb']} lb ({rec['current_basis']})"
            f"  calibrated={rec['calibrated_billable_lb']} lb ({rec['calibrated_basis']})"
            f"{'  *** MOVES ***' if rec['moves'] else ''}",
            file=sys.stderr,
        )
        if rec["force_conservative"]:
            print("   force-conservative: derived (not bench-measured); stays unverified", file=sys.stderr)
        if "fit" in rec:
            f = rec["fit"]
            print(
                f"   fit: tare_total={f['tare_total_lb']} lb + per_tree={f['per_tree_lb']} lb/tree"
                f"  (rms residual {f['rms_residual_lb']} lb over n={f['n']})",
                file=sys.stderr,
            )
    print(
        f"VERDICT: {'refresh PR recommended' if report['needs_refresh'] else 'no change needed'}",
        file=sys.stderr,
    )
    if report["needs_refresh"]:
        print("\nProposed WEIGHT_CALIBRATION fragment:\n" + calibration_fragment(report), file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Calibrate the packed-weight model (GOL-3201). Never writes live.")
    parser.add_argument("--samples", required=True, help="JSON of recorded weight_lb per box (see module docstring).")
    parser.add_argument("--json", action="store_true", help="Emit the machine-readable report on stdout.")
    args = parser.parse_args(argv)

    try:
        grouped = load_samples(args.samples)
    except BadSamples as exc:
        print(f"::error::bad --samples input: {exc}", file=sys.stderr)
        return 2

    report = build_report(grouped)
    if args.json:
        print(json.dumps(report, indent=2))
    _print_human(report)
    return 3 if report["needs_refresh"] else 0


if __name__ == "__main__":
    sys.exit(main())
