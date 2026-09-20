#!/usr/bin/env python3
"""Robustness / fuzz stress-test for the deterministic SI↔BL verifier.

WHY this exists (champion-level, beyond the 1.0000 on the clean bundle)
----------------------------------------------------------------------
The official scorer only ever sees the *clean* hackathon documents. The final
evaluation set, however, may contain noisier real-world artefacts: PDF text
extraction that collapses line breaks, stray non-breaking spaces, extra blank
lines, odd casing, or different label phrasing ("Load Port" vs "Port of
Loading"). This script injects exactly those meaning-preserving perturbations
and re-runs the *same* extract + compare engine to answer one question:

    Does noise that should NOT change the answer actually keep the answer?

It reports, per perturbation, how many BL↔BL documents kept an identical
defect-field set and what the FINAL SCORE is after re-scoring the perturbed
submission. A 1.0000 that survives noise is the real champion signal; a 1.0000
that collapses the moment you touch whitespace is a fragile one.

Discipline: this script never reads per-email gold answers. It compares the
*engine's own* baseline output against its perturbed output, and only consults
the official scorer (in aggregate) the same way tune_eval does.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

import sdoc_paths  # noqa: E402  (needs the sys.path setup above)

# Label phrases we know are interchangeable (from extractor.LABELS synonyms).
_LABEL_SYNONYMS = {
    "port of loading": ["load port", "place of receipt", "pol"],
    "port of discharge": ["discharge port", "destination port", "pod"],
    "gross weight": ["gross wt", "gross mass", "total gross weight"],
    "container count": ["no. of containers", "total containers", "ctnr"],
    "shipper": ["exporter", "principal"],
    "consignee": ["buyer", "receiver"],
    "notify party": ["notify address", "notified party"],
}


# ------------------------------------------------------------------ perturbations
def p_whitespace(text: str) -> str:
    """Randomise spacing: extra spaces, tabs, no double-newline runs lost."""
    out = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", lambda m: " " if random.random() < 0.7 else "  ", line)
        if random.random() < 0.15:
            line = "\t" + line
        out.append(line)
    return "\n".join(out)


def p_nbsp(text: str) -> str:
    """Swap some ASCII spaces for non-breaking / figure spaces (PDF artefacts)."""
    return re.sub(r" ", lambda m: "\u00a0" if random.random() < 0.3 else " ", text)


def p_blank_lines(text: str) -> str:
    """Insert blank lines between every source line (scanned-PDF spacing)."""
    return "\n\n".join(text.splitlines())


def p_uppercase(text: str) -> str:
    """ALL-CAPS the whole document (labels are matched case-insensitively)."""
    return text.upper()


def p_collapse_lines(text: str) -> str:
    """Lose line breaks. Some PDF extractors emit one long line."""
    return " ".join(text.splitlines())


def p_label_synonyms(text: str) -> str:
    """Reword field labels to an equivalent phrasing.

    Only *label positions* are rewritten: an alternative spelling followed by
    a value separator. Rewriting bare occurrences would also hit the value side
    (e.g. turning "...(POL)" into "...(port of loading)") and corrupt the very
    field it is meant to test.
    """
    for canon, alts in _LABEL_SYNONYMS.items():
        for alt in alts:
            if random.random() < 0.5:
                continue
            pat = re.compile(re.escape(alt) + r"(?=\s*[:：|])", re.IGNORECASE)
            text = pat.sub(canon, text, count=1)
    return text


PERTURBATIONS = {
    "whitespace": p_whitespace,
    "nbsp": p_nbsp,
    "blank_lines": p_blank_lines,
    "uppercase": p_uppercase,
    "collapse_lines": p_collapse_lines,
    "label_synonyms": p_label_synonyms,
}


# ------------------------------------------------------------------ harness
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--scorer", default=sdoc_paths.default_scorer(),
                    help="path to score_cli.py "
                         "(default: discovered by scripts/sdoc_paths.py)")
    ap.add_argument("--ground-truth", default="",
                    help="override the private ground truth "
                         "(default: score_cli.py resolves its own)")
    ap.add_argument("--out", default=str(BACKEND_ROOT / "stress_report.json"))
    args = ap.parse_args()
    random.seed(args.seed)

    # Fail before doing any work: the six perturbations take minutes, and
    # without a scorer the report is just a table of None. `Path("").exists()`
    # is True because the empty string means the current directory, so an
    # undiscovered scorer used to be handed to subprocess and every
    # perturbation silently reported final_score=None, which reads like a pass.
    if not args.scorer:
        print(sdoc_paths.missing_file_hint("score_cli.py"))
        return 1
    scorer = Path(args.scorer).expanduser()
    if not scorer.is_file():
        print(f"\nscorer not found: {scorer}\n"
              f"  pass --scorer <path to score_cli.py>")
        return 1

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import models  # noqa: F401
    from app.database import Base
    from app.services import workflow, inbox_service
    from app.services.extractor import extract_fields
    from app.services.comparison import compare
    from app.schemas import COMPARED_FIELDS

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    emails = inbox_service.all_emails()
    if args.limit:
        emails = emails[:args.limit]

    # ----- baseline: clean full pipeline (this is the 1.0000 run) ------------
    base_reports = {}
    for e in emails:
        r = workflow.process_email(db, e["email_id"])
        base_reports[e["email_id"]] = r

    # ----- identify BL_COMPARISON emails whose docs are plain text ------------
    doc_re = re.compile(r"_(SI|BL)\.[a-z]+$", re.IGNORECASE)
    txt_pairs = {}  # email_id -> (si_path, bl_path, si_text, bl_text)
    for e in emails:
        si = bl = None
        for att in e.get("attachments") or []:
            m = doc_re.search(att)
            if not m:
                continue
            if m.group(1).upper() == "SI" and si is None:
                si = att
            elif m.group(1).upper() == "BL" and bl is None:
                bl = att
        if si and bl and si.lower().endswith(".txt") and bl.lower().endswith(".txt"):
            try:
                st = inbox_service.read_attachment(si).decode("utf-8", "replace")
                bt = inbox_service.read_attachment(bl).decode("utf-8", "replace")
            except Exception:
                continue
            txt_pairs[e["email_id"]] = (si, bl, st, bt)

    print(f"baseline documents (clean): {len(txt_pairs)} SI/BL text pairs")

    # ----- helper: build a submission from base + optional perturbed verdicts --
    def build_submission(perturbed: dict[str, dict] | None = None) -> dict:
        sample = inbox_service.sample_submission()
        sub = {}
        for email_id, default in sample.items():
            r = base_reports.get(email_id)
            if r is None:
                sub[email_id] = default
                continue
            if perturbed and email_id in perturbed:
                pv = perturbed[email_id]
                sub[email_id] = {
                    "category": r.category,
                    "status": pv["status"],
                    "review_reason": pv["review_reason"],
                    "has_defect": pv["has_defect"],
                    "defect_fields": pv["defect_fields"],
                }
            else:
                sub[email_id] = {
                    "category": r.category,
                    "status": r.status if r.status != "ERROR" else "NEEDS_REVIEW",
                    "review_reason": r.review_reason,
                    "has_defect": bool(r.has_defect),
                    "defect_fields": r.defect_fields or [],
                }
        return sub

    def compare_pair(si_text: str, bl_text: str) -> dict:
        si = extract_fields(si_text, "SI")
        bl = extract_fields(bl_text, "BL")
        out = compare(si.fields, bl.fields, COMPARED_FIELDS)
        return {
            "status": out.status,
            "has_defect": out.has_defect,
            "defect_fields": out.defect_fields,
            "review_reason": out.review_reason,
            "si_missing": si.missing,
            "bl_missing": bl.missing,
        }

    tmp_dir = Path(tempfile.mkdtemp(prefix="sdoc_stress_"))
    sub_path = tmp_dir / "submission.json"

    # ----- run each perturbation ----------------------------------------------
    scored_all = True
    results = {}
    for pname, pfunc in PERTURBATIONS.items():
        perturbed: dict[str, dict] = {}
        changed = 0
        fields_lost = 0
        for email_id, (_, _, st, bt) in txt_pairs.items():
            base = compare_pair(st, bt)
            # baseline must be a clean decision (not already NEEDS_REVIEW from
            # missing values) for a fair "did noise change the verdict" check.
            pst, pbt = pfunc(st), pfunc(bt)
            pv = compare_pair(pst, pbt)
            perturbed[email_id] = pv
            if set(base["defect_fields"]) != set(pv["defect_fields"]):
                changed += 1
            # extraction degradation: a field extractable in the clean doc
            # became missing once noise was injected.
            base_missing = set(base["si_missing"]) | set(base["bl_missing"])
            pert_missing = set(pv["si_missing"]) | set(pv["bl_missing"])
            lost = len(pert_missing - base_missing)
            if lost > 0:
                fields_lost += lost

        submission = build_submission(perturbed)
        sub_path.write_text(json.dumps(submission, indent=2), encoding="utf-8")

        # --ground-truth is passed only when asked: score_cli.py otherwise reads
        # the answer key sitting next to itself.
        cmd = [sys.executable, str(scorer), str(sub_path)]
        if args.ground_truth:
            cmd += ["--ground-truth", args.ground_truth]
        res = subprocess.run(cmd, capture_output=True, text=True)

        score = None
        for line in (res.stdout or "").splitlines():
            if "FINAL SCORE" in line:
                m = re.search(r"(\d+(?:\.\d+)?)", line.split("FINAL SCORE", 1)[1])
                if m:
                    score = float(m.group(1))
        if score is None:
            scored_all = False
            print(f"  {pname}: scorer produced no FINAL SCORE line\n"
                  f"{(res.stderr or res.stdout).strip()[:400]}")
        results[pname] = {
            "verdict_changes": changed,
            "fields_lost_to_noise": fields_lost,
            "final_score": score,
        }
        print(f"  {pname:16s} verdict_changes={changed:3d}  "
              f"fields_lost={fields_lost:3d}  final_score={score}")

    Path(args.out).write_text(json.dumps({
        "seed": args.seed,
        "n_text_pairs": len(txt_pairs),
        "perturbations": results,
    }, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")
    print(f"temp submission dir: {tmp_dir}")
    if not scored_all:
        print("some perturbations could not be scored; treat this run as failed")
    return 0 if scored_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
