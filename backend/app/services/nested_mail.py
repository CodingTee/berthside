"""Nested email messages as containers (``.eml`` and ``.msg``).

A forwarder routinely forwards the whole message it received from the shipper,
so the SI and the BL arrive *inside* an ``.eml`` attachment rather than beside
it. Without this module that file is one opaque attachment: the doc-type layer
recognises the name, the reader ladder cannot open the container, and the email
escalates as ``unreadable`` even though both documents are sitting right there.

The message is therefore treated exactly like an archive:

* its own body becomes a synthetic ``.frombody.txt`` document, so the cover note
  is visible to the reviewer but can never displace a real attachment;
* its attachments become ordinary members, recursively, which is what makes
  ``forwarded message -> zip -> SI.pdf`` work;
* every member goes through the same :class:`~app.services.archive.MemberSink`
  as a container member: same name rules, same size bounds, same shared budget,
  same safety gate.

``.eml`` is read with the standard library. ``.msg`` is an OLE2 compound file and
is read with ``olefile``: the property streams carry the headers and the body,
and each ``__attach_version1.0_#`` storage holds one attachment.

Nothing here guesses at content: an attachment the reader cannot identify is
still stored under its own name and classified by the normal path.
"""
from __future__ import annotations

import email
import html
import io
import logging
import re
from email import policy
from email.message import Message
from pathlib import Path
from typing import Optional

from app.services.archive import Budget, Expansion, MemberSink, safe_member_name
from app.services import doc_types

log = logging.getLogger(__name__)

EML_SUFFIX = ".eml"
MSG_SUFFIX = ".msg"
MAIL_SUFFIXES = (EML_SUFFIX, MSG_SUFFIX)

# OLE2 compound-file header, the container .msg is always stored in.
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Headers that only a stored message carries. Used *only* to recognise a message
# that lost its extension: a shipping document is full of "Shipper:" and
# "Port of Loading:" lines, so a generic ``Word:`` test would claim every
# extension-less text file and then drop it, because the opener needs a kind.
_MAIL_ONLY_HEADERS = re.compile(
    rb"^(?:From|Return-Path|Message-ID|Received|MIME-Version|Content-Type|"
    rb"Delivered-To|DKIM-Signature|Authentication-Results|X-Original-To|"
    rb"X-Mailer|Reply-To|In-Reply-To|References|Sender)[ \t]*:",
    re.MULTILINE | re.IGNORECASE)

# How many headers we print ahead of the body, and how far into a body we look
# for the labels. The body of a forwarding note is short; a pasted 200 KB thread
# only adds noise the reviewer has to scroll past.
_BODY_CHAR_LIMIT = 4000

_MAIL_HEADERS = (
    ("Email From", "From"),
    ("Email To", "To"),
    ("Email Cc", "Cc"),
    ("Email Subject", "Subject"),
    ("Email Date", "Date"),
)


# --------------------------------------------------------------------- eml
_HTML_BLOCK = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_BREAK = re.compile(r"</?(?:br|p|div|tr|li|h[1-6])\b[^>]*>", re.IGNORECASE)
_HTML_CELL = re.compile(r"</t[dh]>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")


def _html_to_text(raw: str) -> str:
    """Flatten an HTML mail body to lines the extractor can read.

    Table cells become ``label | value``, which is the same shape the extractor
    already reads in a spreadsheet export. Without it, an HTML SI copied into a
    mail body arrives as one long run of words.
    """
    text = _HTML_BLOCK.sub(" ", raw or "")
    text = _HTML_CELL.sub(" | ", text)
    text = _HTML_BREAK.sub("\n", text)
    text = _HTML_TAG.sub(" ", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t\u00a0]{2,}", " ", line).strip(" |") for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _part_text(part: Message) -> str:
    """Decoded text of a leaf part, whatever its transfer encoding."""
    try:
        content = part.get_content()
    except Exception:  # noqa: BLE001 - a part with a charset we cannot decode
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    if (part.get_content_subtype() or "").lower() == "html":
        return _html_to_text(content)
    return content or ""


def _read_eml(content: bytes) -> tuple[list[str], list[tuple[str, bytes]], list[str]]:
    """Return ``(body_lines, attachments, notes)`` for one ``.eml`` payload."""
    msg = email.message_from_bytes(content, policy=policy.default)
    lines: list[str] = []
    for label, header in _MAIL_HEADERS:
        value = msg.get(header)
        if value:
            lines.append(f"{label} | {' '.join(str(value).split())}")

    body: list[str] = []
    attachments: list[tuple[str, bytes]] = []

    for part in msg.walk():
        if part.is_multipart():
            # A forwarded message is itself a multipart part, so this skip is
            # what hoists an inner message's own attachments and body into this
            # message: the reviewer sees one envelope instead of a boundary they
            # have to open by hand.
            continue
        filename = part.get_filename()
        if filename:
            # Attachments arrive either as an explicit attachment or as an
            # inline part carrying a name (a signature logo, an embedded image).
            # Both are files, and both go through the same bounds and gate.
            payload = part.get_payload(decode=True) or b""
            if payload:
                attachments.append((filename, payload))
            continue
        text = _part_text(part)
        if text.strip():
            body.append(text.strip())

    return lines, attachments, body


# --------------------------------------------------------------------- msg
# Property ids used below, as they appear in the stream name
# (``__substg1.0_<property><type>``). Names are the MAPI names.
_MSG_PROPERTIES = (
    ("0037", "Email Subject"),   # PidTagSubject
    ("0c1f", "Email From"),      # PidTagSenderName
    ("5d01", "Email From Address"),  # PidTagSenderSmtpAddress
    ("0e04", "Email To"),        # PidTagDisplayTo
    ("0039", "Email Date"),      # PidTagClientSubmitTime
)
_MSG_BODY_ID = "1000"            # PidTagBody
_MSG_ATTACH_ROOT = "__attach_version1.0_#"
_MSG_ATTACH_DATA = "3701"        # PidTagAttachDataBinary
_MSG_ATTACH_DATA_OBJECT = "3701000D"  # PidTagAttachDataObject (by reference)
_MSG_ATTACH_NAME = ("3707", "3704")   # PidTagAttachLongFilename / AttachFilename
_STREAM_PREFIX = "__substg1.0_"

# MAPI property types, in the order we prefer them.
_UNICODE, _ANSI, _BINARY = "001f", "001e", "0102"


def _decode_property(raw: bytes, ptype: str) -> str:
    if ptype == _ANSI:
        return raw.decode("cp1252", errors="replace")
    if ptype == _BINARY:
        return raw.decode("utf-8", errors="replace")
    return raw.decode("utf-16-le", errors="replace")


def _msg_streams_by_path(ole) -> dict[tuple[str, ...], str]:
    """{entry path: stream name} for every ``__substg1.0_`` stream."""
    out: dict[tuple[str, ...], str] = {}
    try:
        entries = ole.listdir(streams=True, storages=False)
    except Exception:  # noqa: BLE001 - malformed directory
        return out
    for entry in entries:
        if entry and entry[-1].startswith(_STREAM_PREFIX):
            out[tuple(entry)] = entry[-1]
    return out


def _find_stream(streams: dict[tuple[str, ...], str],
                 path: tuple[str, ...]) -> Optional[tuple[str, ...]]:
    """The real entry matching ``path``, compared without regard to case.

    A stream is named ``__substg1.0_<property><type>`` in hex, and writers
    disagree on the case of those digits: Outlook emits upper case
    (``__substg1.0_0037001F``), the OLE writer in the test suite lower case.
    Matching exactly meant every property of a real Outlook message was missed
    while the fixture's own file read perfectly.
    """
    wanted = tuple(part.lower() for part in path)
    for real in streams:
        if tuple(part.lower() for part in real) == wanted:
            return real
    return None


def _property_text(ole, streams: dict, prop: str, scope: tuple[str, ...] = ()) -> Optional[str]:
    """Read one MAPI property, preferring its unicode stream."""
    for ptype in (_UNICODE, _ANSI, _BINARY):
        path = _find_stream(streams, tuple(scope) + (f"{_STREAM_PREFIX}{prop}{ptype}",))
        if path is None:
            continue
        try:
            return _decode_property(ole.openstream(list(path)).read(), ptype)
        except Exception as exc:  # noqa: BLE001 - unreadable property
            log.info("msg property %s unreadable: %s", prop, exc)
    return None


def _read_msg(content: bytes) -> tuple[list[str], list[tuple[str, bytes]], list[str]]:
    """Return ``(body_lines, attachments, notes)`` for one ``.msg`` payload."""
    import olefile

    notes: list[str] = []
    lines: list[str] = []
    attachments: list[tuple[str, bytes]] = []

    with olefile.OleFileIO(io.BytesIO(content)) as ole:
        streams = _msg_streams_by_path(ole)
        for prop, label in _MSG_PROPERTIES:
            value = _property_text(ole, streams, prop)
            if value:
                lines.append(f"{label} | {' '.join(value.split())}")

        body = _property_text(ole, streams, _MSG_BODY_ID)
        body_lines = [ln for ln in (body or "").splitlines() if ln.strip()]

        # Every attachment lives in its own storage, so group the streams by the
        # first path component and read each group as one attachment.
        roots: list[tuple[str, ...]] = []
        for path in streams:
            if path and path[0].lower().startswith(_MSG_ATTACH_ROOT):
                if (path[0],) not in roots:
                    roots.append((path[0],))
        data_names = {
            f"{_STREAM_PREFIX}{_MSG_ATTACH_DATA}{_BINARY}",
            f"{_STREAM_PREFIX}{_MSG_ATTACH_DATA_OBJECT}",
            f"{_STREAM_PREFIX}{_MSG_ATTACH_DATA_OBJECT}{_BINARY}",
        }
        for (root,) in roots:
            name: Optional[str] = None
            for prop in _MSG_ATTACH_NAME:
                name = _property_text(ole, streams, prop, scope=(root,))
                if name:
                    break
            if not name:
                # An embedded message has no filename of its own.
                notes.append(f"{root}: attachment has no filename and was skipped")
                continue
            data_path = None
            for path in streams:
                if path[:1] != (root,):
                    continue
                if path[-1].lower() in data_names:
                    data_path = path
                    break
            if data_path is None:
                notes.append(f"{name}: attachment body not found in the message")
                continue
            try:
                size = ole.get_size(list(data_path))
                if size > 64 * 1024 * 1024:
                    notes.append(f"{name}: attachment is {size // (1024 * 1024)}MB, skipped")
                    continue
                payload = ole.openstream(list(data_path)).read()
            except Exception as exc:  # noqa: BLE001 - unreadable stream
                notes.append(f"{name}: could not be read ({type(exc).__name__})")
                continue
            attachments.append((name, payload))

    return lines, attachments, body_lines


# -------------------------------------------------------------------- public
def detect_mail(filename: str, content: Optional[bytes] = None) -> Optional[str]:
    """``"eml"``, ``"msg"`` or None.

    The extension decides, because the magic bytes cannot: ``.msg`` is an OLE2
    container, the same header a legacy ``.doc`` or ``.xls`` carries. Content is
    only used to confirm a ``.msg`` really is one, so a text file renamed ``.msg``
    is refused by the security gate rather than decoded as a message.
    """
    name = str(filename or "").lower()
    if name.endswith(MSG_SUFFIX):
        return "msg" if bytes(content or b"").startswith(OLE_MAGIC) else None
    if name.endswith(EML_SUFFIX):
        return "eml"
    return None


def _resolve_kind(filename: str, content: bytes) -> Optional[str]:
    """The message kind for these bytes, or None when they are not a message.

    Both :func:`is_nested_mail` and :func:`expand_mail` go through here so they
    can never disagree. When they could, a file announced as a message was then
    refused by the opener, and because a carrier that yields nothing is not an
    error, the document disappeared with only a log line to show for it.
    """
    kind = detect_mail(filename, content)
    if kind is None and not Path(str(filename or "")).suffix \
            and _MAIL_ONLY_HEADERS.search(bytes(content or b"")[:4096]):
        # No usable name left, but the headers are unambiguous.
        return "eml"
    return kind


def is_nested_mail(filename: str, content: Optional[bytes] = None) -> bool:
    """True when this is a message container the reader can open."""
    return _resolve_kind(filename, bytes(content or b"")) is not None


def body_document_name(filename: str) -> str:
    """Name for the message body once it becomes a document.

    Carries the synthetic marker, so the doc-type layer treats it as a fallback:
    it can fill a genuine gap but never displaces a real attachment.
    """
    stem = Path(str(filename or "")).name
    for suffix in MAIL_SUFFIXES:
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return f"{stem}{doc_types.SYNTHETIC_SUFFIX}.txt"


def expand_mail(filename: str, content: bytes,
                budget: Optional[Budget] = None) -> Expansion:
    """Expand a nested message into its body document and its attachments."""
    budget = budget if budget is not None else Budget()
    sink = MemberSink(filename, budget)
    kind = _resolve_kind(filename, content)
    if kind is None:
        sink.expansion.notes.append(f"{filename}: not a readable message")
        return sink.expansion

    try:
        if kind == "eml":
            header_lines, attachments, body_lines = _read_eml(content)
        else:
            header_lines, attachments, body_lines = _read_msg(content)
    except Exception as exc:  # noqa: BLE001 - never let a message break ingest
        log.info("message expansion failed for %s: %s", filename, exc)
        sink.expansion.notes.append(
            f"{filename}: could not be opened as a {kind} message "
            f"({type(exc).__name__})")
        return sink.expansion

    # The body document: headers first (they carry the sender), then the text.
    body = "\n".join(header_lines + body_lines).strip()
    if body:
        body = body[:_BODY_CHAR_LIMIT]
        body_name = body_document_name(filename)
        payload = body.encode("utf-8")
        sink.take(body_name, len(payload), lambda payload=payload: payload)
    else:
        sink.expansion.notes.append(f"{filename}: message carries no body text")

    for raw_name, data in attachments:
        base = safe_member_name(raw_name)
        if base is None:
            # Attachment names come from the message, so a path-like one is an
            # attempt to escape, not a document.
            sink.skip(raw_name)
            continue
        sink.take(base, len(data), lambda data=data: data)

    if not attachments:
        sink.expansion.notes.append(
            f"{filename}: the message has no attachments, only its body")
    return sink.expansion
