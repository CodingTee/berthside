"""Build the ShipMail static inbox from the evaluation corpus.

Why this exists
---------------
The Dashboard reads its corpus from ``data/corpus`` (520 emails).
The ShipMail inbox used to ship with 5 hand-written demo emails, which made
the two surfaces inconsistent ("Dashboard has 500+ files but Gmail has 5").

This script regenerates ``web/shipmail/data/emails.json`` (+ a derived
``shipments.json``) straight from the same bundle the Dashboard uses, so the
inbox always mirrors the Dashboard's dataset and the "process once, store,
reuse" rule still applies: opening the inbox is one static file fetch with
zero backend / pipeline work.

Attachment previews are served on demand by the existing
``GET /api/attachments/{rel_path}`` endpoint, which resolves the bundle
relative path (``attachments/email_001_SI.txt``) against the active
``DATA_SOURCE`` — identical on local dev and on Render.

Usage
-----
    python scripts/generate_shipmail_inbox.py
    python scripts/generate_shipmail_inbox.py --bundle /path/to/bundle \
        --out web/shipmail/data

In the Docker image this runs during build with ``--bundle /bundle`` so the
baked inbox always matches the baked dataset.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent

# Plausible shipment identifiers inside a subject line.
# Order matters: prefer explicit SHP-/5RSG- codes, then "OC <code>" (requires a
# digit right after OC so a word like "DOCS" can't be mis-read as "OC"), then a
# generic 3+ char alnum token followed by 2+ digits (e.g. 5RSG-00133, MEDUUD104332).
_SHIP_RE = re.compile(
    r"(?:SHP-\d+|5RSG-\d+|OC[\s_-]?\d[\w-]*|\b[A-Z0-9]{3,}-?\d{2,}\b)",
    re.IGNORECASE,
)

# Document-kind detection, mirroring the engine's filename token rules
# (``_(SI|BL)\.[a-z]+$`` plus standalone SI/BL/INVOICE stems).
_KIND_RE = [
    (re.compile(r"_(si)\.[a-z0-9]+$", re.I), "SI"),
    (re.compile(r"_(bl)\.[a-z0-9]+$", re.I), "BL"),
    (re.compile(r"invoice|inv[._-]?\d", re.I), "INVOICE"),
    (re.compile(r"_(si)$", re.I), "SI"),
    (re.compile(r"_(bl)$", re.I), "BL"),
]


def detect_kind(filename: str) -> str:
    for rx, kind in _KIND_RE:
        if rx.search(filename):
            return kind
    return "DOC"


def shipment_id_from(subject: str) -> str:
    m = _SHIP_RE.search(subject or "")
    return m.group(0).strip().upper() if m else ""


def friendly_name(email: str) -> str:
    local = (email or "").split("@", 1)[0] or "unknown"
    parts = re.split(r"[._+\-]+", local)
    parts = [p for p in parts if p]
    if not parts:
        return "Unknown"
    if len(parts) == 1:
        return parts[0].capitalize()
    return " ".join(p.capitalize() for p in parts[:3])


def iso_and_label(index: int) -> tuple[str, str]:
    """Synthesize a plausible, descending received timestamp for the demo."""
    base = datetime(2026, 9, 20, 9, 12)
    dt = base - timedelta(days=index // 17, minutes=(index % 17) * 37)
    return dt.strftime("%Y-%m-%dT%H:%M:%S"), dt.strftime("%b %d, %H:%M")


def build(bundle_dir: Path) -> tuple[list[dict], dict[str, dict]]:
    inbox_dir = bundle_dir / "inbox"
    attachments_dir = bundle_dir / "attachments"
    files = sorted(inbox_dir.glob("*.json")) if inbox_dir.is_dir() else []
    if not files:
        raise SystemExit(f"No inbox emails found in {inbox_dir}")

    emails: list[dict] = []
    shipments: dict[str, dict] = {}

    for i, fp in enumerate(files):
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"  ! skip {fp.name}: {exc}")
            continue

        email_id = raw.get("email_id") or fp.stem
        subject = raw.get("subject", "") or ""
        body = raw.get("body", "") or ""
        sender = raw.get("from", "") or ""
        att_paths = raw.get("attachments", []) or []

        attachments = []
        kinds = set()
        for rel in att_paths:
            rel = rel.lstrip("/")
            fname = Path(rel).name
            kind = detect_kind(fname)
            kinds.add(kind)
            size_kb = 0.0
            candidate = attachments_dir / Path(rel).name
            if candidate.is_file():
                size_kb = round(candidate.stat().st_size / 1024.0, 1)
            attachments.append({
                "filename": fname,
                "path": rel,            # bundle-relative; served by /api/attachments
                "size_kb": size_kb,
                "kind": kind,
            })

        sid = shipment_id_from(subject)
        iso, label = iso_and_label(i)
        labels = ["Inbox"]
        if "INVOICE" in kinds:
            labels.append("Finance")
        elif kinds & {"SI", "BL"}:
            labels.append("Shipping")

        emails.append({
            "id": email_id,
            "message_id": email_id,
            "thread_id": email_id,
            "shipment_id": sid,
            "from": sender,
            "from_name": friendly_name(sender),
            "to": "operations@berthside.demo",
            "subject": subject,
            "snippet": (body or "").replace("\n", " ").strip()[:140],
            "received": label,
            "iso_date": iso,
            "unread": i % 4 == 0,
            "starred": i % 23 == 0,
            "labels": labels,
            "body": body,
            "attachments": attachments,
        })

        if sid:
            shipments.setdefault(sid, {
                "shipment_id": sid,
                "status": "INBOX",
                "subject": subject,
                "from": sender,
            })

    # Keep the demo Request-BL narrative available for the mock customer DB.
    for demo in ("SHP-001", "SHP-002", "SHP-003", "SHP-004"):
        shipments.setdefault(demo, {
            "shipment_id": demo,
            "status": "INBOX",
            "subject": f"{demo} demo shipment",
            "from": "operations@berthside.demo",
        })

    emails.sort(key=lambda e: e["iso_date"], reverse=True)
    return emails, shipments


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--bundle",
        default=str(BACKEND_ROOT / "data" / "corpus"),
        help="Path to the evaluation corpus (default: data/corpus)",
    )
    ap.add_argument(
        "--out",
        default=str(BACKEND_ROOT / "web" / "shipmail" / "data"),
        help="Output directory for emails.json / shipments.json",
    )
    args = ap.parse_args()

    bundle = Path(args.bundle).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    print(f"Bundle : {bundle}")
    print(f"Output : {out}")
    emails, shipments = build(bundle)
    if not emails:
        raise SystemExit("Nothing generated — check the bundle path.")

    (out / "emails.json").write_text(
        json.dumps({
            "mailbox": "operations@berthside.demo",
            "note": "Static ShipMail inbox generated from the official hackathon "
                    "bundle. Mirrors the Dashboard corpus so both surfaces show "
                    "the same emails. Opening the inbox costs no backend work.",
            "emails": emails,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out / "shipments.json").write_text(
        json.dumps({"shipments": list(shipments.values())}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Wrote {len(emails)} emails and {len(shipments)} shipments.")


if __name__ == "__main__":
    main()
