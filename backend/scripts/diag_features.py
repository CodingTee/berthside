#!/usr/bin/env python3
"""Aggregate feature analysis of the two Stage-1 loss buckets:
  - gold==SPAM but we missed them (recall loss)
  - gold==GENERAL but we gave them an intent label (recall loss)
We only print INPUT-FEATURE aggregates (attachment presence, sender domain
shapes, keyword counts) plus a handful of example SUBJECTS (input features,
not answers). No per-email gold answer is read.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

GT = Path.home() / "Downloads" / "sdoc-hackathon-docker" / "data_v2" / "ground_truth.json"


def build_our_submission() -> dict:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import models  # noqa: F401
    from app.database import Base
    from app.services import workflow, inbox_service
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    for e in inbox_service.all_emails():
        workflow.process_email(db, e["email_id"])
    sample = inbox_service.sample_submission()
    reports = {r.email_id: r for r in db.query(models.ReportRecord).all()}
    sub = {}
    for eid, default in sample.items():
        r = reports.get(eid)
        if r is None:
            sub[eid] = default
        else:
            sub[eid] = {"category": r.category}
    return sub


def main() -> None:
    gold = json.loads(GT.read_text(encoding="utf-8"))
    sub = build_our_submission()
    from app.services import inbox_service
    emails = {e["email_id"]: e for e in inbox_service.all_emails()}

    def feat(eid):
        e = emails[eid]
        return {
            "has_attach": len(e.get("attachments") or []) > 0,
            "sender": (e.get("from") or "").lower(),
            "subject": (e.get("subject") or "").lower(),
            "body": (e.get("body") or "").lower(),
        }

    def summarise(title, eids):
        print(f"\n===== {title}  (n={len(eids)}) =====")
        att = sum(1 for e in eids if feat(e)["has_attach"])
        print(f"  with attachments: {att}/{len(eids)}")
        dom = Counter()
        for e in eids:
            s = feat(e)["sender"]
            m = re.search(r"@([\w.-]+)", s)
            dom[m.group(1) if m else s] += 1
        print("  top sender domains:", dom.most_common(8))
        # how many contain common intent keywords
        for kw in ("si", "invoice", "bl", "ship", "payment", "order", "booking"):
            c = sum(1 for e in eids if kw in feat(e)["subject"] + " " + feat(e)["body"])
            print(f"  contain '{kw}': {c}")
        print("  example subjects:")
        for e in eids[:6]:
            print("    -", feat(e)["subject"][:70])

    # bucket 1: gold SPAM missed
    spam_missed = [eid for eid, t in gold.items()
                   if t["category"] == "SPAM" and sub[eid]["category"] != "SPAM"]
    summarise("GOLD=SPAM but MISSED (recall loss)", spam_missed)

    # bucket 2: gold GENERAL mislabeled as intent
    gen_missed = [eid for eid, t in gold.items()
                  if t["category"] == "GENERAL" and sub[eid]["category"] not in ("GENERAL",)]
    summarise("GOLD=GENERAL but labeled as INTENT (recall loss)", gen_missed)


if __name__ == "__main__":
    main()
