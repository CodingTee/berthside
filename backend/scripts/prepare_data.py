#!/usr/bin/env python3
"""Copy the hackathon dataset into backend/data/ so it can be baked into the
Docker image for deployment.

    python scripts/prepare_data.py                       # auto-detect the bundle
    python scripts/prepare_data.py --source <folder>     # explicit path

Why: the cloud backend needs the inbox + attachments at runtime. The dataset
is only ~3.5 MB, so the simplest reliable option is to bake it into the image
(free hosting tiers have no volumes). `data/` is git-ignored on purpose — this
script recreates it locally instead of committing the dataset.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_ROOT / "data"

CANDIDATES = [
    # Baked into the image by the Dockerfile (cloud / Render build).
    Path("/bundle"),
    # In-repo copy: <repo>/backend/data/corpus.
    BACKEND_ROOT / "data" / "corpus",
    Path.home() / "Downloads" / "sdoc-hackathon-docker" / "data_v2",
    Path.home() / "Downloads" / "sdoc-hackathon-bundle",
]
REQUIRED = ["inbox", "attachments", "sample_submission.json"]


def is_dataset(path: Path) -> bool:
    return all((path / item).exists() for item in REQUIRED)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    source = Path(args.source) if args.source else None
    if source is None:
        for candidate in CANDIDATES:
            if is_dataset(candidate):
                source = candidate
                break

    if source is None or not is_dataset(source):
        print("Could not find the dataset. Looked in:")
        for c in ([source] if source else []) + CANDIDATES:
            print(f"  - {c}")
        print("\nPass it explicitly:  python scripts/prepare_data.py --source <folder>")
        return 1

    if DATA_DIR.exists() and not args.force:
        print(f"{DATA_DIR} already exists — use --force to overwrite")
        return 0
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)

    DATA_DIR.mkdir(parents=True)
    for item in REQUIRED:
        src, dst = source / item, DATA_DIR / item
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)

    emails = len(list((DATA_DIR / "inbox").glob("email_*.json")))
    atts = len(list((DATA_DIR / "attachments").iterdir()))
    print(f"prepared {DATA_DIR}")
    print(f"  from    : {source}")
    print(f"  emails  : {emails}")
    print(f"  attachments: {atts}")
    print("\nSet DATA_SOURCE=/data in the cloud environment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
