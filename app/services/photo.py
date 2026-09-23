"""Read HEIC / HEIF photographs, which is what a phone camera produces.

An operator photographing a stamped BL on an iPhone attaches ``IMG_4821.HEIC``.
Nothing in the pipeline could open one: Pillow has no HEIF codec of its own, the
OCR engine took an `Image.open` that raised, and the vision providers were handed
the raw bytes labelled ``image/jpeg`` (their mime map has no entry for heic), so
the file was reported unreadable and escalated for no reason.

``pillow-heif`` supplies the codec. It is an optional dependency, so everything
here degrades to "cannot read this" when it is absent rather than failing at
import time.

Two entry points, because the two consumers need different shapes:

* :func:`register` teaches Pillow the format process-wide, which is what lets
  ``ocr_image`` open a HEIC through its existing ``Image.open`` call.
* :func:`to_png` converts one payload to PNG bytes, which is what the vision
  providers need since their mime map cannot describe HEIF.

Neither writes anything to disk.
"""
from __future__ import annotations

import io
import logging
from typing import Optional

log = logging.getLogger(__name__)

HEIF_SUFFIXES = (".heic", ".heif", ".hif", ".avif")

# The ISO base media file format puts a major brand at offset 8. These are the
# ones that mean "a still image encoded as HEIF/AVIF"; a video brand (``isom``,
# ``mp42``) shares the container but is not a document.
HEIF_BRANDS = frozenset({
    b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs",
    b"mif1", b"msf1", b"avif", b"avis",
})

# A phone photo is 12 to 48 megapixels. Neither OCR nor a vision model gains
# anything above this, and a PNG of a 48MP frame would blow past the attachment
# cap after conversion.
MAX_PIXELS = 50_000_000
MAX_SIDE = 4000

_registered = False


def _pillow_heif():
    """Import pillow-heif and register its opener with Pillow, once."""
    global _registered
    try:
        import pillow_heif  # noqa: F401
    except Exception:  # noqa: BLE001 - optional dependency
        return None
    if not _registered:
        try:
            from PIL import Image
            pillow_heif.register_heif_opener()
            Image.MAX_IMAGE_PIXELS = MAX_PIXELS
            _registered = True
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not kill OCR
            log.info("pillow-heif registration failed: %s", exc)
    return pillow_heif if _registered else None


def heif_available() -> bool:
    """True when HEIC/HEIF can actually be decoded here."""
    return _pillow_heif() is not None


def register() -> bool:
    """Make Pillow able to open HEIC/HEIF. Returns whether it worked."""
    return _pillow_heif() is not None


def looks_like_heif(content: bytes) -> bool:
    """Magic check: an ISO-BMFF box with a still-image brand at offset 8."""
    head = bytes(content or b"")[:16]
    return len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in HEIF_BRANDS


def is_heif(filename: str, content: Optional[bytes] = None) -> bool:
    """True when this is a HEIF image, by magic first and name second.

    The magic test matters for ``.hif`` and for a photo whose name was rewritten
    in transit; the suffix test matters for a HEIC whose header was truncated by
    a mail gateway. Claiming by suffix is safe because the next step is a decode
    attempt: if it fails, the payload is handed on unchanged and the reader
    reports it unreadable instead of pretending it was converted.
    """
    if content and looks_like_heif(content):
        return True
    name = str(filename or "").lower()
    return any(name.endswith(suffix) for suffix in HEIF_SUFFIXES)


def to_png(content: bytes) -> Optional[bytes]:
    """Convert a HEIF frame to PNG bytes, or None when it cannot be decoded.

    The first frame is used: a live photo carries a still plus a short video,
    and only the still is a document. EXIF rotation is applied, because a photo
    taken sideways OCRs to nothing.
    """
    if not register():
        return None
    try:
        from PIL import Image, ImageOps, ImageSequence

        Image.MAX_IMAGE_PIXELS = MAX_PIXELS
        with Image.open(io.BytesIO(content)) as img:
            frame = img
            for candidate in ImageSequence.Iterator(img):
                frame = candidate
                break
            frame = ImageOps.exif_transpose(frame) or frame
            if frame.mode not in ("RGB", "L"):
                frame = frame.convert("RGB")
            if max(frame.size) > MAX_SIDE:
                scale = MAX_SIDE / max(frame.size)
                frame = frame.resize(
                    (max(1, int(frame.width * scale)), max(1, int(frame.height * scale))),
                    Image.LANCZOS)
            out = io.BytesIO()
            frame.save(out, format="PNG", optimize=False)
            return out.getvalue()
    except Exception as exc:  # noqa: BLE001 - damaged or unsupported frame
        log.info("HEIF conversion failed (%s): %s", type(exc).__name__, exc)
        return None


def normalize_image(filename: str, content: bytes) -> tuple[str, bytes]:
    """Return ``(filename, content)`` as something every reader can open.

    A HEIC comes back as ``.png`` with PNG bytes. Anything else is returned
    untouched, and a HEIC that cannot be converted is returned untouched too:
    the readers then report it unreadable, which is the honest outcome.
    """
    if not is_heif(filename, content):
        return filename, content
    converted = to_png(content)
    if converted is None:
        log.info("HEIC %s: no decoder available, leaving it unreadable", filename)
        return filename, content
    stem = str(filename or "")
    for suffix in HEIF_SUFFIXES:
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return f"{stem}.png", converted
