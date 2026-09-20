"""Safe archive (ZIP) expansion for shipping-document attachments.

A forwarder frequently sends one ``shipment_documents.zip`` holding the SI, the
BL and the invoice. The verification engine works on individual documents, so
the archive has to be expanded *before* classification: the members become
ordinary attachments and every downstream stage (doc-type detection, pairing,
versioning) keeps working unchanged.

The expansion is deliberately defensive:

* member count, single-member size and total unpacked size are capped (a zip
  bomb must cost bounded memory);
* path traversal and absolute paths are rejected;
* nested archives are not unpacked (one level only);
* every member goes through the same ``verify_file_safety`` gate as a normal
  upload, so a blocked member becomes a visible note instead of a stored file.

Nothing here guesses at content: a member that cannot be read is reported as
unreadable by the normal extraction path.
"""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import PurePosixPath
from typing import Optional

from app.services.security import MAX_ATTACHMENT_SIZE, verify_file_safety

log = logging.getLogger(__name__)

# Bounds for archive expansion.
MAX_MEMBERS = 25
MAX_MEMBER_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = MAX_ATTACHMENT_SIZE  # 30 MB, same cap as a single attachment

ARCHIVE_SUFFIXES = (".zip",)
_NESTED_ARCHIVE_SUFFIXES = ARCHIVE_SUFFIXES + (".7z", ".rar", ".gz", ".tar")


def is_archive(filename: str) -> bool:
    """True when the name marks a ZIP container the reader can expand."""
    return str(filename or "").lower().endswith(ARCHIVE_SUFFIXES)


def _safe_member_name(raw_name: str) -> Optional[str]:
    """Normalise an archive entry name, rejecting traversal and nesting."""
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


def expand_archive(filename: str, content: bytes) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Expand a ZIP into ``(member_name, member_bytes)`` pairs.

    Returns ``(members, notes)``. ``notes`` carries human-readable reasons for
    anything that was skipped or blocked, so the caller can surface them rather
    than silently dropping documents.
    """
    notes: list[str] = []
    if not zipfile.is_zipfile(io.BytesIO(content or b"")):
        return [], [f"{filename}: not a readable ZIP archive"]

    members: list[tuple[str, bytes]] = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > MAX_MEMBERS:
                notes.append(
                    f"{filename}: archive holds {len(infos)} files, "
                    f"only the first {MAX_MEMBERS} were expanded")
                infos = infos[:MAX_MEMBERS]
            for info in infos:
                base = _safe_member_name(info.filename)
                if base is None:
                    continue
                if base.lower().endswith(_NESTED_ARCHIVE_SUFFIXES):
                    notes.append(f"{base}: nested archives are not expanded")
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    notes.append(
                        f"{base}: member is {info.file_size / (1024 * 1024):.1f}MB, "
                        f"above the {MAX_MEMBER_BYTES // (1024 * 1024)}MB member limit")
                    continue
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    notes.append(
                        f"{filename}: unpacked size exceeds the "
                        f"{MAX_TOTAL_BYTES // (1024 * 1024)}MB limit, remaining members skipped")
                    break
                try:
                    data = zf.read(info)
                except Exception as exc:  # noqa: BLE001 - corrupt/ encrypted member
                    notes.append(f"{base}: could not be read ({type(exc).__name__})")
                    continue
                # A zip is just a container: the member must still pass the same
                # safety gate an uploaded file would.
                safe, reason = verify_file_safety(base, data)
                if not safe:
                    notes.append(f"{base}: {reason}")
                    continue
                members.append((base, data))
    except zipfile.BadZipFile as exc:
        return [], [f"{filename}: corrupt ZIP ({exc})"]
    except Exception as exc:  # noqa: BLE001 - never let an archive break ingest
        log.info("zip expansion failed for %s: %s", filename, exc)
        return [], [f"{filename}: could not be expanded ({type(exc).__name__})"]

    return members, notes
