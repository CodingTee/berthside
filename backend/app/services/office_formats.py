"""RTF, OpenDocument and Apple iWork documents as readable text.

These three families reach a shipping mailbox constantly and none of them could
be read before: the name was classified as an SI or a BL, the reader ladder had
no branch for the suffix, and the email escalated as ``unreadable`` with the
document sitting right there.

Three quite different problems, so three quite different readers:

* **RTF** is a plain-text markup language. It is parsed properly rather than
  stripped: control words that open a destination the reader must ignore
  (``\\fonttbl``, ``\\colortbl``, ``\\*\\generator``, an embedded ``\\pict``) are
  skipped as whole groups, because stripping them with a regex leaves their
  contents behind as fake label text. Table cells become ``label | value``,
  which is the shape the extractor already reads.
* **OpenDocument** (``.odt`` / ``.ods`` / ``.odp``) is a ZIP holding
  ``content.xml``. Table rows are joined into ``label | value`` rows and
  paragraphs outside tables become their own lines.
* **iWork** (``.pages`` / ``.numbers`` / ``.key``) is a ZIP whose document body is
  a store of Snappy-compressed protobuf chunks under ``Index/``. Snappy is small
  enough to decode directly, so the document's own text is read and the fields
  are kept; the older ``Index/index.xml`` remains the fallback for packages
  written before iWork '13.

Two rules hold throughout. Reading is best-effort and never raises: a document
that cannot be read returns ``None`` so the caller escalates it honestly as
unreadable instead of comparing half-decoded noise. And nothing here invents a
field: a label that is not in the document stays missing.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Optional

from app.services import extractor

log = logging.getLogger(__name__)

RTF_SUFFIX = ".rtf"
ODF_SUFFIXES = (".odt", ".ods", ".odp", ".odg", ".ott", ".ots", ".otp")
IWORK_SUFFIXES = (".pages", ".numbers", ".key")
OFFICE_SUFFIXES = (RTF_SUFFIX,) + ODF_SUFFIXES + IWORK_SUFFIXES

# A decompressed member is read into memory, so it has to be bounded. A
# content.xml larger than this is not a shipping document.
MAX_MEMBER_BYTES = 24 * 1024 * 1024
# How much text one document may contribute.
MAX_TEXT_CHARS = 200_000

_IWORK_PREVIEW = "QuickLook/Preview.pdf"
_IWORK_LEGACY_INDEX = "Index/index.xml"
_ODF_CONTENT = "content.xml"


# ---------------------------------------------------------------------- detect
def detect_office_format(filename: str, content: Optional[bytes] = None) -> Optional[str]:
    """Name the format, or None when this is not one of them.

    The suffix decides, because these formats share their magic bytes with
    everything else in their family: an ``.ods`` is a ZIP, exactly like a
    ``.docx``, and ``content.xml`` is what tells the two apart.
    """
    name = str(filename or "").lower()
    suffix = Path(name).suffix
    if suffix not in OFFICE_SUFFIXES:
        # A renamed RTF still starts with its own signature.
        if content and bytes(content)[:5] == b"{\\rtf":
            return "rtf"
        return None
    if suffix == RTF_SUFFIX:
        # Some generators omit the header; the suffix is then the only signal.
        return "rtf"
    if suffix in IWORK_SUFFIXES:
        return suffix[1:]
    if content is not None and not zipfile.is_zipfile(io.BytesIO(bytes(content))):
        return None
    return suffix[1:]


def is_office_document(filename: str, content: Optional[bytes] = None) -> bool:
    """True when this module can read the file."""
    return detect_office_format(filename, content) is not None


def is_open_document(content: bytes) -> bool:
    """True when a zip really is OpenDocument and not an OpenXML package."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = set(zf.namelist())
    except Exception:  # noqa: BLE001 - not a readable zip
        return False
    return "mimetype" in names or _ODF_CONTENT in names


# ------------------------------------------------------------------------- rtf
# Groups whose entire contents must be skipped. Their *text* is not document
# text: font names, colour values, revision bookkeeping. A regex strip leaves
# "Times New Roman;Calibri" in the output, which the extractor can read as a
# value, so the group has to go as one unit.
_RTF_SKIP_WORDS = frozenset({
    "fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "nonshppict",
    "themedata", "datastore", "latentstyles", "listtable", "listoverridetable",
    "rsidtbl", "generator", "filetbl", "xmlnstbl", "wgrffmtfilter", "pgptbl",
    "revtbl", "mmathpr", "pgdsctbl", "shpinst", "shprslt", "fldinst",
    "bkmkstart", "bkmkend", "atnauthor", "atnid", "atnref", "atndate",
    "creatim", "revtim", "printim", "buptim", "doccomm", "operator",
    "hlinkbase", "sp", "sn", "sv", "tc", "tcn",
})

# Control words that end a line.
_RTF_BREAK = frozenset({"par", "line", "sect", "pard", "page", "row"})
# Cell separators: the extractor reads "label | value" rows.
_RTF_CELL = frozenset({"cell", "tab", "nestcell"})

# RTF is a byte stream; its escapes carry bytes in one code page.
_RTF_CODEPAGE = "cp1252"


def _skip_group(text: str, i: int) -> int:
    """Given s[i] == '{', return the index just past the matching '}'."""
    depth = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        elif ch == "\\":
            i += 1  # an escaped brace is content, not structure
        i += 1
    return n


def _rtf_group_starts_skipped(text: str, i: int) -> bool:
    """Given s[i] just past '{', decide whether the whole group is ignored."""
    n = len(text)
    if i < n and text[i] == "\\" and i + 1 < n and text[i + 1] == "*":
        return True        # the \* prefix marks an ignorable destination
    if i < n and text[i] == "\\":
        j = i + 1
        while j < n and text[j].isalpha():
            j += 1
        return text[i + 1:j].lower() in _RTF_SKIP_WORDS
    return False


def _rtf_to_text(raw: bytes) -> str:
    """Extract the readable text of an RTF document."""
    text = raw.decode(_RTF_CODEPAGE, errors="replace")
    # A leading {\rtf1 ...} wrapper is structure, not content.
    out: list[str] = []
    i = 0
    n = len(text)
    skip_unicode = 1        # \ucN: how many fallback characters follow \uN

    while i < n:
        ch = text[i]

        if ch == "{":
            if _rtf_group_starts_skipped(text, i + 1):
                i = _skip_group(text, i)
                continue
            i += 1
            continue

        if ch == "}":
            i += 1
            continue

        if ch == "\\":
            i += 1
            if i >= n:
                break
            symbol = text[i]

            if symbol.isalpha():
                j = i
                while j < n and text[j].isalpha():
                    j += 1
                word = text[i:j].lower()
                param = ""
                if j < n and (text[j] == "-" or text[j].isdigit()):
                    k = j + (1 if text[j] == "-" else 0)
                    while k < n and text[k].isdigit():
                        k += 1
                    param = text[j:k]
                    j = k
                if j < n and text[j] == " ":
                    j += 1          # the space is a delimiter, not content
                i = j

                if word == "u" and param:
                    try:
                        code = int(param)
                    except ValueError:
                        code = 0
                    if code < 0:
                        code += 0x10000
                    out.append(chr(code))
                    fallback = skip_unicode
                    # Drop the ASCII fallback characters Word emits after \uN.
                    while fallback > 0 and i < n and text[i] not in "\\{}":
                        i += 1
                        fallback -= 1
                elif word == "uc" and param:
                    try:
                        skip_unicode = max(0, int(param))
                    except ValueError:
                        skip_unicode = 1
                elif word in _RTF_BREAK:
                    out.append("\n")
                elif word in _RTF_CELL:
                    out.append(" | " if word != "tab" else "\t")
                elif word == "emdash":
                    out.append("-")
                elif word == "endash":
                    out.append("-")
                continue

            if symbol == "'":
                # \'hh - one byte in the document code page.
                hh = text[i + 1:i + 3]
                i += 3
                try:
                    out.append(bytes([int(hh, 16)]).decode(_RTF_CODEPAGE, errors="replace"))
                except ValueError:
                    pass
                continue

            if symbol in "\\{}":
                out.append(symbol)
                i += 1
                continue

            if symbol == "~":
                out.append(" ")
                i += 1
                continue

            if symbol == "*":
                # A bare \* outside a group marker carries nothing.
                i += 1
                continue

            # Any other control symbol (\- \_ \: \|) is a hint, not text.
            i += 1
            continue

        if ch in "\r\n":
            out.append("\n")
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _tidy(text: str) -> str:
    """Collapse the whitespace an RTF stream is full of."""
    lines: list[str] = []
    for raw_line in text.replace("\r", "\n").split("\n"):
        line = re.sub(r"[ \t\u00a0]{2,}", " ", raw_line)
        line = re.sub(r"(\s*\|\s*)+", " | ", line).strip(" |")
        if line:
            lines.append(line)
    # Collapse the blank-heavy output a table produces.
    return "\n".join(lines)[:MAX_TEXT_CHARS]


# ------------------------------------------------------------------------- odf
_ODF_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _element_text(node) -> str:
    """All text under one element, honouring line and space elements."""
    parts: list[str] = []

    def walk(element) -> None:
        tag = _local(element.tag)
        if tag == "s":
            # <text:s text:c="3"/> is a run of spaces.
            count = 1
            for key, value in (element.attrib or {}).items():
                if _local(key) == "c":
                    try:
                        count = max(1, int(value))
                    except (TypeError, ValueError):
                        count = 1
            parts.append(" " * count)
        elif tag == "tab":
            parts.append("\t")
        elif tag in ("line-break", "br"):
            parts.append("\n")
        elif element.text:
            parts.append(element.text)
        for child in element:
            walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(node)
    return "".join(parts)


def _table_rows(root) -> list[str]:
    """Every table row as one ``label | value`` line."""
    rows: list[str] = []
    for row in root.iter(f"{{{_ODF_TABLE}}}table-row"):
        cells: list[str] = []
        for cell in row.iter(f"{{{_ODF_TABLE}}}table-cell"):
            cells.append(" ".join(_element_text(cell).split()))
        # A trailing empty cell is the blank half of a two-column layout, not a
        # missing value, so it is dropped rather than printed as an empty field.
        while cells and not cells[-1]:
            cells.pop()
        if any(cells):
            rows.append(" | ".join(cells))
    return rows


def _paragraphs_outside_tables(root) -> list[str]:
    """Paragraph text that is not already inside a table cell.

    A paragraph in a cell is part of the row above; emitting it again would
    print every value twice, once as ``label | value`` and once bare.
    """
    found: list[str] = []

    def walk(element, inside_cell: bool) -> None:
        tag = _local(element.tag)
        now_inside = inside_cell or tag == "table-cell"
        if tag == "p" and not now_inside:
            value = " ".join(_element_text(element).split())
            if value:
                found.append(value)
        for child in element:
            walk(child, now_inside)

    walk(root, False)
    return found


def _odf_to_text(content: bytes) -> Optional[str]:
    """Read ``content.xml`` of an OpenDocument package."""
    import xml.etree.ElementTree as ET

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            if _ODF_CONTENT not in zf.namelist():
                return None
            info = zf.getinfo(_ODF_CONTENT)
            if info.file_size > MAX_MEMBER_BYTES:
                log.info("ods/odt content.xml is %d bytes, refusing", info.file_size)
                return None
            xml_bytes = zf.read(_ODF_CONTENT)
    except Exception as exc:  # noqa: BLE001 - not a readable package
        log.info("OpenDocument package unreadable: %s", exc)
        return None

    try:
        root = ET.fromstring(xml_bytes)
    except Exception as exc:  # noqa: BLE001 - malformed XML
        log.info("OpenDocument content.xml malformed: %s", exc)
        return None

    # Table rows first: a spreadsheet SI is a two-column table, and the row is
    # the unit that carries the label. Cells are joined with the separator the
    # extractor already reads.
    rows = _table_rows(root)
    paragraphs = _paragraphs_outside_tables(root)

    text = "\n".join(rows + paragraphs)
    return _tidy(text) or None


# ----------------------------------------------------------------------- iwork
def iwork_preview_pdf(content: bytes) -> Optional[bytes]:
    """The QuickLook preview inside an iWork package, when it has one.

    Present for Finder, not for reading: an older version of this reader used it
    because the body looked unreadable, and it stays because a package that
    defeats both the text store and the XML index can still be shown as an
    image.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            if _IWORK_PREVIEW not in zf.namelist():
                return None
            info = zf.getinfo(_IWORK_PREVIEW)
            if info.file_size > MAX_MEMBER_BYTES:
                return None
            return zf.read(_IWORK_PREVIEW)
    except Exception as exc:  # noqa: BLE001 - not a readable package
        log.info("iWork package unreadable: %s", exc)
        return None


# iWork '13 and later keep the real document in a package of Snappy-compressed
# protobuf chunks under Index/. The QuickLook preview only exists for Finder,
# and the XML index only for packages written before iWork '13, so neither can
# stand in for the text itself. Snappy is simple enough to decode directly,
# which keeps this working off macOS and without a new dependency.
_IWORK_IWA_DIR = "Index/"
_IWA_SUFFIX = ".iwa"
_IWA_HEADER_BYTES = 4          # a version byte, then the first chunk's length
_IWA_MAX_MEMBERS = 512
_IWA_MAX_CHUNKS = 4096
_IWA_MIN_LINE = 8

# Text inside a decompressed chunk. The newline belongs in the class because
# iWork stores a whole paragraph, line breaks and all, as one string field, so
# splitting on it is what yields the lines. High bytes belong too: the store is
# UTF-8, so a document that prints "Gross Weight" ahead of a localised unit
# (Gross Weight毛重(KGS)) used to break the run at the first multi-byte
# character, which pushed the label and its value into separate runs and lost
# the field. On the 86 iWork mock documents that cost gross_weight_kg on 20 of
# them, measured 2026-09-21.
_IWA_RUN = re.compile(rb"[\x20-\x7e\x80-\xff\n\r\t]{8,}")

# A protobuf length prefix is one plain byte, so any length in the printable
# range arrives glued to the front of the string it measures: a leading ":" is
# the 58 that counts "Notify Party/Intermediate Consignee: ...". Document lines
# do not begin with punctuation, so the scaffolding is dropped by removing
# leading non-letters.
_IWA_SCAFFOLD = re.compile(r"^[^A-Za-z]+")


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    """Read a protobuf-style base-128 varint, returning (value, next index)."""
    value = 0
    shift = 0
    while i < len(buf):
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift > 35:
            break
    raise ValueError("truncated varint")


def _snappy_block(src: bytes) -> tuple[bytes, int]:
    """Decompress one raw Snappy block, also reporting the bytes it consumed.

    The container header carries each chunk's compressed length, but a Snappy
    block already declares how long its *output* will be. Taking the boundary
    from the block itself means a header we have misread cannot quietly cut the
    text short.
    """
    length, i = _varint(src, 0)
    out = bytearray()
    while len(out) < length:
        tag = src[i]
        i += 1
        kind = tag & 0x03
        if kind == 0:                                   # literal run
            size = tag >> 2
            if size < 60:
                size += 1
            else:
                extra = size - 59
                size = int.from_bytes(src[i:i + extra], "little") + 1
                i += extra
            out += src[i:i + size]
            i += size
        else:                                           # copy from output
            if kind == 1:
                size = ((tag >> 2) & 0x07) + 4
                offset = ((tag >> 5) << 8) | src[i]
                i += 1
            elif kind == 2:
                size = (tag >> 2) + 1
                offset = int.from_bytes(src[i:i + 2], "little")
                i += 2
            else:
                size = (tag >> 2) + 1
                offset = int.from_bytes(src[i:i + 4], "little")
                i += 4
            start = len(out) - offset
            if start < 0:
                raise ValueError("copy offset before the start of the output")
            for k in range(size):
                out.append(out[start + k])
    return bytes(out), i


def _iwa_chunks(member: bytes) -> list[bytes]:
    """The decompressed chunks of one ``.iwa`` member.

    Past the four-byte header a member is a run of Snappy blocks, each of which
    says how far it reaches; a block that will not decode ends the run rather
    than failing the document, because a newer iWork version would otherwise
    take the whole format down with it.
    """
    chunks: list[bytes] = []
    i = _IWA_HEADER_BYTES
    while i < len(member) and len(chunks) < _IWA_MAX_CHUNKS:
        try:
            payload, used = _snappy_block(member[i:])
        except Exception:  # noqa: BLE001 - unreadable chunk ends the run
            break
        if used <= 0:
            break
        chunks.append(payload)
        i += used
    return chunks


def _iwork_iwa_text(content: bytes) -> Optional[str]:
    """Document fields kept in an iWork package's own text store.

    The store also holds stylesheets, locale names and colour names, so runs are
    filtered to the lines the extractor recognises as another field's label:
    without that, a style called "Gross Weight" would read as a value.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            members = [n for n in zf.namelist()
                       if n.startswith(_IWORK_IWA_DIR) and n.endswith(_IWA_SUFFIX)]
            lines: list[str] = []
            seen: set[str] = set()
            for name in members[:_IWA_MAX_MEMBERS]:
                if zf.getinfo(name).file_size > MAX_MEMBER_BYTES:
                    continue
                for chunk in _iwa_chunks(zf.read(name)):
                    for match in _IWA_RUN.finditer(chunk):
                        for line in match.group().decode("utf-8", "replace").splitlines():
                            line = _IWA_SCAFFOLD.sub("", " ".join(line.split()))
                            if len(line) < _IWA_MIN_LINE or line in seen:
                                continue
                            if not extractor.is_label_line(line):
                                continue
                            seen.add(line)
                            lines.append(line)
    except Exception as exc:  # noqa: BLE001 - not a readable package
        log.info("iWork text store unreadable: %s", exc)
        return None
    return "\n".join(lines)[:MAX_TEXT_CHARS] if lines else None


def _iwork_legacy_text(content: bytes) -> Optional[str]:
    """Read the XML index of an iWork '08/'09 package."""
    import xml.etree.ElementTree as ET

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = set(zf.namelist())
            target = next((n for n in (_IWORK_LEGACY_INDEX, "index.xml",
                                       "Index/Document.xml") if n in names), None)
            if target is None:
                return None
            if zf.getinfo(target).file_size > MAX_MEMBER_BYTES:
                return None
            xml_bytes = zf.read(target)
    except Exception as exc:  # noqa: BLE001
        log.info("iWork index unreadable: %s", exc)
        return None

    try:
        root = ET.fromstring(xml_bytes)
    except Exception:  # noqa: BLE001 - not well-formed
        return None

    lines: list[str] = []
    for node in root.iter():
        tag = _local(node.tag)
        value = " ".join((node.text or "").split())
        if not value:
            continue
        if tag == "sf:p":
            lines.append(value)
        elif tag.endswith("-label") or tag in ("sf:label", "key"):
            # A label/value pair: the label element and the value element sit
            # as siblings in iWork's serialisation.
            lines.append(value)
    return _tidy("\n".join(lines)) or None


# --------------------------------------------------------------------- public
def read_office_text(filename: str, content: bytes) -> Optional[tuple[str, str]]:
    """Read a document, returning ``(text, source)`` or None.

    ``source`` names the reader that produced the text, so the origin stays
    visible in the verification result instead of being flattened away.
    """
    fmt = detect_office_format(filename, content)
    if fmt is None:
        return None

    try:
        if fmt == "rtf":
            text = _tidy(_rtf_to_text(content))
            return (text, "rtf") if text else None

        if fmt in ("odt", "ods", "odp", "odg", "ott", "ots", "otp"):
            if not zipfile.is_zipfile(io.BytesIO(content)):
                # A "document" that is not a package at all: RTF bytes under an
                # OpenDocument name is the one mix-up worth handling.
                if bytes(content)[:5] == b"{\\rtf":
                    text = _tidy(_rtf_to_text(content))
                    return (text, "rtf") if text else None
                return None
            text = _odf_to_text(content)
            return (text, f"odf-{fmt}") if text else None

        # iWork: the package's own text store is the authoritative route and the
        # only one that works off macOS. The XML index remains the fallback for
        # packages written before iWork '13.
        text = _iwork_iwa_text(content)
        if text:
            return text, f"iwork-{fmt}"
        text = _iwork_legacy_text(content)
        return (text, f"iwork-{fmt}-index") if text else None
    except Exception as exc:  # noqa: BLE001 - never let a document break ingest
        log.info("office reader failed for %s (%s): %s", filename, fmt, exc)
        return None
