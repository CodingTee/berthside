"""Parse delivery status notifications and write them back to dispatch records."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import DeliveryEventRecord, DispatchRecord, utcnow
from app.tracing import get_trace_id

_DSN_SUBJECT_MARKERS = (
    "delivery status notification",
    "undelivered mail returned",
    "returned mail",
    "mail delivery failed",
    "delivery failure",
    "failure notice",
    "undeliverable",
    "bounced mail",
)

_STAGE_REF = re.compile(r"Ref\s*#\s*([A-Za-z0-9._:-]+)", re.I)
_ANGLE_ID = re.compile(r"<[^>\s]+>")


def _header_text(value: Optional[str]) -> str:
    from email.header import decode_header, make_header

    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def is_delivery_status_notification(msg) -> bool:
    """Recognise a DSN before it enters the document pipeline."""
    if (msg.get_content_type() or "").lower() == "multipart/report":
        if (str(msg.get_param("report-type") or "")).lower() == "delivery-status":
            return True
    sender = _header_text(msg.get("From", "")).lower()
    if "mailer-daemon" in sender or "postmaster" in sender:
        return True
    subject = _header_text(msg.get("Subject", "")).lower()
    return any(marker in subject for marker in _DSN_SUBJECT_MARKERS)


def _part_text(part) -> str:
    try:
        raw = part.get_payload(decode=True)
        if raw:
            return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    except Exception:
        pass
    payload = part.get_payload()
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        return "\n".join(
            item.as_string() if hasattr(item, "as_string") else str(item)
            for item in payload
        )
    return ""


def _clean_address(value: str) -> str:
    return value.split(";", 1)[-1].strip().strip("<>").strip()


def _parse_status_block(text: str, info: Dict[str, Any]) -> None:
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip().lower(), val.strip()
        if key == "original-message-id" and not info["original_message_id"]:
            info["original_message_id"] = val
        elif key == "final-recipient":
            info["recipient"] = _clean_address(val)
        elif key == "original-recipient" and not info["recipient"]:
            info["recipient"] = _clean_address(val)
        elif key == "action":
            info["action"] = val.lower()
        elif key == "status":
            info["status_code"] = val
        elif key == "diagnostic-code":
            info["diagnostic"] = val[:1000]
        elif key == "original-subject" and not info["original_subject"]:
            info["original_subject"] = val


def parse_delivery_status(msg) -> Dict[str, Any]:
    """Pull the original Message-ID, recipient and DSN fields from a bounce."""
    info: Dict[str, Any] = {
        "subject": _header_text(msg.get("Subject", "")),
        "sender": _header_text(msg.get("From", "")),
        "original_message_id": (msg.get("Original-Message-ID") or "").strip(),
        "original_subject": "",
        "recipient": "",
        "action": "",
        "status_code": "",
        "diagnostic": "",
        "references": [],
    }
    for header in ("In-Reply-To", "References"):
        info["references"].extend(_ANGLE_ID.findall(msg.get(header, "") or ""))

    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = (part.get_content_type() or "").lower()
        if ctype == "message/delivery-status":
            _parse_status_block(_part_text(part), info)
        elif ctype in ("message/rfc822", "text/rfc822-headers"):
            payload = part.get_payload()
            original = payload[0] if isinstance(payload, list) and payload else payload
            if hasattr(original, "get"):
                if not info["original_message_id"]:
                    info["original_message_id"] = (original.get("Message-ID") or "").strip()
                if not info["original_subject"]:
                    info["original_subject"] = _header_text(original.get("Subject", ""))
                if not info["recipient"]:
                    info["recipient"] = _clean_address(str(original.get("To", "")))
    return info


def _id_variants(value: str) -> List[str]:
    raw = (value or "").strip()
    bare = raw.strip("<>").strip()
    if not bare:
        return []
    return list(dict.fromkeys([raw, "<%s>" % bare, bare]))


def _event_type(info: Dict[str, Any]) -> str:
    action = (info["action"] or "").lower()
    code = info["status_code"] or ""
    if action == "delivered":
        return "DELIVERED"
    if action == "delayed" or code.startswith("4."):
        return "DELAYED"
    if action in ("failed", "failure") or code.startswith("5."):
        return "BOUNCED"
    subject = (info["subject"] or "").lower()
    if any(k in subject for k in ("failure", "undelivered", "returned mail", "bounce")):
        return "BOUNCED"
    return "FAILED"


def _find_dispatch(db: Session, info: Dict[str, Any]) -> Optional[DispatchRecord]:
    ids = list(info["references"]) + [info["original_message_id"]]
    for candidate in ids:
        variants = _id_variants(candidate)
        if not variants:
            continue
        row = (
            db.query(DispatchRecord)
            .filter(DispatchRecord.message_id.in_(variants))
            .order_by(DispatchRecord.id.desc())
            .first()
        )
        if row:
            return row

    for subject in (info["original_subject"], info["subject"]):
        match = _STAGE_REF.search(subject or "")
        if match:
            row = (
                db.query(DispatchRecord)
                .filter(DispatchRecord.stage_id == match.group(1))
                .order_by(DispatchRecord.id.desc())
                .first()
            )
            if row:
                return row

    if info["recipient"]:
        return (
            db.query(DispatchRecord)
            .filter(DispatchRecord.recipient == info["recipient"])
            .order_by(DispatchRecord.id.desc())
            .first()
        )
    return None


def record_delivery_receipt(db: Session, msg) -> Dict[str, Any]:
    """Persist a DSN event and update the dispatch it belongs to."""
    info = parse_delivery_status(msg)
    event_type = _event_type(info)
    dispatch = _find_dispatch(db, info)

    if info["original_message_id"]:
        prior = (
            db.query(DeliveryEventRecord)
            .filter(
                DeliveryEventRecord.message_id == info["original_message_id"],
                DeliveryEventRecord.event_type == event_type,
                DeliveryEventRecord.recipient == (info["recipient"] or None),
            )
            .first()
        )
        if prior:
            return {
                "status": "DUPLICATE", "event_id": prior.id, "event_type": event_type,
                "matched": bool(dispatch), "dispatch_id": dispatch.id if dispatch else None,
                "recipient": info["recipient"], "message_id": info["original_message_id"],
            }

    event = DeliveryEventRecord(
        dispatch_id=dispatch.id if dispatch else None,
        message_id=info["original_message_id"] or None,
        event_type=event_type,
        recipient=info["recipient"] or None,
        action=info["action"] or None,
        status_code=info["status_code"] or None,
        diagnostic=info["diagnostic"] or None,
        raw_subject=info["subject"] or None,
        raw_from=info["sender"] or None,
        trace_id=get_trace_id(),
    )
    db.add(event)
    if dispatch:
        dispatch.delivery_state = event_type
        dispatch.delivery_event_at = utcnow()
        dispatch.delivery_detail = info["diagnostic"] or info["status_code"] or None
    db.commit()

    return {
        "status": "RECORDED",
        "event_id": event.id,
        "event_type": event_type,
        "matched": bool(dispatch),
        "dispatch_id": dispatch.id if dispatch else None,
        "recipient": info["recipient"],
        "message_id": info["original_message_id"],
    }
