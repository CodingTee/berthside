"""Measure the effect of the tier-2 (fuzzy) attachment lookup.

Two questions, both answered with the real engine rather than by reasoning:

1. On the 520-email corpus, does tier 2 change ANY attachment resolution?
   It must not: every attachment matches the "<id>_SI" convention, so tier 1
   should already resolve all of them and tier 2 should never fire.
2. What does tier 2 do with the names real senders actually use, including the
   ones it must refuse? The names below are the acceptance criteria.

Run:  python scripts/diag_filename_fallback.py
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(BACKEND_ROOT / "scripts"))

_STRICT_ONLY = re.compile(r"_(SI|BL)\.[a-z]+$", re.IGNORECASE)


def strict_only(email: dict) -> tuple[str, str]:
    """The pre-change behaviour: convention-only lookup."""
    si = bl = None
    for att in email.get("attachments") or []:
        m = _STRICT_ONLY.search(att)
        if not m:
            continue
        if m.group(1).upper() == "SI" and si is None:
            si = att
        elif m.group(1).upper() == "BL" and bl is None:
            bl = att
    return si, bl


# Names a real forwarder would use, and the verdict the fallback must reach.
CASES: list[tuple[str, str, str | None]] = [
    # -- must resolve (tier 2 fires) -------------------------------------
    ("Draft_BL_v2.pdf", "BL", "token 'bl'"),
    ("BL-102938.pdf", "BL", "token 'bl'"),
    ("Shipping_Instruction_PO123.pdf", "SI", "phrase shippinginstruction"),
    ("shipping instruction 4471.docx", "SI", "phrase shippinginstruction"),
    ("SI.xlsx", "SI", "token 'si'"),
    ("Bol_Nantong.pdf", "BL", "token 'bol'"),
    ("HBL final.pdf", "BL", "token 'hbl'"),
    ("Scan_SI_Form.jpg", "SI", "phrase siform"),
    ("B-L draft.txt", "BL", "phrase 'bldraft' after separator removal"),
    # -- must stay unresolved (ambiguous or wrong kind) -------------------
    ("shipping_invoice.pdf", None, "'shipping' alone is not an SI"),
    ("draft_rates.pdf", None, "'draft' alone is not a BL"),
    ("SI and BL combined.pdf", None, "tier 2: names both, must not pick a side"),
    ("statement_of_account.pdf", None, "no doc-type token"),
    ("Draft_BL_v2.zip", None, "a container we cannot read inside"),
    ("SI", None, "no extension"),
    ("Attachment_1.pdf", None, "silent about the type"),
    # -- tier 1 keeps priority over tier 2 ---------------------------------
    ("SI_and_BL.pdf", "BL", "tier 1 wins: the name ends with _BL.pdf"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-source", default="")
    args = ap.parse_args()
    if args.data_source:
        import os
        os.environ["DATA_SOURCE"] = args.data_source

    from app.services import inbox_service, workflow  # noqa: E402

    emails = inbox_service.all_emails()
    print(f"corpus: {len(emails)} emails")

    changed: list[tuple[str, tuple[str, str], tuple[str, str]]] = []
    unmatched = 0
    for email in emails:
        before = strict_only(email)
        after = workflow._find_doc_attachments(email)
        if before != after:
            changed.append((email["email_id"], before, after))
        for att in email.get("attachments") or []:
            if not _STRICT_ONLY.search(att):
                unmatched += 1

    print(f"attachments not matching the convention: {unmatched}")
    print(f"emails whose resolution changed:        {len(changed)}")
    for eid, before, after in changed:
        print(f"   {eid}: {before} -> {after}")
    if not changed:
        print("   (none, as expected: tier 1 already resolves every attachment)")

    print("\n" + "=" * 72)
    print("tier-2 behaviour on names real senders use")
    print("=" * 72)
    wrong = 0
    for name, expected, why in CASES:
        got_si, got_bl = workflow._find_doc_attachments({"attachments": [name]})
        got = "SI" if got_si else ("BL" if got_bl else None)
        ok = got == expected
        wrong += not ok
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {name:<34} -> {str(got):<5} (expect {expected})  {why}")

    print(f"\n{len(CASES) - wrong}/{len(CASES)} cases as expected")


if __name__ == "__main__":
    main()
