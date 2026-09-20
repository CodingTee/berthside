"""`tune_eval.py` must find the organisers' bundle wherever it happens to be.

This guards a real regression. The script used to hardcode
`~/Downloads/sdoc-hackathon-docker` for `score_cli.py` and the ground truth,
even though `scripts/sdoc_paths.py` existed to resolve exactly that. Once the
bundle moved, every documented `python scripts/tune_eval.py` invocation died
with "scorer not found", so the Regression-gate section of the README quietly
stopped working.

The `_score_cmd` tests pin the other half of the fix: `--ground-truth` is only
passed when the caller asks for it, leaving `score_cli.py` to resolve its own
sibling file. That way the scorer and the answer key can never be paired across
two different bundles.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BACKEND_ROOT / "scripts"
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

import sdoc_paths  # noqa: E402
import scripts.tune_eval as tune_eval  # noqa: E402
from scripts.tune_eval import _score_cmd  # noqa: E402


def test_ground_truth_is_omitted_so_score_cli_resolves_its_own(tmp_path):
    cmd = _score_cmd(tmp_path / "score_cli.py", "submission.json")
    assert "--ground-truth" not in cmd


def test_explicit_ground_truth_is_passed_through(tmp_path):
    truth = tmp_path / "ground_truth.json"
    cmd = _score_cmd(tmp_path / "score_cli.py", "submission.json", truth)
    assert cmd[-2:] == ["--ground-truth", str(truth)]


def test_bundle_location_is_delegated_not_hardcoded():
    source = (SCRIPTS_DIR / "tune_eval.py").read_text(encoding="utf-8")
    assert "sdoc_paths" in source, "tune_eval.py must use the shared resolver"
    assert "Downloads" not in source, "the old hardcoded fallback came back"


@pytest.fixture()
def corpus_available():
    """Skip the tests that run the pipeline when the corpus is not unpacked."""
    from app.services import inbox_service

    try:
        if not inbox_service.all_emails(include_ingested=False):
            pytest.skip("corpus not available on this machine")
    except Exception:  # noqa: BLE001 - any load failure means "not available"
        pytest.skip("corpus not available on this machine")


def test_discovers_the_bundle_through_sdoc_materials(tmp_path, corpus_available):
    """With $SDOC_MATERIALS set, a stub scorer must actually be executed.

    The stub echoes its argv, which proves two things at once: discovery found
    the bundle, and `--ground-truth` was left off the command line.
    """
    bundle = tmp_path / "sdoc-hackathon-docker"
    (bundle / "server").mkdir(parents=True)
    (bundle / "server" / "score_cli.py").write_text(
        "import sys\nprint('STUB-ARGV', sys.argv[1:])\n", encoding="utf-8")
    (bundle / "data_v2").mkdir()

    res = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "tune_eval.py"),
         "--limit", "1", "--out", str(tmp_path / "sub.json")],
        capture_output=True, text=True,
        env={**os.environ, "SDOC_MATERIALS": str(bundle)},
        cwd=str(BACKEND_ROOT))

    assert res.returncode == 0, res.stderr
    assert "STUB-ARGV" in res.stdout
    assert "--ground-truth" not in res.stdout


def test_missing_bundle_reports_where_it_looked(
        tmp_path, monkeypatch, capsys, corpus_available):
    """Nothing to find: fail with the searched locations, not a traceback."""
    monkeypatch.setattr(sdoc_paths, "candidate_roots", lambda: [])
    monkeypatch.setattr(sys, "argv", [
        "tune_eval.py", "--limit", "1", "--out", str(tmp_path / "sub.json")])

    assert tune_eval.main() == 1

    out = capsys.readouterr().out
    assert "score_cli.py" in out
    assert "SDOC_MATERIALS" in out
