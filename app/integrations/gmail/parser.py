"""Convert Gmail API message payloads into BerthSide email payloads."""
from __future__ import annotations

import base64
import re
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Callable

from app.schemas import AttachmentPayload, EmailAnalyzeRequest


def parse_message(
    message: dict,
    attachment_loader: Callable[[str, str], bytes],
) -> EmailAnalyzeRequest:
    headers = _headers(message)
    gmail_id = message.get("id") or ""
    subject = _decode_header(headers.get("subject", ""))
    sender = _decode_header(headers.get("from", ""))
    body_parts: list[str] = []
    attachments: list[AttachmentPayload] = []
    _walk_parts(
        message_id=gmail_id,
        part=message.get("payload") or {},
        attachment_loader=attachment_loader,
        body_parts=body_parts,
        attachments=attachments,
    )
    return EmailAnalyzeRequest(
        email_id=f"GMAIL-{gmail_id}",
        sender=sender,
        subject=subject,
        body="\n\n".join(p for p in body_parts if p).strip(),
        attachments=attachments,
        metadata={
            "source": "real_gmail",
            "gmail_message_id": gmail_id,
            "thread_id": message.get("threadId"),
            "history_id": message.get("historyId"),
            # When the mail was received. Version chronology is built on this,
            # so it travels with the payload: without it the only ordering signal
            # left is the order Gmail happened to return messages in.
            "received": _received_iso(message, headers),
        },
    )


def _received_iso(message: dict, headers: dict[str, str]) -> str | None:
    """Received timestamp as an ISO-8601 string, or ``None``.

    The ``Date`` header is the mail's own claim and is preferred. Gmail's
    ``internalDate`` (epoch milliseconds, set by the server) is the fallback for
    a missing or malformed header.

    Never raises. A bad header must not stop an email from being ingested, and a
    mail with no usable timestamp is processed exactly as before — just without
    chronology, so the caller falls back to registration order.
    """
    raw = headers.get("date")
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                # A Date header without a zone is read as UTC rather than local
                # time: guessing the sender's zone would silently shift every
                # version in the shipment.
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()

    internal = message.get("internalDate")
    try:
        if internal is not None:
            return datetime.fromtimestamp(
                int(internal) / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        pass
    return None


def _headers(message: dict) -> dict[str, str]:
    payload = message.get("payload") or {}
    headers = {}
    for item in payload.get("headers") or []:
        name = str(item.get("name") or "").lower()
        value = str(item.get("value") or "")
        if name:
            headers[name] = value
    return headers


def _decode_header(value: str) -> str:
    pieces = []
    for text, charset in decode_header(value or ""):
        if isinstance(text, bytes):
            pieces.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            pieces.append(text)
    return "".join(pieces).strip()


def _walk_parts(
    *,
    message_id: str,
    part: dict,
    attachment_loader: Callable[[str, str], bytes],
    body_parts: list[str],
    attachments: list[AttachmentPayload],
) -> None:
    filename = part.get("filename") or ""
    body = part.get("body") or {}
    mime = part.get("mimeType") or ""
    if filename:
        attachment_id = body.get("attachmentId")
        if attachment_id:
            content = attachment_loader(message_id, attachment_id)
        else:
            content = _decode_body_data(body.get("data") or "")
        attachments.append(AttachmentPayload(
            filename=_safe_filename(filename),
            content_base64=base64.b64encode(content).decode("ascii"),
        ))
        return

    data = body.get("data")
    if data and mime in {"text/plain", "text/html"}:
        text = _decode_body_data(data).decode("utf-8", errors="replace")
        body_parts.append(_strip_html(text) if mime == "text/html" else text)

    for child in part.get("parts") or []:
        _walk_parts(
            message_id=message_id,
            part=child,
            attachment_loader=attachment_loader,
            body_parts=body_parts,
            attachments=attachments,
        )


def _decode_body_data(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _safe_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip(" .")
    return cleaned[:160] or "gmail_attachment"
