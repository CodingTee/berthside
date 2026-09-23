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
* ``OCR_ENABLED`` is honoured here, by ``ocr_available()``, so every entry point
  agrees. The image path used to ignore it entirely and transcribe images even
  with OCR switched off, while the PDF path obeyed it: the container said
  "OCR off" and ran OCR anyway.
* Page count is capped (``_MAX_PAGES``). Rendering was unbounded, so one
  500-page PDF could hold a request open for minutes.

Cost, measured rather than assumed
----------------------------------
A full 520-email run with OCR enabled spends **~35s** here, of which:

    ONNX inference        29.5s   (6 pages, ~4.9s each)
    page rendering         1.4s   (8 render calls)
    engine construction    5.3s   (9 rebuilds of RapidOCR, now cached)

Inference dominates, and it is already the cheapest honest configuration. Cutting
the render DPI to 200 saves about 4s but loses 1 to 2 of the 7 compared fields on
4 of the 6 scanned PDFs, and fragments the heading into "SHIPPING INSTRUC TION".
Since the transcription is what a human reviewer has to verify, the fields are
worth more than the seconds, so ``_RENDER_DPI`` stays at 300. Do not lower it
without re-running that measurement.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from threading import Lock

from app.config import get_settings

log = logging.getLogger(__name__)

# One-time capability probe result. ``None`` = not probed yet.
_OCR_CAPABLE: bool | None = None

# Render resolution for PDF pages. 300 DPI is the measured quality point; see
# the module docstring before changing it.
_RENDER_DPI = 300

# Hard bound on pages rendered and OCR'd per document. Nothing in the corpus
# exceeds one page, so this costs nothing today and bounds a pathological input.
_MAX_PAGES = 20

# The ONNX sessions behind the engine are a single shared object, and running
# OCR concurrently was measured to be no faster (0.86x to 0.99x, because
# inference already saturates the CPU). Serialising therefore costs nothing and
# keeps the cached engine out of two threads at once.
_OCR_LOCK = Lock()

# Minimum per-line confidence we accept from the OCR engine. Below this the
# line is dropped rather than fed to the extractor: a wrong value is worse
# than a missing one (a missing one escalates to a human, a wrong one
# silently produces a false discrepancy).
_MIN_LINE_CONFIDENCE = 0.5

# Vertical tolerance (px) for grouping detected words into the same text line.
_ROW_TOLERANCE = 12


def ocr_available() -> bool:
    """True when OCR is switched on *and* a renderer and engine are importable.

    The switch is read on every call, so it is the one place that decides. Only
    the "are the libraries importable" probe is cached, because that is the
    expensive half and it cannot change while the process runs.
    """
    if not get_settings().ocr_enabled:
        return False
    global _OCR_CAPABLE
    if _OCR_CAPABLE is None:
        _OCR_CAPABLE = _renderer() is not None and _engine() is not None
        if not _OCR_CAPABLE:
            log.info("OCR unavailable: install pypdfium2 + "
                     "rapidocr-onnxruntime to enable scanned-PDF transcription")
    return _OCR_CAPABLE


# ------------------------------------------------------------------ backends
@lru_cache(maxsize=1)
def _renderer():
    """Return a ``(bytes, dpi, max_pages) -> iterable[PIL.Image]`` callable, or None.

    Cached: the probe walks three import paths and was re-run on every OCR call.
    Every backend is lazy or bounded, so a long PDF is never fully materialised.
    """
    try:
        import pypdfium2 as pdfium

        def render(content: bytes, dpi: int = _RENDER_DPI,
                   max_pages: int = _MAX_PAGES):
            doc = pdfium.PdfDocument(content)

            def pages():
                try:
                    for index in range(min(len(doc), max_pages)):
                        yield doc[index].render(scale=dpi / 72.0).to_pil()
                finally:
                    doc.close()

            return pages()

        return render
    except Exception:  # noqa: BLE001 — optional dependency
        pass
    try:
        from pdf2image import convert_from_bytes

        def render(content: bytes, dpi: int = _RENDER_DPI,
                   max_pages: int = _MAX_PAGES):
            # pdf2image applies the bound itself, so the extra pages are never
            # rendered in the first place.
            return convert_from_bytes(content, dpi=dpi,
                                      first_page=1, last_page=max_pages)

        return render
    except Exception:  # noqa: BLE001 — optional dependency
        pass
    try:
        import fitz  # PyMuPDF

        def render(content: bytes, dpi: int = _RENDER_DPI,
                   max_pages: int = _MAX_PAGES):
            import io as _io

            from PIL import Image

            doc = fitz.open(stream=content, filetype="pdf")

            def pages():
                try:
                    for index, page in enumerate(doc):
                        if index >= max_pages:
                            break
                        pix = page.get_pixmap(dpi=dpi)
                        yield Image.open(_io.BytesIO(pix.tobytes("png")))
                finally:
                    doc.close()

            return pages()

        return render
    except Exception:  # noqa: BLE001 — optional dependency
        return None


@lru_cache(maxsize=1)
def _engine():
    """Return a ``PIL.Image -> list[(text, score, y, x)]`` callable, or None.

    Cached because constructing ``RapidOCR()`` loads the ONNX models and costs
    about 0.6s. The old code paid that on every call: nine times across one
    inbox run, to OCR six pages.
    """
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
    At most ``_MAX_PAGES`` pages are read.
    """
    if not ocr_available():
        return None
    render = _renderer()
    if render is None:
        return None
    try:
        pages: list[str] = []
        with _OCR_LOCK:
            engine = None
            for img in render(content):
                if engine is None:
                    # Built lazily. A corrupt PDF raises on the first page, and
                    # the old order loaded the ONNX models (0.6s) before finding
                    # that out; two such files in the corpus paid it for nothing.
                    engine = _engine()
                    if engine is None:
                        return None
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


def ocr_image(content: bytes, filename: str = "") -> str | None:
    """Read a single image or multi-page TIFF image and OCR each frame/page.

    Supports .png, .jpg, .jpeg, .tif, .tiff, .bmp, .webp, plus .heic / .heif
    once `photo.register` has taught Pillow the format. At most ``_MAX_PAGES``
    frames are read, so a multi-frame TIFF cannot hold a request open.
    Returns concatenated extracted text, or None if unreadable / OCR unavailable.
    """
    if not ocr_available():
        return None

    try:
        import io
        from PIL import Image, ImageSequence

        from app.services import photo

        # A phone camera writes HEIC, which Pillow cannot open on its own.
        photo.register()

        # Defend against decompression bomb DOS attacks
        Image.MAX_IMAGE_PIXELS = 50_000_000

        img = Image.open(io.BytesIO(content))
        pages = []
        with _OCR_LOCK:
            engine = None
            for index, frame in enumerate(ImageSequence.Iterator(img)):
                if index >= _MAX_PAGES:
                    log.info("image '%s': stopping at the %d-page cap",
                             filename, _MAX_PAGES)
                    break
                if engine is None:
                    engine = _engine()
                    if engine is None:
                        return None
                frame_rgb = frame.convert("RGB")
                items = engine(frame_rgb)
                if items:
                    text = _items_to_lines(items)
                    if text.strip():
                        pages.append(text)
        combined = "\n".join(pages).strip()
        return combined or None
    except Exception as exc:  # noqa: BLE001
        log.warning("OCR attempt failed for image '%s': %s", filename, exc)
        return None

