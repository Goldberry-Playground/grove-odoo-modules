"""Tests for scripts/ci/pirateship_query_drift.py (gom#347 step 4, GOL-3303)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "ci" / "pirateship_query_drift.py"
FIXTURE = Path(__file__).parent / "fixtures" / "pirateship_rates_query.ts"
# The hash in the fixture (upstream as of 2026-10-05; also the spec's pin).
FIXTURE_HASH = "2692f7149bbb741a0fd343d1d59d8c02b0d4cf50abe36d3ebce54bdef2b73f85"
OTHER_HASH = "0" * 64

spec = importlib.util.spec_from_file_location("pirateship_query_drift", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def _repo(tmp_path: Path, *, pin_file: str | None = None, consts: dict[str, str] | None = None) -> Path:
    root = tmp_path / "repo"
    (root / "scripts" / "ci").mkdir(parents=True, exist_ok=True)
    if pin_file is not None:
        (root / mod.PIN_FILE).write_text(pin_file + "\n")
    for rel, h in (consts or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'# pinned from upstream\nRATES_QUERY_HASH = "{h}"\n')
    return root


def _run(root: Path, *extra: str, out: Path | None = None) -> tuple[int, str]:
    argv = ["--root", str(root), "--upstream-file", str(FIXTURE), "--json", *extra]
    if out:
        argv += ["--out", str(out)]
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = mod.main(argv)
    return code, buf.getvalue()


def test_parse_upstream_recomputes_declared_hash():
    declared, computed = mod.parse_upstream(FIXTURE.read_text())
    assert declared == FIXTURE_HASH
    assert computed == FIXTURE_HASH, "sha256 of the unescaped RATES_QUERY must equal the declared hash"


def test_parse_upstream_rejects_garbage():
    with pytest.raises(mod.UpstreamError):
        mod.parse_upstream("export const SOMETHING_ELSE = 1\n")


def test_in_sync_via_pin_file(tmp_path):
    code, out = _run(_repo(tmp_path, pin_file=FIXTURE_HASH))
    assert code == mod.EXIT_OK
    assert json.loads(out)["status"] == "ok"


def test_drift_via_pin_file_writes_report(tmp_path):
    report = tmp_path / "report.md"
    code, out = _run(_repo(tmp_path, pin_file=OTHER_HASH), out=report)
    assert code == mod.EXIT_DRIFT
    assert json.loads(out)["status"] == "drift"
    text = report.read_text()
    assert "DRIFT" in text and FIXTURE_HASH in text and OTHER_HASH in text


def test_code_constant_is_discovered_and_must_agree_with_pin_file(tmp_path):
    root = _repo(tmp_path, pin_file=FIXTURE_HASH, consts={"grove_headless/shipping/pirateship.py": OTHER_HASH})
    code, _ = _run(root, out=tmp_path / "r.md")
    assert code == mod.EXIT_DRIFT
    assert "DISAGREE" in (tmp_path / "r.md").read_text()


def test_code_constant_alone_is_enough(tmp_path):
    root = _repo(tmp_path, consts={"scripts/rate_check/pirateship_client.py": FIXTURE_HASH})
    code, _ = _run(root)
    assert code == mod.EXIT_OK


def test_unpinned_is_not_an_alert(tmp_path):
    code, out = _run(_repo(tmp_path))
    assert code == mod.EXIT_UNPINNED
    assert json.loads(out)["status"] == "unpinned"


def test_expected_override_forces_mismatch(tmp_path):
    code, out = _run(_repo(tmp_path, pin_file=FIXTURE_HASH), "--expected", OTHER_HASH)
    assert code == mod.EXIT_DRIFT
    code, _ = _run(_repo(tmp_path, pin_file=OTHER_HASH), "--expected", FIXTURE_HASH)
    assert code == mod.EXIT_OK, "--expected wins over the repo pin"


def test_bad_expected_is_an_error(tmp_path):
    code, _ = _run(_repo(tmp_path, pin_file=FIXTURE_HASH), "--expected", "not-a-hash")
    assert code == mod.EXIT_ERROR


def test_inconsistent_upstream_is_an_error_not_drift(tmp_path):
    bad = tmp_path / "bad.ts"
    bad.write_text(FIXTURE.read_text().replace(FIXTURE_HASH, OTHER_HASH))
    code = mod.main(["--root", str(_repo(tmp_path, pin_file=FIXTURE_HASH)), "--upstream-file", str(bad), "--json"])
    assert code == mod.EXIT_ERROR


def test_unreachable_upstream_is_an_error(tmp_path):
    code = mod.main(
        ["--root", str(_repo(tmp_path, pin_file=FIXTURE_HASH)), "--upstream-url", "http://127.0.0.1:9/x", "--json"]
    )
    assert code == mod.EXIT_ERROR


def test_github_output_is_written(tmp_path, monkeypatch):
    gh_out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
    _run(_repo(tmp_path, pin_file=FIXTURE_HASH))
    kv = dict(line.split("=", 1) for line in gh_out.read_text().splitlines())
    assert kv["status"] == "ok" and kv["upstream_hash"] == FIXTURE_HASH and kv["exit"] == "0"


def test_repo_pin_file_matches_fixture_upstream():
    """The committed pin must be the hash upstream declared when the fixture was taken."""
    assert (REPO / mod.PIN_FILE).read_text().strip() == FIXTURE_HASH
    assert os.path.isfile(SCRIPT)
