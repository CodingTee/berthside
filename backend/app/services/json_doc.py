"""Read a shipping document that arrived as JSON.

A machine-generated SI is often not a document at all: an ERP exports the
shipment as an object, and the attachment is ``SHP-047_SI.json``. Before this
reader existed such a file was classified by name, failed to decode as text,
and was reported unreadable, so the shipment showed every field as missing.

Rather than teach the extractor a second set of rules, the object is flattened
into the same ``label | value`` lines the CSV, XLSX, DOCX and XML readers
already produce. Keys become labels through `labels.label_from_key`, which is
the same rule the XML reader uses, so the two paths cannot drift.

Nesting is flattened by joining the path: ``{"shipment": {"portOfLoading": ...}}``
yields the label "Shipment Port Of Loading", which still contains the printed
label the extractor looks for.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.services.labels import label_from_key

log = logging.getLogger(__name__)

JSON_SUFFIXES = (".json", ".jsonl", ".ndjson")

MAX_TEXT_CHARS = 100_000
# A path prefix repeated on every line is mostly noise, and deep paths stop
# looking like labels at all. Beyond this the remaining keys are emitted
# without the prefix.
MAX_PATH_DEPTH = 4
# A list of scalars becomes one line. Beyond this many items the list stops
# reading as a labelled value and starts looking like a data dump.
MAX_LIST_ITEMS = 20
# Path segments that carry no label meaning, so they are dropped from the
# prefix instead of pushing the real label further right.
_IGNORED_SEGMENTS = frozenset({
    "data", "payload", "content", "contents", "body", "value", "values",
    "fields", "document", "doc", "root", "items", "records", "rows",
})


def _suffix(filename: str) -> str:
    lowered = (filename or "").lower()
    for suffix in JSON_SUFFIXES:
        if lowered.endswith(suffix):
            return suffix
    return ""


def _parse(filename: str, content: bytes) -> Optional[tuple[Any, str]]:
    """Decode the payload, tolerating a UTF-8 BOM and a JSON Lines stream.

    Returns ``(parsed, source)`` where ``source`` names the route that decoded
    it. A record-per-line attachment is reported as ``jsonl`` rather than
    ``json``: the two forms fail differently (a truncated final line is dropped
    from a stream but would sink a whole document), so an operator reading a
    verdict should be able to tell which one was used.
    """
    if not content:
        return None
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("utf-16")
        except UnicodeDecodeError:
            return None

    try:
        return json.loads(text), "json"
    except json.JSONDecodeError:
        pass

    # A .jsonl / .ndjson attachment, or a stream of objects. Each complete line
    # is one record; a truncated trailing line is dropped rather than failing
    # the whole document.
    if _suffix(filename) in (".jsonl", ".ndjson") or text.lstrip().startswith("{") and "\n{" in text:
        records: list[Any] = []
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if records:
            return records, "jsonl"
    return None


def _format_scalar(value: Any) -> str:
    if value is True or value is False:
        # JSON booleans are not values a document prints; "true" would be read
        # as a word and could be mistaken for a name.
        return ""
    if value is None:
        return ""
    if isinstance(value, float):
        # 24960.0 should read as the number the document would print.
        return f"{value:,.2f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}"
    if isinstance(value, int):
        return f"{value:,}"
    return " ".join(str(value).split())


def _walk(node: Any, path: list[str], out: list[tuple[str, str]]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            segment = label_from_key(str(key))
            lowered = str(key).strip().lower()
            child = path if lowered in _IGNORED_SEGMENTS else path + [segment]
            _walk(value, child, out)
        return

    if isinstance(node, (list, tuple)):
        scalars = [v for v in node if not isinstance(v, (dict, list, tuple))]
        containers = [v for v in node if isinstance(v, (dict, list, tuple))]
        if scalars and not containers:
            label = " ".join(p for p in path if p)
            value = ", ".join(filter(None, (_format_scalar(v) for v in scalars[:MAX_LIST_ITEMS])))
            if value:
                out.append((label, value))
            return
        for item in node:
            # A list of objects repeats the same key set, so the path is not
            # indexed; each record contributes its own labelled lines.
            _walk(item, path, out)
        return

    label = " ".join(p for p in path[-MAX_PATH_DEPTH:] if p)
    value = _format_scalar(node)
    if label and value:
        out.append((label, value))


def read_json_text(filename: str, content: bytes) -> Optional[tuple[str, str]]:
    """Flatten a JSON document into ``label | value`` lines.

    Returns ``(text, source)``, or ``None`` when the payload is not JSON at all.
    An empty or valueless object returns ``None`` too: a document that carries
    no fields is not readable, and saying so is what lets the workflow escalate
    instead of reporting a discrepancy against nothing.
    """
    if not _suffix(filename):
        return None
    parsed = _parse(filename, content)
    if parsed is None:
        return None
    document, source = parsed

    pairs: list[tuple[str, str]] = []
    _walk(document, [], pairs)

    lines: list[str] = []
    for label, value in pairs:
        lines.append(f"{label} | {value}" if label else value)
        if sum(len(line) for line in lines) > MAX_TEXT_CHARS:
            break
    text = "\n".join(lines)[:MAX_TEXT_CHARS]
    return (text, source) if text.strip() else None


def detect_json(filename: str, content: Optional[bytes] = None) -> Optional[str]:
    """Claim the document when the suffix says JSON and the body parses."""
    if not _suffix(filename):
        return None
    if content is None:
        return "json"
    return "json" if _parse(filename, content) is not None else None


def is_json(filename: str, content: Optional[bytes] = None) -> bool:
    return detect_json(filename, content) is not None
