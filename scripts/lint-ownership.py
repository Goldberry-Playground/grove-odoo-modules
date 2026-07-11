#!/usr/bin/env python3
"""Validate an ownership.yml against the AgenticOS group-coding ownership schema.

Zero-dependency (Python 3.8+ stdlib only) so it runs identically in every repo's
CI regardless of language toolchain. It parses the strict YAML subset used by
ownership.yml (see docs / the file header for the grammar), validates the schema,
and checks that every primary source directory at the repo root is claimed by at
least one partition (acceptance: "no unmapped top-level app/module dir").

Usage:
  python3 scripts/lint-ownership.py [--file ownership.yml] [--root .] [--quiet]

Exit codes: 0 = valid, 1 = validation errors, 2 = usage/parse error.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

# Top-level dirs that are never source partitions (build output, deps, VCS, IDE).
IGNORED_TOP_LEVEL = {
    ".git", ".github", ".hg", ".svn",
    "node_modules", ".pnpm-store", ".turbo", ".next", ".cache",
    "dist", "build", "out", "coverage", ".nyc_output",
    ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    ".idea", ".vscode", ".sandcastle", ".design-sync", ".ds-sync",
    ".terraform",
}
# ".github" is intentionally in IGNORED_TOP_LEVEL for *coverage* (CI config, not
# product source). Partitions may still map it explicitly if they choose.


# --------------------------------------------------------------------------- #
# Minimal YAML-subset parser (block maps, block/flow sequences, scalars).
# Strict: 2-space indentation, spaces only (no tabs). Good enough for the
# constrained ownership.yml grammar and fails loudly on anything unexpected.
# --------------------------------------------------------------------------- #
class YamlError(Exception):
    pass


def _strip_comment(line: str) -> str:
    # Remove trailing "# ..." comments that are not inside quotes / brackets.
    out, in_s, in_d = [], False, False
    i = 0
    while i < len(line):
        c = line[i]
        if c == "'" and not in_d:
            in_s = not in_s
        elif c == '"' and not in_s:
            in_d = not in_d
        elif c == "#" and not in_s and not in_d:
            if i == 0 or line[i - 1] in " \t":
                break
        out.append(c)
        i += 1
    return "".join(out)


def _scalar(tok: str):
    tok = tok.strip()
    if tok == "" or tok == "~" or tok.lower() == "null":
        return None
    if (tok[0] == '"' and tok[-1] == '"') or (tok[0] == "'" and tok[-1] == "'"):
        return tok[1:-1]
    if tok.startswith("[") and tok.endswith("]"):
        inner = tok[1:-1].strip()
        if inner == "":
            return []
        return [_scalar(p) for p in _split_flow(inner)]
    if re.fullmatch(r"-?\d+", tok):
        return int(tok)
    if tok.lower() in ("true", "false"):
        return tok.lower() == "true"
    return tok


def _split_flow(inner: str):
    parts, depth, cur = [], 0, []
    in_s = in_d = False
    for c in inner:
        if c == "'" and not in_d:
            in_s = not in_s
        elif c == '"' and not in_s:
            in_d = not in_d
        if c == "[" and not in_s and not in_d:
            depth += 1
        elif c == "]" and not in_s and not in_d:
            depth -= 1
        if c == "," and depth == 0 and not in_s and not in_d:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    if cur:
        parts.append("".join(cur))
    return [p for p in parts]


class _Line:
    __slots__ = ("indent", "text", "no")

    def __init__(self, indent, text, no):
        self.indent = indent
        self.text = text
        self.no = no


def _tokenize(src: str):
    lines = []
    for i, raw in enumerate(src.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
            raise YamlError(f"line {i}: tab in indentation (use spaces)")
        stripped = _strip_comment(raw).rstrip()
        if stripped.strip() == "":
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append(_Line(indent, stripped.strip(), i))
    return lines


def _parse_block(lines, idx, indent):
    """Parse a mapping or sequence at the given indent; return (value, next_idx)."""
    if idx >= len(lines):
        return None, idx
    first = lines[idx]
    if first.indent < indent:
        return None, idx
    if first.text.startswith("- "):
        return _parse_seq(lines, idx, first.indent)
    return _parse_map(lines, idx, first.indent)


def _parse_map(lines, idx, indent):
    result = {}
    while idx < len(lines):
        ln = lines[idx]
        if ln.indent < indent:
            break
        if ln.indent > indent:
            raise YamlError(f"line {ln.no}: unexpected indentation")
        if ln.text.startswith("- "):
            raise YamlError(f"line {ln.no}: sequence item inside mapping")
        if ":" not in ln.text:
            raise YamlError(f"line {ln.no}: expected 'key: value'")
        key, _, val = ln.text.partition(":")
        key = key.strip()
        val = val.strip()
        if val == "":
            child, idx = _parse_block(lines, idx + 1, indent + 1)
            result[key] = child
        else:
            result[key] = _scalar(val)
            idx += 1
    return result, idx


def _parse_seq(lines, idx, indent):
    result = []
    while idx < len(lines):
        ln = lines[idx]
        if ln.indent < indent:
            break
        if ln.indent > indent:
            raise YamlError(f"line {ln.no}: unexpected indentation")
        if not ln.text.startswith("- "):
            break
        rest = ln.text[2:].strip()
        if rest == "":
            child, idx = _parse_block(lines, idx + 1, indent + 1)
            result.append(child)
        elif ":" in rest and not (rest.startswith("[") or rest.startswith('"') or rest.startswith("'")):
            # inline map item: "- key: value" — reparse as a map by rewriting the
            # current line to the map key at a deeper indent, then continue there.
            lines[idx] = _Line(indent + 2, rest, ln.no)
            child, idx = _parse_map(lines, idx, indent + 2)
            result.append(child)
        else:
            result.append(_scalar(rest))
            idx += 1
    return result, idx


def parse_yaml(src: str):
    lines = _tokenize(src)
    if not lines:
        return {}
    value, idx = _parse_block(lines, 0, 0)
    if idx != len(lines):
        raise YamlError(f"line {lines[idx].no}: could not parse (indentation?)")
    return value


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #
HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def validate(doc, root, errors):
    if not isinstance(doc, dict):
        errors.append("top level must be a mapping")
        return
    if doc.get("version") != 1:
        errors.append(f"version: expected 1, got {doc.get('version')!r}")
    if not isinstance(doc.get("repo"), str) or not doc.get("repo"):
        errors.append("repo: required non-empty string")

    default_review = _as_list((doc.get("defaults") or {}).get("review")) if isinstance(doc.get("defaults"), dict) else []
    for h in default_review:
        if not isinstance(h, str) or not HANDLE_RE.match(h):
            errors.append(f"defaults.review: invalid handle {h!r}")

    partitions = doc.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        errors.append("partitions: required non-empty list")
        return

    names, claimed_globs = set(), []
    for i, p in enumerate(partitions):
        where = f"partitions[{i}]"
        if not isinstance(p, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        name = p.get("name")
        if not isinstance(name, str) or not name:
            errors.append(f"{where}.name: required non-empty string")
        else:
            where = f"partition '{name}'"
            if name in names:
                errors.append(f"{where}: duplicate partition name")
            names.add(name)

        paths = _as_list(p.get("paths"))
        if not paths:
            errors.append(f"{where}.paths: required non-empty list")
        for pat in paths:
            if not isinstance(pat, str) or not pat:
                errors.append(f"{where}.paths: invalid glob {pat!r}")
                continue
            claimed_globs.append(pat)
            head = pat.split("/", 1)[0]
            # Only probe existence when the leading path segment is a literal
            # (no glob metacharacters) — e.g. "apps/hub/**" -> "apps". A wildcard
            # head like "docker-compose.*.yml" can't be resolved cheaply; skip it.
            if head and not any(ch in head for ch in "*?[]") and not os.path.exists(os.path.join(root, head)):
                errors.append(f"{where}: glob '{pat}' references missing top-level path '{head}'")

        owners = _as_list(p.get("owner"))
        if not owners:
            errors.append(f"{where}.owner: required (agent handle or list)")
        for o in owners:
            if not isinstance(o, str) or not HANDLE_RE.match(o or ""):
                errors.append(f"{where}.owner: invalid handle {o!r}")

        review = _as_list(p.get("review")) or default_review
        if not review:
            errors.append(f"{where}.review: required (no defaults.review fallback set)")
        for r in review:
            if not isinstance(r, str) or not HANDLE_RE.match(r or ""):
                errors.append(f"{where}.review: invalid handle {r!r}")

        if "wave" in p and not isinstance(p.get("wave"), int):
            errors.append(f"{where}.wave: must be an integer if present")

    # Duplicate identical globs across partitions => ambiguous ownership.
    seen = set()
    for g in claimed_globs:
        if g in seen:
            errors.append(f"glob '{g}' is claimed by more than one partition (ambiguous ownership)")
        seen.add(g)

    _check_coverage(root, claimed_globs, errors)


def _glob_claims_dir(glob: str, d: str) -> bool:
    head = glob.split("/", 1)[0]
    return head == d or head == "**" or (head.endswith("**") and d.startswith(head[:-2].rstrip("/")))


def _check_coverage(root, globs, errors):
    try:
        entries = sorted(os.listdir(root))
    except OSError as e:
        errors.append(f"cannot read repo root '{root}': {e}")
        return
    for name in entries:
        full = os.path.join(root, name)
        if not os.path.isdir(full):
            continue
        if name in IGNORED_TOP_LEVEL or name.startswith("."):
            continue
        if not any(_glob_claims_dir(g, name) for g in globs):
            errors.append(f"top-level source dir '{name}/' is not covered by any partition")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate ownership.yml")
    ap.add_argument("--file", default="ownership.yml")
    ap.add_argument("--root", default=None, help="repo root for coverage (default: dir of --file)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    path = args.file
    if not os.path.exists(path):
        print(f"ownership-lint: file not found: {path}", file=sys.stderr)
        return 2
    root = args.root or (os.path.dirname(os.path.abspath(path)) or ".")

    try:
        with open(path, encoding="utf-8") as fh:
            doc = parse_yaml(fh.read())
    except YamlError as e:
        print(f"ownership-lint: parse error in {path}: {e}", file=sys.stderr)
        return 2

    errors = []
    validate(doc, root, errors)
    if errors:
        print(f"ownership-lint: {len(errors)} error(s) in {path}:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    if not args.quiet:
        n = len(doc.get("partitions", []))
        print(f"ownership-lint: OK — {path} ({n} partitions, all top-level source dirs covered)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
