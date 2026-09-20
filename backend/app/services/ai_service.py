"""AI service layer — the integration point for P3.

Providers
---------
rule    : deterministic classifier + regex extractor (default, zero deps)
remote  : P3's AI microservice over HTTP (classify + extract endpoints)
hybrid  : remote first, rule engine as fallback when the service is down

The contract with P3 (agree this with them, then adjust `RemoteAIService`):

    POST {AI_SERVICE_URL}/classify
        {"email": {email_id, from, subject, body, attachments}}
        -> {"category": "...", "confidence": 0.0-1.0}

    POST {AI_SERVICE_URL}/extract
        {"doc_type": "SI"|"BL", "filename": "...", "content_base64": "..."}
        -> {"fields": {shipper: ..., consignee: ..., notify_party: ...,
                       port_of_loading: ..., port_of_discharge: ...,
                       container_count: ..., gross_weight_kg: ...},
            "readable": true}

Everything downstream (comparison) is deterministic regardless of provider.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from app.config import get_settings
from app.services import extractor
from app.services.classifier import Classification, classify as rule_classify

log = logging.getLogger(__name__)
settings = get_settings()


# --------------------------------------------------------------------------
def classify_email(email: dict) -> Classification:
    provider = settings.ai_provider
    if provider in ("remote", "hybrid", "cascade"):
        try:
            if provider == "cascade":
                from app.services.llm_gateway import gateway
                res = gateway.classify_ambiguous_email(email)
                return Classification(res["category"], res["confidence"], res["source"])
            return _remote_classify(email)
        except Exception as exc:  # noqa: BLE001 — degrade, never crash
            log.warning("remote/cascade classify failed (%s); provider=%s",
                        exc, provider)
            if provider == "remote":
                raise
    return rule_classify(email)


def extract_document(doc_type: str, filename: str, content: bytes) -> extractor.ExtractionResult:
    """Extract fields from one attachment, via AI provider or rules."""
    provider = settings.ai_provider
    if provider in ("remote", "hybrid", "cascade"):
        try:
            if provider == "cascade":
                from app.services.llm_gateway import gateway
                image_exts = (".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".webp")
                if any(filename.lower().endswith(ext) for ext in image_exts):
                    res_dict = gateway.extract_from_image(content, filename, doc_type)
                    remote = extractor.ExtractionResult(
                        doc_type=doc_type,
                        fields=res_dict.get("fields", {}),
                        readable=res_dict.get("readable", True),
                    )
                    remote.missing = extractor.missing_of(remote.fields)
                    return _merge_with_local(remote, doc_type, filename, content)
                # For non-images in cascade mode: try local reader first, if yield < 4 use LLM
                local = _rule_extract(doc_type, filename, content)
                if local.readable and len(local.missing) <= 3:
                    return local
                text, _ = document_text(filename, content)
                if text:
                    res_dict = gateway.extract_from_unstructured_text(text, doc_type)
                    remote = extractor.ExtractionResult(
                        doc_type=doc_type,
                        fields=res_dict.get("fields", {}),
                        readable=res_dict.get("readable", True),
                    )
                    remote.missing = extractor.missing_of(remote.fields)
                    return _merge_with_local(remote, doc_type, filename, content)
                return local
            remote = _remote_extract(doc_type, filename, content)
            # An AI service that cannot read a PDF/Word/Excel must not make us
            # worse than the local reader — fill whatever it missed.
            return _merge_with_local(remote, doc_type, filename, content)
        except Exception as exc:  # noqa: BLE001
            log.warning("extract failed for %s (%s); provider=%s",
                        filename, exc, provider)
            if provider == "remote":
                raise
    return _rule_extract(doc_type, filename, content)


def _merge_with_local(remote: extractor.ExtractionResult, doc_type: str,
                      filename: str, content: bytes) -> extractor.ExtractionResult:
    """Use the AI's fields, but backfill from local parsing where it fell short.

    Guarantees the hybrid path is never worse than the rule path.
    """
    if remote.readable and not remote.missing:
        return remote

    local = _rule_extract(doc_type, filename, content)
    if not local.readable:
        return remote if remote.fields else local

    merged = dict(remote.fields)
    for field, value in local.fields.items():
        merged.setdefault(field, value)
    remote.fields = merged
    remote.missing = extractor.missing_of(merged)
    remote.readable = True
    return remote


# ------------------------------------------------------------ rule provider
def _rule_extract(doc_type: str, filename: str, content: bytes) -> extractor.ExtractionResult:
    name = filename.lower()
    if name.endswith(".txt"):
        return extractor.extract_fields(
            content.decode("utf-8", errors="replace"), doc_type)

    # Non-plain-text attachments (xlsx/pdf/docx) — parsed when the optional
    # reader library is installed; otherwise escalated for review, where a
    # remote AI provider (OCR / vision) or a human handles it.
    extracted = _try_read_binary(name, content, doc_type)
    if extracted is not None:
        text, source = extracted
        if text:
            result = extractor.extract_fields(text, doc_type)
            # Keep the reader that produced the text: "pdf-ocr" means a human
            # should spot-check it (OCR can misread digits), and it makes the
            # processing path visible instead of a black box.
            result.source = source
            return result

    result = extractor.ExtractionResult(doc_type=doc_type, readable=False)
    result.missing = list(extractor.COMPLETENESS_FIELDS)
    return result


def _yield(text: str | None, doc_type: str | None) -> int:
    """How many of the 7 compared fields the extractor can read from `text`."""
    if not text or not doc_type:
        return 0
    try:
        return len(extractor.COMPLETENESS_FIELDS) - len(
            extractor.extract_fields(text, doc_type).missing)
    except Exception:  # noqa: BLE001 — never let a probe break the reader
        return 0


def _read_pdf(content: bytes, doc_type: str | None) -> Optional[tuple[str, str]]:
    """Read a PDF through an escalating ladder of readers.

    1. **Text layer** (``pypdf``) — fast, exact, covers machine-generated PDFs.
    2. **Layout / tables** (``pdfplumber``) — recovers PDFs whose content is
       laid out as a *table*: pypdf emits cell text in a broken order, while
       pdfplumber can walk the grid and re-emit ``label | value`` rows.
    3. **Pixels** (OCR) — the only way to read a *scanned / image-only* PDF.

    Each rung only runs when the previous one is missing too much (fewer than
    4 of the 7 compared fields), and the best-scoring result wins. Every rung
    is optional: if a library is absent or the file is corrupt, the ladder
    returns ``None`` and the caller escalates as ``unreadable``.
    """
    import io

    best: tuple[str, str] | None = None
    best_score = -1

    # ---- rung 1: embedded text layer -------------------------------------
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        text = _clean_extracted(
            "\n".join((p.extract_text() or "") for p in reader.pages))
    except Exception as exc:  # noqa: BLE001 — corrupt/truncated file
        log.info("pypdf could not read a PDF: %s", exc)
        text = ""
    if text:
        best, best_score = (text, "pdf"), _yield(text, doc_type)

    # ---- rung 2: layout-aware + table extraction -------------------------
    if best_score < 4:
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                chunks = []
                for page in pdf.pages:
                    # Emit table rows first: "label | value" is exactly what
                    # the extractor's regexes are written for.
                    for table in (page.extract_tables() or []):
                        for row in table:
                            cells = [str(c).strip() for c in row
                                     if c is not None and str(c).strip()]
                            if cells:
                                chunks.append(" | ".join(cells))
                    page_text = page.extract_text() or ""
                    if page_text:
                        chunks.append(page_text)
            text2 = _clean_extracted("\n".join(chunks))
        except Exception as exc:  # noqa: BLE001 — optional dependency
            log.info("pdfplumber could not read a PDF: %s", exc)
            text2 = ""
        if text2:
            score2 = _yield(text2, doc_type)
            if score2 > best_score:
                best, best_score = (text2, "pdf-tables"), score2

    # ---- rung 3: OCR the rendered pages ----------------------------------
    # No switch test here on purpose. `ocr_pdf` opens with `ocr_available()`,
    # which reads OCR_ENABLED live; this module holds an import-time snapshot of
    # the settings, so testing `settings.ocr_enabled` here meant a change made
    # after startup never took effect. One place decides, and it is that one.
    if best_score < 4:
        try:
            from app.services.ocr import ocr_pdf
            text3 = _clean_extracted(ocr_pdf(content) or "")
        except Exception as exc:  # noqa: BLE001 — OCR is best-effort
            log.warning("OCR failed for a PDF: %s", exc)
            text3 = ""
        if text3:
            score3 = _yield(text3, doc_type)
            if score3 > best_score:
                best, best_score = (text3, "pdf-ocr"), score3

    return best


# A legacy .doc stream is a mix of binary structures and text, so any reading of
# it is a guess. Require this many of the 7 compared fields as evidence before
# trusting one: below it the honest answer is `unreadable`, and a human decides.
_MIN_LEGACY_FIELDS = 2


def _lines_from_stream(text: str) -> str:
    """Keep the printable, word-bearing lines of a decoded legacy stream."""
    lines = []
    for part in re.split(r"[\r\n\x07\x0c\x0b\x1e\x1f]+", text):
        clean = re.sub(r"[^\x20-\x7E\t]+", " ", part).strip()
        if len(clean) >= 4 and any(c.isalpha() for c in clean):
            lines.append(clean)
    return "\n".join(lines)


def _decode_ole_stream(stream: bytes, doc_type: str | None) -> tuple[int, str]:
    """Decode a WordDocument stream, keeping whichever reading the extractor can use.

    Word 97 stores its text as UTF-16LE. Decoding the raw stream as latin-1
    therefore leaves a NUL between every character, and the label regexes then
    see "S h i p p e r" and match nothing, while the result still counts as
    readable: the case reached the comparison with all seven fields empty and a
    reason of `missing_value`, blaming the sender for a document we failed to
    read. Scoring both plausible decodings with the extractor itself is the
    cheapest honest arbiter, because the extractor is what has to read it.

    Returns `(fields_found, text)`.
    """
    candidates = []
    for encoding in ("utf-16-le", "latin-1"):
        # NULs are consumed by the utf-16 decode; what is left over marks the
        # binary padding between 2-byte text runs.
        decoded = stream.decode(encoding, errors="ignore").replace("\x00", "")
        candidates.append(_lines_from_stream(decoded))
    scored = [(_yield(text, doc_type), text) for text in candidates]
    return max(scored, key=lambda pair: pair[0])


def _try_read_binary(filename: str, content: bytes,
                     doc_type: str | None = None) -> Optional[tuple[str, str]]:
    """Best-effort plain-text extraction from non-txt attachments.

    Returns ``(text, source)`` where ``source`` records which reader produced
    the text. Returns ``None`` when no reader is available or the file is
    unreadable (e.g. a scanned/image-only PDF) — the caller then escalates the
    case for OCR / a vision model (P3) / a human.

    NOTE: layout-aware PDF *table* extraction and OCR are intentionally left to
    P3's AI (the advanced stage). ``pypdf`` recovers running text; when a PDF is
    image-only it yields nothing and we escalate rather than guess.
    """
    import io

    try:
        if filename.endswith(".xlsx"):
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(content), data_only=True)
            lines = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) for c in row if c is not None]
                    if cells:
                        lines.append(" | ".join(cells))
            return ("\n".join(lines) or None), "xlsx"

        if filename.endswith(".xls"):
            try:
                import xlrd
                wb = xlrd.open_workbook(file_contents=content)
                lines = []
                for sheet in wb.sheets():
                    for row_idx in range(sheet.nrows):
                        cells = [str(sheet.cell_value(row_idx, c)).strip() for c in range(sheet.ncols)]
                        cells = [c for c in cells if c]
                        if cells:
                            lines.append(" | ".join(cells))
                text = _clean_extracted("\n".join(lines))
                return (text or None), "xls"
            except Exception as exc:
                log.info("xlrd failed for %s: %s", filename, exc)
                return None

        if filename.endswith(".csv"):
            # Spreadsheet exports carry one label per cell, exactly like .xlsx.
            # Joining cells with " | " reuses the separator the extractor
            # already accepts, so no extraction rule has to change.
            import csv as _csv

            try:
                raw = content.decode("utf-8-sig", errors="replace")
            except Exception:  # noqa: BLE001 - fall back to a byte-safe codec
                raw = content.decode("latin-1", errors="replace")
            rows: list[str] = []
            try:
                for row in _csv.reader(io.StringIO(raw)):
                    cells = [c.strip() for c in row if c and c.strip()]
                    if cells:
                        rows.append(" | ".join(cells))
            except Exception as exc:  # noqa: BLE001 - malformed CSV
                log.info("csv reader failed for %s: %s", filename, exc)
                return None
            joined = _clean_extracted("\n".join(rows))
            return (joined or None), "csv"

        if filename.endswith(".xml"):
            # Tag names are the labels ("<Shipper>ACME</Shipper>"), so the
            # element tree maps straight onto the extractor's label|value form.
            import xml.etree.ElementTree as ET

            try:
                root = ET.fromstring(content.decode("utf-8", errors="replace"))
            except Exception as exc:  # noqa: BLE001 - not well-formed XML
                log.info("xml parse failed for %s: %s", filename, exc)
                return None
            lines: list[str] = []
            for node in root.iter():
                tag = (node.tag or "").split("}")[-1].strip()
                value = " ".join((node.text or "").split()).strip()
                if tag and value:
                    lines.append(f"{_xml_tag_to_label(tag)} | {value}")
            joined = _clean_extracted("\n".join(lines))
            return (joined or None), "xml"

        if filename.endswith(".pdf"):
            return _read_pdf(content, doc_type)

        if filename.endswith(".docx"):
            from docx import Document
            doc = Document(io.BytesIO(content))
            lines = [p.text for p in doc.paragraphs]
            for table in doc.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                    if cells:
                        lines.append(" | ".join(cells))
            text = _clean_extracted("\n".join(lines))
            return (text or None), "docx"

        if filename.endswith(".doc"):
            # 1. In case it was OpenXML docx renamed to .doc
            try:
                from docx import Document
                doc = Document(io.BytesIO(content))
                lines = [p.text for p in doc.paragraphs]
                for table in doc.tables:
                    for row in table.rows:
                        cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                        if cells:
                            lines.append(" | ".join(cells))
                text = _clean_extracted("\n".join(lines))
                if text:
                    return text, "doc-docx"
            except Exception:
                pass

            # 2. Compound File Binary Format (OLE2) .doc parsing
            try:
                import olefile
                if olefile.isOleFile(io.BytesIO(content)):
                    with olefile.OleFileIO(io.BytesIO(content)) as ole:
                        if ole.exists("WordDocument"):
                            stream = ole.openstream("WordDocument").read()
                            score, text = _decode_ole_stream(stream, doc_type)
                            # Evidence gate: a stream this reader can barely
                            # decode must be escalated, not compared. A wrong
                            # value read out of binary noise looks exactly like
                            # a real discrepancy, so guessing here would
                            # manufacture a defect that does not exist.
                            if score >= _MIN_LEGACY_FIELDS:
                                return text, "doc-ole"
                            log.info(
                                "legacy .doc yielded %d/%d fields; escalating as "
                                "unreadable rather than guessing",
                                score, len(extractor.COMPLETENESS_FIELDS))
            except Exception as exc:
                log.info("olefile failed for %s: %s", filename, exc)
                return None

        image_exts = (".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".webp")
        if any(filename.endswith(ext) for ext in image_exts):
            try:
                from app.services.ocr import ocr_image
                text = ocr_image(content, filename)
                if text:
                    return _clean_extracted(text), "image-ocr"
            except Exception as exc:
                log.warning("Image OCR failed for %s: %s", filename, exc)
                return None
    except Exception as exc:  # noqa: BLE001 — library missing or file corrupt
        log.info("optional reader failed for %s: %s", filename, exc)
        return None
    return None



def document_text(filename: str, content: bytes) -> tuple[Optional[str], str]:
    """Best-effort plain text for any attachment the readers can open.

    Separate from :func:`extract_document` because some documents are stored
    without being compared (a commercial invoice, for example). It returns
    ``(text, source)``; ``source`` is ``unreadable`` when no reader could open
    the file, which the caller must surface rather than paper over.
    """
    name = (filename or "").lower()
    if name.endswith(".txt"):
        return content.decode("utf-8", errors="replace"), "txt"
    got = _try_read_binary(name, content, None)
    if got and got[0]:
        return got[0], got[1]
    return None, "unreadable"


def _xml_tag_to_label(tag: str) -> str:
    """Turn a camel-case XML tag into words the extractor recognises.

    XML element names cannot contain spaces, so a shipping-document schema has
    to spell its labels ``<PortOfLoading>``. The extractor's label rules are
    written against how those labels are *printed* — "Port of Loading" — so
    without this the tag never matches and every field silently reads as null.

    Only word boundaries are inserted; nothing is renamed or guessed, so a tag
    that already matches (``<Shipper>``) is passed through unchanged.
    """
    import re as _re

    spaced = _re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tag)
    spaced = _re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    spaced = spaced.replace("_", " ")
    return _re.sub(r"\s+", " ", spaced).strip()


def _clean_extracted(text: str) -> str:
    """Normalise extracted text so the regex extractor can read it reliably.

    Collapses runs of whitespace (PDFs often interleave spaces between
    characters), drops null bytes, and strips leading/trailing blank lines.
    """
    if not text:
        return ""
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]{2,}", "  ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------- remote provider
def _post_json(path: str, payload: dict) -> dict:
    """Call the AI service with bounded exponential backoff.

    Failures are classified so the caller can surface a *visible* reason to the
    frontend instead of failing silently. The rule engine is always available as
    the fallback (see `classify_email` / `extract_document`), so an AI outage is
    a degraded mode, never a crash.
    """
    url = settings.ai_service_url.rstrip("/") + path
    headers = {"Content-Type": "application/json"}
    if settings.ai_api_key:
        headers["Authorization"] = f"Bearer {settings.ai_api_key}"

    last_exc: Optional[Exception] = None
    for attempt in range(1 + settings.ai_max_retries):
        try:
            req = Request(url, data=json.dumps(payload).encode(), headers=headers)
            with urlopen(req, timeout=settings.ai_timeout_seconds) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:  # 4xx/5xx — retrying won't help, fail fast
            last_exc = exc
            log.warning("AI service HTTP %s on %s: %s", exc.code, path, exc)
            raise ConnectionError(
                f"AI service returned HTTP {exc.code} on {path}") from exc
        except URLError as exc:  # network / timeout → retry with backoff
            last_exc = exc
            if attempt < settings.ai_max_retries:
                backoff = 0.2 * (2 ** attempt)  # 0.2s, 0.4s, ...
                log.warning("AI service attempt %d failed (%s); retrying in %.1fs",
                            attempt + 1, exc, backoff)
                time.sleep(backoff)
            else:
                log.warning("AI service attempt %d failed: %s", attempt + 1, exc)
    raise ConnectionError(f"AI service unreachable after retries: {last_exc}")


def _remote_classify(email: dict) -> Classification:
    data = _post_json("/classify", {"email": email})
    category = str(data.get("category", "")).upper()
    valid = {"BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"}
    if category not in valid:
        raise ValueError(f"AI returned invalid category: {category!r}")
    return Classification(category, float(data.get("confidence", 1.0)),
                           "remote AI")


def _remote_extract(doc_type: str, filename: str,
                    content: bytes) -> extractor.ExtractionResult:
    payload = {
        "doc_type": doc_type,
        "filename": filename,
        "content_base64": base64.b64encode(content).decode("ascii"),
    }
    data = _post_json("/extract", payload)
    fields: dict[str, Any] = data.get("fields") or {}
    result = extractor.ExtractionResult(
        doc_type=doc_type,
        fields={k: v for k, v in fields.items() if v is not None},
        readable=bool(data.get("readable", True)),
    )
    result.missing = extractor.missing_of(result.fields)
    return result
