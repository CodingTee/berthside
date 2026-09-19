"""Best-effort OCR fallback for image-only / scanned PDF attachments.

Why this exists
---------------
The deterministic extractor reads *text*. Machine-generated PDFs expose a text
layer via ``pypdf``; a **scanned / image-only** PDF does not, so without this
module the workflow escalates it as ``unreadable`` and a real discrepancy can be
silently dropped. OCR transcribes the pixels so the case can still be decided.

Toolchain (all optional, all pip-installable, no system binaries required)
-------------------------------------------------------------------------
* renderer : ``pypdfium2`` (preferred, ships with pdfplumber) | ``pdf2image``
             (needs poppler) | ``PyMuPDF``
* engine   : ``rapidocr-onnxruntime`` (preferred, bundled ONNX models, offline)
             | ``pytesseract`` (needs the tesseract binary)

Robustness contract
-------------------
* Every dependency is optional. If anything is missing or fails, ``ocr_pdf``
  returns ``None`` and the caller keeps escalating as ``unreadable`` — OCR is a
  pure improvement, never a regression.
* The function never raises: OCR failures are logged and swallowed.
* On the static hackathon bundle every PDF has a text layer, so this path is
  never reached and the local score is unchanged.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# One-time capability probe result. ``None`` = not probed yet.
_OCR_CAPABLE: bool | None = None

# Minimum per-line confidence we accept from the OCR engine. Below this the
# line is dropped rather than fed to the extractor: a wrong value is worse
# than a missing one (a missing one escalates to a human, a wrong one
# silently produces a false discrepancy).
_MIN_LINE_CONFIDENCE = 0.5

# Vertical tolerance (px) for grouping detected words into the same text line.
_ROW_TOLERANCE = 12


def ocr_available() -> bool:
    """True only when a renderer *and* an OCR engine are importable."""
    global _OCR_CAPABLE
    if _OCR_CAPABLE is not None:
        return _OCR_CAPABLE
    _OCR_CAPABLE = _renderer() is not None and _engine() is not None
    if not _OCR_CAPABLE:
        log.info("OCR unavailable: install pypdfium2 + rapidocr-onnxruntime "
                 "to enable scanned-PDF transcription")
    return _OCR_CAPABLE


# ------------------------------------------------------------------ backends
def _renderer():
    """Return a ``bytes -> list[PIL.Image]`` callable, or None."""
    try:
        import pypdfium2 as pdfium

        def render(content: bytes, dpi: int = 300):
            doc = pdfium.PdfDocument(content)
            scale = dpi / 72.0
            return [doc[i].render(scale=scale).to_pil() for i in range(len(doc))]

        return render
    except Exception:  # noqa: BLE001 — optional dependency
        pass
    try:
        from pdf2image import convert_from_bytes

        def render(content: bytes, dpi: int = 300):
            return convert_from_bytes(content, dpi=dpi)

        return render
    except Exception:  # noqa: BLE001 — optional dependency
        pass
    try:
        import fitz  # PyMuPDF

        def render(content: bytes, dpi: int = 300):
            doc = fitz.open(stream=content, filetype="pdf")
            out = []
            for page in doc:
                pix = page.get_pixmap(dpi=dpi)
                from PIL import Image
                import io as _io
                out.append(Image.open(_io.BytesIO(pix.tobytes("png"))))
            return out

        return render
    except Exception:  # noqa: BLE001 — optional dependency
        return None


def _engine():
    """Return a ``PIL.Image -> list[(text, score, y, x)]`` callable, or None."""
    try:
        from rapidocr_onnxruntime import RapidOCR

        ocr = RapidOCR()

        def run(img):
            import numpy as np
            arr = np.array(img.convert("RGB"))[:, :, ::-1]  # RGB -> BGR
            result, _ = ocr(arr)
            items = []
            for box, text, score in result or []:
                if not text or not text.strip():
                    continue
                ys = [p[1] for p in box]
                xs = [p[0] for p in box]
                items.append((text.strip(), float(score),
                              min(ys), min(xs)))
            return items

        return run
    except Exception:  # noqa: BLE001 — optional dependency
        pass
    try:
        import pytesseract

        def run(img):
            data = pytesseract.image_to_data(
                img, lang="eng", config="--psm 6",
                output_type=pytesseract.Output.DICT)
            items = []
            for text, conf, top, left in zip(
                    data.get("text", []), data.get("conf", []),
                    data.get("top", []), data.get("left", [])):
                if not text or not str(text).strip():
                    continue
                try:
                    score = float(conf) / 100.0
                except (TypeError, ValueError):
                    score = 1.0
                items.append((str(text).strip(), score, float(top), float(left)))
            return items

        return run
    except Exception:  # noqa: BLE001 — optional dependency
        return None


# ---------------------------------------------------------------------- main
def _items_to_lines(items) -> str:
    """Group recognised words into reading-order lines (top-to-bottom)."""
    ordered = sorted(items, key=lambda it: (it[2], it[3]))
    lines: list[list[tuple]] = []
    for item in ordered:
        if lines and abs(item[2] - lines[-1][0][2]) <= _ROW_TOLERANCE:
            lines[-1].append(item)
        else:
            lines.append([item])

    out = []
    for row in lines:
        words = [t for t, score, _y, _x in sorted(row, key=lambda it: it[3])
                 if score >= _MIN_LINE_CONFIDENCE]
        if words:
            out.append(" ".join(words))
    return "\n".join(out)


def ocr_pdf(content: bytes) -> str | None:
    """Render each PDF page and OCR it. Returns concatenated text, or None.

    Returns ``None`` when OCR is disabled, the toolchain is missing, or nothing
    readable came back — the caller then escalates the case as ``unreadable``.
    """
    if not ocr_available():
        return None
    render = _renderer()
    engine = _engine()
    if render is None or engine is None:
        return None
    try:
        pages = []
        for img in render(content):
            items = engine(img)
            if items:
                text = _items_to_lines(items)
                if text.strip():
                    pages.append(text)
        combined = "\n".join(pages).strip()
        return combined or None
    except Exception as exc:  # noqa: BLE001 — OCR is best-effort only
        log.warning("OCR attempt failed for a PDF: %s", exc)
        return None
