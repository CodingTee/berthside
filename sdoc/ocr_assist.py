"""Local OCR assist for image-only PDF attachments (verdict-neutral).

The engine deliberately escalates unreadable documents to NEEDS_REVIEW and
must keep doing so: unreadable is the correct verdict for empty, corrupt and
scanned files, and the evaluation rewards escalating them. This module never
changes a result's status -- it only gives the human reviewer a machine-read
preview of what an unreadable attachment actually contains, shown in the
Review Desk alongside the escalation reason.

Pipeline (fully local, zero API calls):
    PyMuPDF renders the page at 300 dpi
      -> RapidOCR (ONNX models bundled with the wheel, pure pip install)
      -> text boxes merged into lines by row
      -> parsed with the same label synonyms as the regular extractors

Anything unrecoverable (0-byte file, corrupt bytes, no image content, no
readable text) returns ok=False with a reason instead of raising.
"""
from __future__ import annotations

import os
import re

from .extractor import parse_fields, COMPARE_FIELDS, _FLAT_LABELS

_ocr_engine = None
_cache: dict[str, dict] = {}


def _ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


def _render_page_png(path: str, dpi: int = 300) -> bytes | None:
    """Render page 1 to PNG bytes; None if the PDF has no renderable page."""
    import pymupdf
    with pymupdf.open(path) as doc:
        if not doc.page_count:
            return None
        pix = doc[0].get_pixmap(dpi=dpi)
        return pix.tobytes("png")


def _lines_from_png(png: bytes) -> list[dict]:
    """Run OCR on a PNG and return {'text', 'conf', 'y'} per detected box,
    sorted top-to-bottom. RapidOCR returns [box(4x2), text, score]."""
    result, _ = _ocr()(png)
    lines = []
    for box, text, conf in (result or []):
        xs = [p[0] for p in box]; ys = [p[1] for p in box]
        lines.append({"text": str(text).strip(), "conf": float(conf),
                      "x": min(xs), "y": sum(ys) / 4.0})
    lines.sort(key=lambda l: (round(l["y"] / 12.0), l["x"]))  # row-bucket by y
    return [l for l in lines if l["text"]]


_GROSS_RE = re.compile(
    r"(?im)^(?:total\s+)?gross\s*w.{0,8}ght[^:\n]{0,14}:?\s*([\d,]{3,}(?:\.\d+)?)\s*k?g\b")
_CONT_RE = re.compile(
    r"(?im)(?:no\.?\s*of\s*containers(?:\s*or\s*packages)?|(?:total\s+)?containers|container\s*count)\s*:?\s*(\d+)\s*x?")


def _fuzzy_field(line: str):
    """Match an OCR-mangled label ('ShiDer:', 'Consig nee:', 'NotiTy.') to a
    canonical field. The label pool is small and known (pools.LABELS), so a
    difflib window match on the alnum-normalized line prefix works well.
    Returns (field, value) or (None, None). Value is split after the label's
    alnum footprint (smallest window wins ties, so it never eats value chars
    that a longer window would grab)."""
    from difflib import SequenceMatcher
    norm = re.sub(r"[^A-Z0-9]", "", line.upper())
    if len(norm) < 4:
        return None, None
    best = (0.0, None, 0)  # ratio, field, alnum window size
    for lab, field in _FLAT_LABELS:
        ln = re.sub(r"[^A-Z0-9]", "", lab.upper())
        if not ln or len(ln) < 5:
            continue
        for w in range(max(4, len(ln) - 2), len(ln) + 2):
            if w > len(norm):
                break
            ratio = SequenceMatcher(None, norm[:w], ln).ratio()
            if ratio > best[0]:
                best = (ratio, field, w)
    if best[0] < 0.75:
        return None, None
    _, field, w = best
    # walk the original line counting alnum chars until the window is consumed
    seen = 0
    split = len(line)
    for idx, ch in enumerate(line):
        if ch.isalnum():
            seen += 1
            if seen >= w:
                split = idx + 1
                break
    value = line[split:].lstrip(" :.,;").strip()
    # when a real label/value separator sits next to the estimated split,
    # prefer it ('Port of Loadirig: NHAVA...' must split at ':', not mid-label)
    for s in (":", ". "):
        p = line.find(s)
        if p != -1 and abs(p - split) <= 4:
            value = line[p + len(s):].strip()
            break
    return field, value


def _fields_from_text(text: str) -> dict[str, str]:
    """Same synonyms as the regular extractors, plus fuzzy label matching for
    OCR-mangled labels and tolerant regex fallbacks for the two bottom-line
    fields (OCR may mangle their labels beyond fuzzy range)."""
    fields = parse_fields(text)
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        up = line.upper()
        if any(up.startswith(lab.upper()) for lab, f in _FLAT_LABELS):
            continue  # exact match already handled by parse_fields
        field, value = _fuzzy_field(line)
        if field and value and field not in fields:
            fields[field] = value
    if not fields.get("gross_weight_kg"):
        m = _GROSS_RE.search(text)
        if m:
            fields["gross_weight_kg"] = m.group(1)
    if not fields.get("container_count"):
        m = _CONT_RE.search(text)
        if m:
            fields["container_count"] = m.group(1)
    return fields


def ocr_attachment(data_root: str, rel_path: str) -> dict:
    """OCR one attachment. Returns a verdict-neutral preview dict:
    {ok, reason, fields, text, avg_conf}. Never raises; never touches status."""
    full = os.path.join(data_root, rel_path)
    key = f"{data_root}|{rel_path}|{os.path.getmtime(full) if os.path.exists(full) else 0}"
    if key in _cache:
        return _cache[key]

    out: dict = {"rel_path": rel_path, "ok": False, "reason": None,
                 "fields": {}, "text": "", "avg_conf": None}
    try:
        if not os.path.exists(full) or os.path.getsize(full) == 0:
            out["reason"] = "file is missing or empty"
            return out
        if os.path.splitext(rel_path)[1].lower() != ".pdf":
            out["reason"] = "not a PDF"
            return out
        png = _render_page_png(full)
        if png is None:
            out["reason"] = "PDF has no renderable page"
            return out
        boxes = _lines_from_png(png)
        if not boxes:
            out["reason"] = "no readable text found in the rendered image"
            return out
        text = "\n".join(b["text"] for b in boxes)
        out.update({
            "ok": True,
            "text": text,
            "fields": _fields_from_text(text),
            "avg_conf": round(sum(b["conf"] for b in boxes) / len(boxes), 3),
        })
        return out
    except Exception as exc:  # corrupt bytes, encrypted PDFs, OCR runtime issues
        out["reason"] = f"OCR failed: {type(exc).__name__}: {exc}"
        return out
    finally:
        _cache[key] = out


def bl_attachment_of(email: dict) -> str | None:
    atts = email.get("attachments", [])
    return next((a for a in atts if "_BL." in a), None)
