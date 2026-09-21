"""Safe container expansion for shipping-document attachments.

A forwarder frequently sends one ``shipment_documents.zip`` holding the SI, the
BL and the invoice. The verification engine works on individual documents, so
the container has to be expanded *before* classification: the members become
ordinary attachments and every downstream stage (doc-type detection, pairing,
versioning) keeps working unchanged.

Supported containers: ZIP, 7z, RAR, tar (including ``.tar.gz`` / ``.tgz``) and a
bare gzip stream. The format is decided by the file's magic bytes first and by
the suffix second, so a container that was renamed still expands and a file that
only *claims* a container extension is read by the right reader (or reported
honestly, rather than being handed to the wrong one).

The expansion is deliberately defensive:

* member count, single-member size and total unpacked size are capped. One
  ``ExpansionBudget`` is passed down through every nesting level, so a payload
  built out of many small archives cannot multiply the cap by its nesting depth;
* path traversal and absolute paths are rejected, and members are reduced to
  their base name, so nothing is written outside the ingest directory;
* a triple extension (``invoice.pdf.exe``) inside a container is caught by the
  same ``verify_file_safety`` gate a direct upload passes. That used to be a log
  line only, which made "wrap the payload in a ZIP" a way to reach the pipeline
  with an executable the gate would otherwise refuse;
* nesting is allowed but bounded (``MAX_LEVELS``), and every level still spends
  from the same budget.

A container that cannot be expanded at all (corrupt, encrypted, or a RAR with no
extractor available) is a *note*, not a security incident: the honest outcome is
that the email carries no readable documents and escalates for a human. Only
content that fails the safety gate raises an alert.

Nothing here guesses at content: a member that cannot be read is reported as
unreadable by the normal extraction path.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from app.services.security import MAX_ATTACHMENT_SIZE, verify_file_safety

log = logging.getLogger(__name__)

# Bounds for container expansion. They apply to the whole payload, not to each
# container separately: see ExpansionBudget.
MAX_MEMBERS = 25
MAX_MEMBER_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = MAX_ATTACHMENT_SIZE  # 30 MB, same cap as a single attachment

# How many container layers may be opened inside one payload
# (payload.zip -> inner.zip -> document is depth 2).
MAX_LEVELS = 2

ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar", ".tar", ".tgz", ".gz")

# Suffix -> container, used only when the magic bytes are inconclusive (a
# truncated or nonstandard container). ``.tgz`` and ``.tar.gz`` are gzip first,
# and the decompressed stream is then recognised as a tar by its own magic.
_SUFFIX_FORMAT = {
    ".zip": "zip",
    ".7z": "7z",
    ".rar": "rar",
    ".tar": "tar",
    ".tgz": "gzip",
    ".gz": "gzip",
}

# Longest first: "Rar!\x1a\x07\x01\x00" (RAR5) must be tested before the RAR4
# marker it starts with.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"Rar!\x1a\x07\x01\x00", "rar"),
    (b"Rar!\x1a\x07\x00", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),
    (b"PK\x07\x08", "zip"),
    (b"\x1f\x8b", "gzip"),
)

# Suffixes that are a ZIP or an OLE2 file *on the inside* but a document on the
# outside. They must never be expanded as containers: doing so replaces the
# document with its own internal parts, so an .odt became "mimetype" plus
# "manifest.xml" and the Shipping Instruction it carried was never read. The
# document readers own these; the container layer has to stay out of the way.
PACKAGE_SUFFIXES = frozenset({
    # Office OpenXML
    ".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm",
    # OpenDocument
    ".odt", ".ods", ".odp", ".ott", ".ots", ".otp", ".odg", ".fodt", ".fods",
    # iWork (a zip carrying a QuickLook preview)
    ".pages", ".numbers", ".key",
    # OLE2 compound files that are documents, not archives
    ".doc", ".xls", ".ppt", ".msg",
    # An e-book is a document too
    ".epub",
})


def _is_package_document(filename: str) -> bool:
    """True for a format whose own reader must see the whole file.

    A container and a package can share the same magic bytes, so the suffix is
    the only thing that separates them. The list is explicit rather than a rule
    ("anything Office-like") on purpose: an unknown suffix is still treated as a
    container, because a renamed ZIP has to keep expanding.
    """
    name = str(filename or "").lower()
    return any(name.endswith(suffix) for suffix in PACKAGE_SUFFIXES)

_TAR_MAGIC_OFFSET = 257
_TAR_MAGIC = b"ustar"


@dataclass
class Budget:
    """What one payload may still spend on expansion, across every level.

    A fresh instance is the top-level allowance; the caller threads the same
    object through nested containers so the caps are per *email*, not per
    container.
    """

    members_left: int = MAX_MEMBERS
    bytes_left: int = MAX_TOTAL_BYTES


@dataclass
class Expansion:
    """The outcome of expanding one container.

    ``notes`` explain what was skipped and why (bounds, unreadable, no tool).
    ``blocked`` names members that failed the safety gate; those are security
    findings and the caller raises them as alerts.
    """

    members: list[tuple[str, bytes]]
    notes: list[str]
    blocked: list[str]


def _looks_like_tar(content: bytes) -> bool:
    return len(content) > _TAR_MAGIC_OFFSET + 5 and \
        content[_TAR_MAGIC_OFFSET:_TAR_MAGIC_OFFSET + 5] == _TAR_MAGIC


# Bytes that carry text rather than data, for naming a nameless member.
_TEXT_BYTES = frozenset(b"\t\n\r\f\v") | frozenset(range(0x20, 0x7F))


def _looks_textual(data: bytes) -> bool:
    """True when these bytes are text, not binary.

    Deliberately conservative: NUL bytes and a high proportion of control
    characters mean binary. Getting this wrong in the permissive direction would
    label a binary member as ``.txt`` and hand it to a text parser.
    """
    sample = bytes(data or b"")[:4096]
    if not sample or b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        pass
    # Not UTF-8. Could still be single-byte text, so judge by the content.
    printable = sum(1 for byte in sample if byte in _TEXT_BYTES or byte >= 0xA0)
    return printable / len(sample) >= 0.97


def _gzip_inner_name(content: bytes) -> Optional[str]:
    """The original filename stored in a gzip header, when it has one.

    The name only survives in the header (flag bit 3); a stream written by
    ``gzip -c`` or by a library that does not set it has none, which is why the
    caller also has the container's own name to fall back on.
    """
    if len(content) < 10 or not (content[3] & 0x08):
        return None
    end = content.find(b"\x00", 10)
    if end < 0:
        return None
    return content[10:end].decode("latin-1", errors="replace") or None


def detect_container(filename: str, content: Optional[bytes] = None) -> Optional[str]:
    """Name the container format, or None when this is not a container.

    Magic bytes win over the extension, because they describe the file while the
    name only describes what someone hoped it was. The extension is the fallback
    so a damaged container still reaches its own reader, which reports a precise
    reason instead of "unknown file".

    A document that merely happens to *be* a ZIP, an OLE2 file or a gzip stream
    is not a container here; see :data:`PACKAGE_SUFFIXES`.
    """
    if _is_package_document(filename):
        return None
    head = bytes(content or b"")[:16]
    for magic, fmt in _MAGIC:
        if head.startswith(magic):
            return fmt
    if content is not None and _looks_like_tar(content):
        return "tar"
    name = str(filename or "").lower()
    for suffix, fmt in _SUFFIX_FORMAT.items():
        if name.endswith(suffix):
            return fmt
    return None


def is_archive(filename: str, content: Optional[bytes] = None) -> bool:
    """True when this is a container the reader can expand.

    ``content`` is optional so the name alone can be tested (that is what the
    doc-type layer needs), but callers that hold the bytes should pass them: a
    renamed archive is still an archive.
    """
    return detect_container(filename, content) is not None


def safe_member_name(raw_name: str) -> Optional[str]:
    """Normalise a container entry name, rejecting traversal and directories."""
    name = (raw_name or "").replace("\\", "/")
    if not name or name.endswith("/"):
        return None  # directory entry
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("..", "") for part in path.parts):
        return None
    base = path.name
    if not base or base.startswith("."):
        return None  # hidden / dotfile (e.g. __MACOSX resource forks)
    return base


def _is_escape_attempt(raw_name: str) -> bool:
    """True when an entry name tries to leave the archive root.

    Directory entries and ``__MACOSX`` resource forks are dropped silently:
    they are junk, not an incident. A name that walks upwards or points at an
    absolute path is worth recording.
    """
    name = (raw_name or "").replace("\\", "/")
    if not name:
        return False
    path = PurePosixPath(name)
    return path.is_absolute() or ".." in path.parts


class MemberSink:
    """Applies the shared bounds and the safety gate to every member."""

    def __init__(self, filename: str, budget: Budget):
        self.filename = filename
        self.budget = budget
        self.expansion = Expansion(members=[], notes=[], blocked=[])
        self._seen: set[str] = set()

    def skip(self, raw_name: str) -> None:
        """Record an entry that was dropped before it could be judged."""
        if _is_escape_attempt(raw_name):
            self.expansion.notes.append(
                f"{self.filename}: '{raw_name}' points outside the archive "
                f"and was not extracted")

    def take(self, base: str, size: int, read) -> None:
        """Accept one member. ``read`` returns its bytes, called only if wanted."""
        if self.budget.members_left <= 0:
            self.expansion.notes.append(
                f"{self.filename}: member limit reached "
                f"({MAX_MEMBERS} per email), remaining members skipped")
            return
        if base in self._seen:
            self.expansion.notes.append(f"{base}: duplicate name inside {self.filename}")
            return
        if size > MAX_MEMBER_BYTES:
            self.expansion.notes.append(
                f"{base}: member is {size / (1024 * 1024):.1f}MB, "
                f"above the {MAX_MEMBER_BYTES // (1024 * 1024)}MB member limit")
            return
        if size > self.budget.bytes_left:
            self.expansion.notes.append(
                f"{self.filename}: unpacked size exceeds the "
                f"{MAX_TOTAL_BYTES // (1024 * 1024)}MB limit, remaining members skipped")
            return
        try:
            data = read()
        except Exception as exc:  # noqa: BLE001 - corrupt / encrypted member
            self.expansion.notes.append(f"{base}: could not be read ({type(exc).__name__})")
            return
        if len(data) > MAX_MEMBER_BYTES:
            # The declared size lied. Trust the bytes we actually got.
            self.expansion.notes.append(
                f"{base}: unpacked to {len(data) / (1024 * 1024):.1f}MB, "
                f"above the {MAX_MEMBER_BYTES // (1024 * 1024)}MB member limit")
            return
        # A container is just a carrier: the member must still pass the same
        # safety gate an uploaded file would.
        safe, reason = verify_file_safety(base, data)
        if not safe:
            self.expansion.blocked.append(f"{base} (inside {self.filename}): {reason}")
            return
        self._seen.add(base)
        self.budget.members_left -= 1
        self.budget.bytes_left -= len(data)
        self.expansion.members.append((base, data))


def _zip_members(collector: MemberSink, content: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            base = safe_member_name(info.filename)
            if base is None:
                collector.skip(info.filename)
                continue

            def read(info=info):
                # Bounded read: a member whose declared size lies cannot make
                # this allocate more than the member cap.
                with zf.open(info) as handle:
                    return handle.read(MAX_MEMBER_BYTES + 1)

            collector.take(base, info.file_size, read)


def _tar_members(collector: MemberSink, content: bytes) -> None:
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as tf:
        for info in tf:
            if not info.isfile():
                continue
            # A symlink member is a path escape attempt, not a document.
            if info.issym() or info.islnk():
                collector.expansion.notes.append(
                    f"{info.name}: links inside an archive are not followed")
                continue
            base = safe_member_name(info.name)
            if base is None:
                collector.skip(info.name)
                continue

            def read(info=info):
                handle = tf.extractfile(info)
                return b"" if handle is None else handle.read(MAX_MEMBER_BYTES + 1)

            collector.take(base, info.size, read)


def _gunzip(content: bytes, cap: int) -> bytes:
    """Decompress a gzip stream, refusing to materialise more than `cap`."""
    engine = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    for start in range(0, len(content), 65536):
        chunk = content[start:start + 65536]
        out += engine.decompress(chunk, cap - len(out) + 1)
        if len(out) > cap:
            raise ValueError("decompressed stream exceeds the size cap")
    out += engine.flush()
    if len(out) > cap:
        raise ValueError("decompressed stream exceeds the size cap")
    return bytes(out)


def _gzip_members(collector: MemberSink, filename: str, content: bytes) -> None:
    # Unpack a little over the member cap so the "too big" note can quantify it.
    raw = _gunzip(content, MAX_MEMBER_BYTES + 1)
    if _looks_like_tar(raw):
        # ".tar.gz" / ".tgz" is a tar stream that happens to be compressed.
        _tar_members(collector, raw)
        return
    stem = Path(str(filename or "")).name
    for suffix in (".gz", ".tgz"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    # The header name is what the file was called before it was compressed; the
    # container's own name is the fallback when the writer did not store one.
    member = safe_member_name(_gzip_inner_name(content) or "") or stem
    if not member:
        collector.expansion.notes.append(
            f"{filename}: gzip stream holds no name to expand into")
        return
    if not Path(member).suffix and _looks_textual(raw):
        # A bare stream has no extension to carry the document type, and the
        # doc-type layer reads the name. Naming it for what it demonstrably is
        # keeps the member comparable instead of merely readable.
        member = f"{member}.txt"
    collector.take(member, len(raw), lambda: raw)


def _sevenzip_members(collector: MemberSink, content: bytes) -> None:
    import py7zr

    with py7zr.SevenZipFile(io.BytesIO(content), mode="r") as archive:
        wanted: list[tuple[str, int]] = []
        for info in archive.list():
            if info.is_directory:
                continue
            base = safe_member_name(info.filename)
            if base is None:
                collector.skip(info.filename)
                continue
            if info.uncompressed > MAX_MEMBER_BYTES or \
                    info.uncompressed > collector.budget.bytes_left:
                collector.take(base, info.uncompressed, lambda: b"")
                continue
            wanted.append((base, info.uncompressed))
        if not wanted:
            return
        # py7zr has no bounded single-member stream. Extraction to a private
        # directory is the only route, and the sizes above bound what can land
        # there before anything is written.
        with tempfile.TemporaryDirectory(prefix="sdoc-7z-") as tmp:
            archive.extract(path=tmp, targets=[name for name, _ in wanted])
            for base, size in wanted:
                produced = Path(tmp) / base
                if not produced.is_file():
                    collector.expansion.notes.append(
                        f"{base}: not produced by the 7z extractor")
                    continue
                collector.take(base, size, produced.read_bytes)


# --------------------------------------------------------------- rar backend
_RAR_READY: Optional[bool] = None


def _bsdtar_path() -> Optional[str]:
    """Locate a RAR-capable external extractor.

    ``rarfile`` reads the headers by itself but delegates the decompression, so a
    tool is required for anything stored with a real method. Windows ships
    libarchive as ``System32\\tar.exe`` (bsdtar), which reads RAR; ``unrar`` or
    ``7z`` are used when they are on PATH.
    """
    for name in ("unrar", "bsdtar", "7z", "7zz"):
        found = shutil.which(name)
        if found:
            return found
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    for candidate in (Path(system_root) / "System32" / "tar.exe",
                      Path(system_root) / "Sysnative" / "tar.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def _rar_available() -> bool:
    """True when rarfile can find something to extract with (probed once)."""
    global _RAR_READY
    if _RAR_READY is not None:
        return _RAR_READY
    try:
        import rarfile
    except Exception:  # noqa: BLE001 - optional dependency
        _RAR_READY = False
        return False

    tool = _bsdtar_path()
    if tool:
        rarfile.BSDTAR_TOOL = tool
        rarfile.UNRAR_TOOL = tool
        rarfile.SEVENZIP_TOOL = tool
    try:
        rarfile.tool_setup(unrar=True, bsdtar=True, unar=True,
                           sevenzip=True, sevenzip2=True)
        _RAR_READY = True
    except Exception as exc:  # noqa: BLE001 - no working extractor
        log.info("RAR extraction unavailable: %s", exc)
        _RAR_READY = False
    return _RAR_READY


def _rar_members(collector: MemberSink, filename: str, content: bytes) -> None:
    """Extract the members of a RAR archive.

    ``rarfile`` parses the headers by itself but delegates the decompression, so
    it needs an external tool either way. Its bsdtar backend fails on RAR5
    members ("Failed the read enough data: req=622 got=53") on archives that
    bsdtar unpacks cleanly, so when bsdtar is available the bytes are taken from
    bsdtar directly. rarfile remains the route when bsdtar is not present,
    because it can still drive unrar or 7z.
    """
    tool = _bsdtar_path()
    if tool:
        _rar_via_bsdtar(collector, filename, content, tool)
        return
    _rar_via_rarfile(collector, filename, content)


def _rar_via_bsdtar(collector: MemberSink, filename: str, content: bytes,
                    tool: str) -> None:
    """Unpack with libarchive, then read back only what landed where it should.

    The extraction directory is the boundary. Every file is resolved and checked
    against it before being read, so an entry that escaped (an absolute path, a
    ``..`` segment, or a symlink pointing out) is reported instead of imported.
    """
    with tempfile.TemporaryDirectory(prefix="sdoc-rar-") as tmp:
        root = Path(tmp)
        archive_path = root / "payload.rar"
        archive_path.write_bytes(content)
        dest = root / "out"
        dest.mkdir()
        try:
            done = subprocess.run(
                [tool, "-xf", str(archive_path), "-C", str(dest)],
                capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
            collector.expansion.notes.append(
                f"{filename}: the RAR extractor could not be run "
                f"({type(exc).__name__})")
            return
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace").strip()[:160]
            collector.expansion.notes.append(
                f"{filename}: the RAR extractor failed with exit "
                f"{done.returncode}{': ' + detail if detail else ''}")
            return

        boundary = dest.resolve()
        for produced in sorted(dest.rglob("*")):
            if not produced.is_file():
                continue
            relative = produced.relative_to(dest).as_posix()
            try:
                produced.resolve().relative_to(boundary)
            except ValueError:
                collector.expansion.notes.append(
                    f"{filename}: '{relative}' left the extraction directory "
                    f"and was not read")
                continue
            member = safe_member_name(relative)
            if member is None:
                collector.skip(relative)
                continue
            collector.take(member, produced.stat().st_size, produced.read_bytes)


def _rar_via_rarfile(collector: MemberSink, filename: str, content: bytes) -> None:
    import rarfile

    if not _rar_available():
        collector.expansion.notes.append(
            f"{filename}: RAR needs an external extractor "
            f"(unrar, 7z or bsdtar); none was found, so it was not expanded")
        return

    # rarfile hands the archive to the external tool by path, so the bytes go
    # through a private temporary file (bounded by the attachment cap).
    with tempfile.TemporaryDirectory(prefix="sdoc-rar-") as tmp:
        archive_path = Path(tmp) / "payload.rar"
        archive_path.write_bytes(content)
        with rarfile.RarFile(str(archive_path)) as rf:
            for info in rf.infolist():
                if info.isdir():
                    continue
                base = safe_member_name(info.filename)
                if base is None:
                    collector.skip(info.filename)
                    continue
                if info.file_size > MAX_MEMBER_BYTES or \
                        info.file_size > collector.budget.bytes_left:
                    collector.take(base, info.file_size, lambda: b"")
                    continue
                out_dir = Path(tmp) / "out"
                out_dir.mkdir(exist_ok=True)
                try:
                    rf.extract(info, path=str(out_dir))
                    produced = out_dir / base
                    data = produced.read_bytes() if produced.is_file() else b""
                except Exception as exc:  # noqa: BLE001 - encrypted / bad tool
                    collector.expansion.notes.append(
                        f"{base}: could not be extracted from the RAR "
                        f"({type(exc).__name__})")
                    continue
                collector.take(base, len(data), lambda data=data: data)


_EXPANDERS = {
    "zip": lambda c, fn, content: _zip_members(c, content),
    "tar": lambda c, fn, content: _tar_members(c, content),
    "gzip": lambda c, fn, content: _gzip_members(c, fn, content),
    "7z": lambda c, fn, content: _sevenzip_members(c, content),
    "rar": lambda c, fn, content: _rar_members(c, fn, content),
}


def expand_archive(filename: str, content: bytes,
                   budget: Optional[Budget] = None) -> Expansion:
    """Expand one container into its members.

    Returns an :class:`Expansion`: the accepted members, notes explaining what
    was skipped, and any member the safety gate blocked. The caller logs the
    notes and raises the blocked ones as alerts, so a dropped document is never
    silent and a malicious one is not buried in a log file.
    """
    budget = budget if budget is not None else Budget()
    fmt = detect_container(filename, content)
    if fmt is None:
        return Expansion([], [f"{filename}: not a readable archive"], [])
    if not content:
        return Expansion([], [f"{filename}: empty archive"], [])

    collector = MemberSink(filename, budget)
    try:
        _EXPANDERS[fmt](collector, filename, content)
    except Exception as exc:  # noqa: BLE001 - never let an archive break ingest
        log.info("expansion failed for %s (%s): %s", filename, fmt, exc)
        collector.expansion.notes.append(
            f"{filename}: could not be expanded as {fmt} ({type(exc).__name__})")
    return collector.expansion
