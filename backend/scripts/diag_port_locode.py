"""Measure the UN/LOCODE asymmetry risk in the port comparison.

Question: does the compared set contain emails where one document prints a port
with its UN/LOCODE and the other prints the same port without it? If so, what is
the gold verdict, and does the current rule still decide it correctly?

Run:  python scripts/diag_port_locode.py
      python scripts/diag_port_locode.py --data-source ../sdoc-hackathon-bundle

The ground truth is resolved by scripts/sdoc_paths.py, so no absolute path is
baked in here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(BACKEND_ROOT / "scripts"))

import sdoc_paths  # noqa: E402

PORTS = ("port_of_loading", "port_of_discharge")

def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-source", default="", help="bundle folder or dataset URL")
    ap.add_argument("--ground-truth", default=sdoc_paths.default_ground_truth())
    return ap.parse_args()


_args = _parse_args()
if _args.data_source:
    os.environ["DATA_SOURCE"] = _args.data_source
if not _args.ground_truth:
    raise SystemExit(sdoc_paths.missing_file_hint("ground_truth.json"))

from app.services import inbox_service  # noqa: E402
from app.services.extractor import (  # noqa: E402
    extract_fields, normalize, port_code, ports_match,
)

truth = json.loads(Path(_args.ground_truth).read_text(encoding="utf-8"))
emails = {e["email_id"]: e for e in inbox_service.all_emails()}


def pick(attachments: list[str], kind: str) -> str | None:
    return next((a for a in attachments if re.search(rf"_{kind}\.", a, re.I)), None)


def name_without_code(value: str) -> str:
    return re.sub(r"\s*\(?[A-Z]{5}\)?\s*", " ", value).strip(" ,")


rows = []
for eid, email in emails.items():
    gold = truth.get(eid, {})
    if gold.get("category") != "BL_COMPARISON":
        continue
    paths = email.get("attachments") or []
    si_path, bl_path = pick(paths, "SI"), pick(paths, "BL")
    if not si_path or not bl_path:
        continue
    si_fields = extract_fields(inbox_service.read_attachment_text(si_path), "SI").fields
    bl_fields = extract_fields(inbox_service.read_attachment_text(bl_path), "BL").fields
    for field in PORTS:
        si_val = str(si_fields.get(field, "") or "")
        bl_val = str(bl_fields.get(field, "") or "")
        if not si_val or not bl_val:
            continue
        si_norm = str(normalize(field, si_val))
        bl_norm = str(normalize(field, bl_val))
        rows.append(dict(
            email_id=eid, field=field, gold=gold.get("status"),
            si=si_val, bl=bl_val, si_norm=si_norm, bl_norm=bl_norm,
            si_code=port_code(si_val), bl_code=port_code(bl_val),
            same_name=name_without_code(si_val).upper() == name_without_code(bl_val).upper(),
            matched=ports_match(si_norm, bl_norm),
        ))

print("=" * 78)
print("UN/LOCODE asymmetry across the two compared ports (public set)")
print("=" * 78)
print(f"port comparisons examined : {len(rows)}")

presence = Counter((bool(r["si_code"]), bool(r["bl_code"])) for r in rows)
print("\nLOCODE present on (SI, BL):")
for (s, b), n in sorted(presence.items(), key=lambda kv: -kv[1]):
    label = "both sides" if s and b else "neither side" if not (s or b) else "ONE SIDE ONLY"
    print(f"   si={int(s)} bl={int(b)}  {n:>4}  {label}")

asym = [r for r in rows if bool(r["si_code"]) != bool(r["bl_code"])]
print(f"\nasymmetric comparisons: {len(asym)}")
for r in asym:
    print(f"\n   {r['email_id']}  {r['field']}   gold={r['gold']}  same_name={r['same_name']}")
    print(f"      SI: {r['si']!r}")
    print(f"      BL: {r['bl']!r}")
    print(f"      normalised -> {r['si_norm']!r} vs {r['bl_norm']!r}   match={r['matched']}")

print("\n" + "=" * 78)
print("verdict impact")
print("=" * 78)
print("gold status of asymmetric rows:",
      dict(Counter(r["gold"] for r in asym)) or "none")

bad = [r for r in asym if not r["matched"] and r["same_name"]]
bystander = [r for r in asym if not r["matched"]]
print(f"asymmetric rows the current rule calls a MISMATCH : {len(bystander)}")
print(f"  ... of which the port names are otherwise identical: {len(bad)}")

# Sanity check: the rule must still catch the real port mismatches.
truth_mismatch = [
    r for r in rows
    if r["gold"] == "MISMATCH" and not r["matched"]
]
print(f"\nreal port mismatches still caught (gold MISMATCH, rule says no match): {len(truth_mismatch)}")

print("\n" + "=" * 78)
print("is the LOCODE part of the rule load-bearing?  strict (name+code) vs lenient (name only)")
print("=" * 78)


def lenient(a: str, b: str) -> bool:
    return name_without_code(a).upper().replace(",", " ") == name_without_code(b).upper().replace(",", " ")


strict_hit = [r for r in rows if not r["matched"]]
lenient_hit = [r for r in rows if not lenient(r["si"], r["bl"])]
print(f"pairs the strict rule calls a mismatch  : {len(strict_hit)}")
print(f"pairs the lenient rule calls a mismatch : {len(lenient_hit)}")

lost = [r for r in strict_hit if lenient(r["si"], r["bl"])]
print(f"\nreal mismatches the lenient rule would MISS: {len(lost)}")
for r in lost:
    print(f"   gold={r['gold']}  {r['email_id']} {r['field']}")
    print(f"      SI {r['si']!r}")
    print(f"      BL {r['bl']!r}")
    print(f"      LOCODE {r['si_code']} vs {r['bl_code']}")

gained = [r for r in lenient_hit if r["matched"]]
print(f"\nfalse mismatches the lenient rule would ADD: {len(gained)}")
for r in gained:
    print(f"   gold={r['gold']}  {r['email_id']} {r['field']}  SI={r['si']!r} BL={r['bl']!r}")

