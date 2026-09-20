"""The OCR switch, the page cap and the cached probe.

OCR is optional and, on this corpus, outcome-neutral: switching it off changes
0 of 520 verdicts, reasons and defect lists. So the things worth pinning down
are the controls around it, not its output. What went wrong before was exactly
that: `OCR_ENABLED=0` was obeyed by the PDF reader and ignored by the image
reader, so the container reported OCR off and ran it anyway.
"""
from __future__ import annotations

import inspect
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from app.services import ocr


# ------------------------------------------------------------------- the switch
def test_the_switch_gates_every_entry_point(monkeypatch):
    """One switch, read live, obeyed by both readers.

    `ocr_pdf` used to be guarded by an import-time snapshot of the settings in
    `ai_service`, and `ocr_image` was not guarded at all.
    """
    from app.config import get_settings

    monkeypatch.setenv("OCR_ENABLED", "0")
    get_settings.cache_clear()
    assert ocr.ocr_available() is False
    assert ocr.ocr_pdf(b"%PDF-1.4\n") is None
    assert ocr.ocr_image(b"\x89PNG\r\n\x1a\n", "scan.png") is None

    monkeypatch.setenv("OCR_ENABLED", "1")
    get_settings.cache_clear()
    if ocr._renderer() is None or ocr._engine() is None:
        pytest.skip("OCR toolchain not installed")
    assert ocr.ocr_available() is True


def test_the_switch_is_read_again_after_it_changes(monkeypatch):
    """Caching the switch would repeat the bug in the other direction."""
    from app.config import get_settings

    if ocr._renderer() is None or ocr._engine() is None:
        pytest.skip("OCR toolchain not installed")

    monkeypatch.setenv("OCR_ENABLED", "0")
    get_settings.cache_clear()
    assert ocr.ocr_available() is False

    monkeypatch.setenv("OCR_ENABLED", "1")
    get_settings.cache_clear()
    assert ocr.ocr_available() is True
    get_settings.cache_clear()


# ------------------------------------------------------------------ the budget
def test_a_multi_frame_image_stops_at_the_page_cap(monkeypatch):
    """An unbounded loop turns one attachment into an unbounded request.

    A 500-frame TIFF held the request open until the last frame finished. The
    corpus has no such file, so this is a bound on a pathological input rather
    than a fix for an observed failure.
    """
    frames = [Image.new("RGB", (80, 24), "white")
              for _ in range(ocr._MAX_PAGES + 7)]
    buf = io.BytesIO()
    frames[0].save(buf, format="TIFF", save_all=True, append_images=frames[1:])

    seen = []

    def fake_engine():
        def run(_img):
            seen.append(1)
            return [("Gross Weight (KG): 21,577 KG", 0.99, 0.0, 0.0)]

        return run

    monkeypatch.setattr(ocr, "_OCR_CAPABLE", True)
    monkeypatch.setattr(ocr, "_engine", fake_engine)

    text = ocr.ocr_image(buf.getvalue(), "many_frames.tif")

    assert len(seen) == ocr._MAX_PAGES
    assert text


def test_every_renderer_backend_accepts_the_page_bound():
    """The bound has to be wired into each backend, not just the one in use.

    pypdfium2 is what ships today; pdf2image and PyMuPDF are alternates that
    take over when it is absent. A new backend added without the parameter
    would silently lose the bound.
    """
    render = ocr._renderer()
    if render is None:
        pytest.skip("no PDF renderer installed")

    params = inspect.signature(render).parameters
    assert "max_pages" in params
    assert params["max_pages"].default == ocr._MAX_PAGES
    assert "dpi" in params
    assert params["dpi"].default == ocr._RENDER_DPI


def test_the_render_dpi_is_not_lowered_without_remeasuring():
    """300 DPI is a measured choice, not a default.

    At 200 DPI the six scanned corpus PDFs lose 1 to 2 of the 7 compared fields
    on four of them, and the heading comes back as "SHIPPING INSTRUC TION". The
    transcription is what a human reviewer has to verify, so the seconds the
    lower resolution saves (about 4s of a 35s run) are not worth the loss.
    """
    assert ocr._RENDER_DPI == 300


def test_the_toolchain_is_probed_once():
    """`RapidOCR()` loads ONNX models and used to be rebuilt on every call.

    Measured at about 0.6s each, paid nine times across one inbox run to OCR
    six pages.
    """
    assert ocr._renderer() is ocr._renderer()
    assert ocr._engine() is ocr._engine()
