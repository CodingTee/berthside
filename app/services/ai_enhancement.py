"""Optional AI assistance built on deterministic verification results.

The functions here never compare SI vs BL and never decide correctness. They
only rephrase structured issues, draft reviewer-facing communication, and flag
ambiguous OCR-like text for human confirmation.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import IssueRecord, ResolutionRecord, ShipmentRecord

settings = get_settings()

SAFETY_NOTE = (
    "This is assistance only. The values differ; a human must confirm which "
    "document or value is authoritative before any official document is changed."
)


def mismatch_assistance(db: Session, issue_id: int) -> dict[str, Any]:
    issue = db.query(IssueRecord).filter_by(id=issue_id).first()
    if issue is None:
        raise KeyError("issue not found")

    payload = _issue_payload(issue)
    remote = _remote_assist("mismatch_explanation", payload)
    if remote:
        return _coerce_mismatch_remote(issue, remote)

    return _rule_mismatch_assistance(issue)


def correction_email_draft(db: Session, shipment_id: int) -> dict[str, Any]:
    shipment = db.query(ShipmentRecord).filter_by(id=shipment_id).first()
    if shipment is None:
        raise KeyError("shipment not found")

    issues = (
        db.query(IssueRecord)
        .filter_by(shipment_id=shipment_id)
        .filter(IssueRecord.status != "SUPERSEDED")
        .order_by(IssueRecord.id)
        .all()
    )
    payload = {
        "shipment_id": shipment_id,
        "shipment_key": shipment.shipment_key,
        "reference_number": shipment.reference_number,
        "issues": [_issue_payload(issue) for issue in issues],
        "safety_note": SAFETY_NOTE,
    }
    remote = _remote_assist("correction_email", payload)
    if remote:
        return {
            "shipment_id": shipment_id,
            "provider": str(remote.get("provider") or "remote"),
            "confidence": _confidence(remote.get("confidence"), default="MEDIUM"),
            "subject": str(remote.get("subject") or _email_subject(shipment)),
            "body": str(remote.get("body") or _email_body(shipment, issues)),
            "requires_human_review": True,
        }

    return {
        "shipment_id": shipment_id,
        "provider": "rule",
        "confidence": "MEDIUM",
        "subject": _email_subject(shipment),
        "body": _email_body(shipment, issues),
        "requires_human_review": True,
    }


def ambiguous_interpretation(
    text: str, field_name: Optional[str] = None
) -> dict[str, Any]:
    payload = {"text": text, "field_name": field_name, "safety_note": SAFETY_NOTE}
    remote = _remote_assist("ambiguous_interpretation", payload)
    if remote:
        return {
            "provider": str(remote.get("provider") or "remote"),
            "input_text": text,
            "field_name": field_name,
            "interpreted_value": remote.get("interpreted_value"),
            "confidence": _confidence(remote.get("confidence"), default="LOW"),
            "needs_human_review": True,
            "explanation": str(remote.get("explanation") or _ambiguous_explanation(text)),
        }

    interpreted, confidence = _rule_interpret_ambiguous(text, field_name)
    return {
        "provider": "rule",
        "input_text": text,
        "field_name": field_name,
        "interpreted_value": interpreted,
        "confidence": confidence,
        "needs_human_review": True,
        "explanation": _ambiguous_explanation(text),
    }


def _issue_payload(issue: IssueRecord) -> dict[str, Any]:
    resolution = None
    return {
        "issue_id": issue.id,
        "field_name": issue.field_name,
        "si_value": issue.si_value,
        "bl_value": issue.bl_value,
        "difference": issue.difference,
        "deterministic_explanation": issue.explanation,
        "status": issue.status,
        "resolution": resolution,
        "safety_note": SAFETY_NOTE,
    }


def _rule_mismatch_assistance(issue: IssueRecord) -> dict[str, Any]:
    label = _label(issue.field_name)
    si = _fmt(issue.si_value)
    bl = _fmt(issue.bl_value)
    diff = f" The measured difference is {issue.difference}." if issue.difference else ""
    explanation = (
        f"The {label} value in the current BL is {bl}, while the SI shows {si}."
        f"{diff}"
    )
    suggestion = (
        f"Review the {label} discrepancy. If the SI is confirmed as the "
        f"authoritative instruction, the BL draft could be updated to {si}."
    )
    return {
        "issue_id": issue.id,
        "field_name": issue.field_name,
        "provider": "rule",
        "confidence": "HIGH" if issue.si_value is not None and issue.bl_value is not None else "MEDIUM",
        "explanation": explanation,
        "suggestion": suggestion,
        "safety_note": SAFETY_NOTE,
        "source_values": {"si_value": issue.si_value, "bl_value": issue.bl_value},
    }


def _coerce_mismatch_remote(issue: IssueRecord, remote: dict[str, Any]) -> dict[str, Any]:
    fallback = _rule_mismatch_assistance(issue)
    return {
        "issue_id": issue.id,
        "field_name": issue.field_name,
        "provider": str(remote.get("provider") or "remote"),
        "confidence": _confidence(remote.get("confidence"), default=fallback["confidence"]),
        "explanation": str(remote.get("explanation") or fallback["explanation"]),
        "suggestion": _uncertain_suggestion(str(remote.get("suggestion") or fallback["suggestion"])),
        "safety_note": SAFETY_NOTE,
        "source_values": fallback["source_values"],
    }


def _email_subject(shipment: ShipmentRecord) -> str:
    ref = shipment.reference_number or shipment.shipment_key
    return f"Action Required: BL discrepancy review for shipment {ref}"


def _email_body(shipment: ShipmentRecord, issues: list[IssueRecord]) -> str:
    ref = shipment.reference_number or shipment.shipment_key
    lines = [
        "Hello,",
        "",
        "We identified discrepancies between the Shipping Instruction and the current Bill of Lading draft.",
        "",
        f"Shipment: {ref}",
        "",
    ]
    if issues:
        for issue in issues:
            lines.extend([
                f"{_label(issue.field_name)}:",
                f"SI: {_fmt(issue.si_value)}",
                f"BL: {_fmt(issue.bl_value)}",
                "",
            ])
    else:
        lines.append("No active discrepancies are currently recorded.")
        lines.append("")
    lines.extend([
        "Please review and confirm the correct value before any official document is updated.",
        "",
        "Regards,",
    ])
    return "\n".join(lines)


def _rule_interpret_ambiguous(text: str, field_name: Optional[str]) -> tuple[Optional[str], str]:
    value = text.strip()
    confidence = "LOW"
    if field_name in {"gross_weight_kg", "container_count"} or re.search(r"\d|[OoIl]", value):
        candidate = (
            value.replace("O", "0")
            .replace("o", "0")
            .replace("I", "1")
            .replace("l", "1")
        )
        if candidate != value and re.search(r"\d", candidate):
            return candidate, "LOW"
    return value or None, confidence


def _ambiguous_explanation(text: str) -> str:
    if re.search(r"[OoIl]", text or ""):
        return (
            "The text contains characters that can be confused during OCR, such "
            "as O/0 or I/1. Treat this as low confidence and ask a human to confirm."
        )
    return "The text may require human confirmation before it is used as a shipping value."


def _remote_assist(task: str, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    # Only invoke external/remote AI if provider is configured for AI
    # (cascade, hybrid, remote, or local ollama -- no cloud key needed).
    if settings.ai_provider not in ("cascade", "hybrid", "remote", "ollama"):
        return None

    # 1. First priority: Use Cascading LLM Gateway if keys are available
    try:
        from app.services.llm_gateway import gateway
        use_ollama = settings.ai_provider == "ollama"
        if use_ollama and not gateway.check_ollama_status().get("online"):
            return None
        keys = gateway._get_keys()
        if use_ollama or any(keys.values()):
            if task == "correction_email":
                ref = payload.get("reference_number") or payload.get("shipment_key") or "Shipment"
                issues = payload.get("issues", [])
                disc_list = [
                    {
                        "field": iss.get("field_name", ""),
                        "expected": iss.get("si_value", ""),
                        "actual": iss.get("bl_value", ""),
                        "discrepancy_type": "mismatch" if iss.get("si_value") != iss.get("bl_value") else "unreadable"
                    }
                    for iss in issues
                ]
                res = gateway.generate_hitl_draft(
                    email={"subject": f"Shipment {ref}", "from": "customer_ops@client.com"},
                    discrepancies=disc_list,
                    backend="ollama" if use_ollama else "cascade",
                )
                if res and res.get("draft_body"):
                    return {
                        "provider": res.get("source", "llm-gateway"),
                        "confidence": "HIGH",
                        "subject": res.get("draft_subject", f"Action Required: BL discrepancy review for shipment {ref}"),
                        "body": res.get("draft_body", ""),
                    }
            elif task == "mismatch_explanation":
                field = payload.get("field_name", "")
                si = payload.get("si_value")
                bl = payload.get("bl_value")
                diff = payload.get("difference", "")
                sys_prompt = "You are a shipping document auditor. Explain the discrepancy between SI and Draft BL and suggest how human operations should verify it. Respond with JSON: {\"explanation\": \"...\", \"suggestion\": \"...\"}"
                prompt = f"Field: {field}\nSI Value: {si}\nBL Value: {bl}\nDifference: {diff}"
                res = gateway.call_text_ollama(prompt, sys_prompt) if use_ollama else gateway.call_text_cascade(prompt, sys_prompt)
                if res and res.get("content"):
                    parsed = gateway._extract_json_from_text(res["content"])
                    if parsed:
                        return {
                            "provider": res.get("provider", "llm-gateway"),
                            "confidence": "HIGH",
                            "explanation": parsed.get("explanation"),
                            "suggestion": parsed.get("suggestion"),
                        }
            elif task == "ambiguous_interpretation":
                text = payload.get("text", "")
                field = payload.get("field_name", "")
                sys_prompt = f"Interpret this ambiguous OCR shipping text for field '{field}'. Return JSON: {{\"interpreted_value\": \"...\", \"explanation\": \"...\"}}"
                res = gateway.call_text_ollama(text, sys_prompt) if use_ollama else gateway.call_text_cascade(text, sys_prompt)
                if res and res.get("content"):
                    parsed = gateway._extract_json_from_text(res["content"])
                    if parsed:
                        return {
                            "provider": res.get("provider", "llm-gateway"),
                            "interpreted_value": parsed.get("interpreted_value"),
                            "confidence": "HIGH",
                            "explanation": parsed.get("explanation"),
                        }
    except Exception:
        pass

    # 2. Secondary fallback: Remote AI service URL if configured
    if settings.ai_provider not in ("remote", "hybrid") or not settings.ai_service_url:
        return None
    url = settings.ai_service_url.rstrip("/") + "/assist"
    headers = {"Content-Type": "application/json"}
    if settings.ai_api_key:
        headers["Authorization"] = f"Bearer {settings.ai_api_key}"
    try:
        req = Request(
            url,
            data=json.dumps({"task": task, "payload": payload}).encode("utf-8"),
            headers=headers,
        )
        with urlopen(req, timeout=settings.ai_timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):
        return None


def _confidence(value: Any, default: str = "MEDIUM") -> str:
    if isinstance(value, (int, float)):
        if value >= 0.75:
            return "HIGH"
        if value >= 0.45:
            return "MEDIUM"
        return "LOW"
    value = str(value or default).upper()
    return value if value in {"HIGH", "MEDIUM", "LOW"} else default


def _uncertain_suggestion(text: str) -> str:
    lowered = text.lower()
    unsafe = [" is wrong", " must be updated", " confirmed correction"]
    if any(token in lowered for token in unsafe):
        return (
            f"{text}\n\nNote: this remains a suggested action only. Confirm the "
            "authoritative value before changing any official document."
        )
    return text


def _label(field_name: str) -> str:
    return field_name.replace("_kg", "").replace("_", " ")


def _fmt(value: Any) -> str:
    if value is None:
        return "not provided"
    return str(value)
