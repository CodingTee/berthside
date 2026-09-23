#!/usr/bin/env python3
"""Offline evaluation loop: run the pipeline + score it, no server needed.

    python scripts/tune_eval.py                     # full inbox, score
    python scripts/tune_eval.py --limit 60          # quick smoke run
    python scripts/tune_eval.py --out sub.json      # keep the submission

Why this exists: the self-evaluation endpoint is only reachable through the
official Docker server, but the organisers ship `score_cli.py` + the private
ground_truth.json in the docker bundle, so we can score locally in seconds and
iterate on the classifier without rebuilding anything.

The pipeline, the comparison engine and the submission builder are the *same*
code the API runs; only the transport is skipped.

Finding the bundle: `scripts/sdoc_paths.py` already resolves both private files
of the organisers' bundle (it honours `$SDOC_MATERIALS` and otherwise searches
upward from `backend/`), so this script calls into it instead of assuming a
path. The ground truth is deliberately *not* resolved here: score_cli.py
derives it from its own folder, so the scorer and the answer key can never be
taken from two different bundles.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

import sdoc_paths  # noqa: E402  (needs the sys.path setup above)


def _score_cmd(scorer: Path, submission: str | Path,
               ground_truth: str | Path | None = None) -> list[str]:
    """Argv for score_cli.py. `--ground-truth` only when explicitly asked for."""
    cmd = [sys.executable, str(scorer), str(submission)]
    if ground_truth:
        cmd += ["--ground-truth", str(ground_truth)]
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(BACKEND_ROOT / "submission.json"))
    ap.add_argument("--scorer", default=sdoc_paths.default_scorer(),
                    help="path to score_cli.py "
                         "(default: discovered by scripts/sdoc_paths.py)")
    ap.add_argument("--ground-truth", default="",
                    help="override the private ground truth "
                         "(default: score_cli.py resolves its own)")
    ap.add_argument("--db", default="")
    args = ap.parse_args()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import models  # noqa: F401
    from app.database import Base
    from app.services import workflow
    from app.services import inbox_service
    from app.services.submission import submission_entry

    if args.db:
        engine = create_engine(f"sqlite:///{args.db}")
    else:
        engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    # The corpus only. Emails ingested through the API live in the developer's
    # database and would otherwise join the run, so the score would describe a
    # corpus that does not exist. This is not hypothetical: it once processed
    # 521 emails instead of 520 for exactly this reason.
    emails = inbox_service.all_emails(include_ingested=False)
    if args.limit:
        emails = emails[:args.limit]

    t0 = time.perf_counter()
    by_status: dict[str, int] = {}
    for e in emails:
        r = workflow.process_email(db, e["email_id"])
        by_status[r.status] = by_status.get(r.status, 0) + 1
    elapsed = time.perf_counter() - t0

    # Build the submission in sample_submission shape.
    sample = inbox_service.sample_submission()
    reports = {r.email_id: r for r in db.query(models.ReportRecord).all()}
    submission = {}
    for email_id, default in sample.items():
        r = reports.get(email_id)
        if r is None:
            submission[email_id] = default
            continue
        submission[email_id] = submission_entry(r)

    Path(args.out).write_text(json.dumps(submission, indent=2), encoding="utf-8")
    print(f"processed {len(emails)} emails in {elapsed:.1f}s -> {args.out}")
    print("by_status:", json.dumps(by_status, sort_keys=True))

    if not args.scorer:
        print(sdoc_paths.missing_file_hint("score_cli.py"))
        return 1
    scorer = Path(args.scorer).expanduser()
    if not scorer.is_file():
        print(f"\nscorer not found: {scorer}\n"
              f"  pass --scorer <path to score_cli.py>")
        return 1

    ground_truth = (Path(args.ground_truth).expanduser()
                    if args.ground_truth else None)
    if ground_truth is not None and not ground_truth.is_file():
        print(f"\nground truth not found: {ground_truth}")
        return 1

    print(f"scorer       : {scorer}")
    print("ground truth : " + (str(ground_truth) if ground_truth
                               else "resolved by score_cli.py next to itself"))

    import subprocess
    res = subprocess.run(_score_cmd(scorer, args.out, ground_truth),
                         capture_output=True, text=True)
    print()
    print(res.stdout or res.stderr)
    return res.returncode


if __name__ == "__main__":
    raise SystemExit(main())
