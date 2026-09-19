"""Locate the organisers' private evaluation files without hardcoding a path.

Every diagnostic script here needs two files that ship ONLY inside the official
Docker bundle, which is why they are not in this repository:

    server/score_cli.py          the scorer the judges run
    data_v2/ground_truth.json    the answer key

Where each teammate unpacked that bundle differs per machine. Rather than every
script inventing its own absolute path (which only works for whoever wrote it),
this module resolves both files in one place, in this order:

    1. $SDOC_MATERIALS           set it once, every script picks it up
    2. conventional locations    searched upward from the backend root
    3. ~/Downloads/...           the layout tune_eval.py already assumes

Nothing here touches application code. These scripts are developer tooling, and
the paths they resolve are optional: a missing key just disables the one check
that needed it, it never breaks the app.

Usage inside a script:

    import sdoc_paths
    ap.add_argument("--ground-truth", default=sdoc_paths.default_ground_truth())
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BACKEND_ROOT / "scripts"

# The bundle keeps a stable internal layout, so once we find its root we know
# exactly where everything else lives.
BUNDLE_DIRNAME = "sdoc-hackathon-docker"
SCORER_REL = Path("server") / "score_cli.py"
GROUND_TRUTH_REL = Path("data_v2") / "ground_truth.json"

# How far to walk upward from backend/ looking for a sibling `materials/` dir.
_MAX_UPWARD = 3


def candidate_roots() -> list[Path]:
    """Every place the bundle might live, best guess first."""
    roots: list[Path] = []

    env = os.environ.get("SDOC_MATERIALS", "").strip()
    if env:
        roots.append(Path(env).expanduser())

    # Workspace layouts differ: some people keep `materials/` next to the repo,
    # some two levels up. Walk upward and try each.
    start = BACKEND_ROOT
    levels = [start, *list(start.parents)[:_MAX_UPWARD]]
    roots.extend(root / "materials" / BUNDLE_DIRNAME for root in levels)

    # The convention tune_eval.py has always assumed.
    roots.append(Path.home() / "Downloads" / BUNDLE_DIRNAME)

    seen: set[Path] = set()
    unique: list[Path] = []
    for r in roots:
        key = r.expanduser()
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def _locate(relative: Path) -> Path | None:
    for root in candidate_roots():
        candidate = root / relative
        if candidate.is_file():
            return candidate
    return None


def default_scorer() -> str:
    """Path to score_cli.py, or '' when it cannot be found."""
    found = _locate(SCORER_REL)
    return str(found) if found else ""


def default_ground_truth() -> str:
    """Path to ground_truth.json, or '' when it cannot be found."""
    found = _locate(GROUND_TRUTH_REL)
    return str(found) if found else ""


def missing_file_hint(what: str) -> str:
    """An actionable message for when neither defaults nor args resolved."""
    searched = "\n".join(f"    {root}" for root in candidate_roots())
    return (
        f"\nCould not find {what}. None of these locations has it:\n"
        f"{searched}\n\n"
        f"Point at it in either of these ways:\n"
        f"    export SDOC_MATERIALS=/path/to/{BUNDLE_DIRNAME}\n"
        f"    python <this script> --ground-truth /path/to/{what}\n"
    )


def load_official_scoring(scorer: str):
    """Import the judges' own scoring.py, the module sitting next to score_cli.

    Loaded by file path on purpose: putting the bundle's `server/` directory on
    sys.path would shadow this project's `app` package with the docker image's
    `app.py`, and every subsequent `from app...` import would resolve to the
    wrong code.
    """
    scoring_path = Path(scorer).expanduser().resolve().parent / "scoring.py"
    if not scoring_path.is_file():
        raise FileNotFoundError(f"no scoring.py next to the scorer at {scoring_path}")
    spec = importlib.util.spec_from_file_location("sdoc_scoring", scoring_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
