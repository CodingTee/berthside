"""Thin Gmail API client wrapper."""
from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.integrations.gmail.auth import load_credentials


def gmail_libraries_available() -> bool:
    try:
        import googleapiclient.discovery  # noqa: F401
        return True
    except Exception:
        return False


def build_service():
    """Build an authenticated Gmail API service."""
    if not gmail_libraries_available():
        raise RuntimeError("google-api-python-client is not installed")
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)


def list_message_ids(max_results: int | None = None, query: str | None = None) -> list[str]:
    settings = get_settings()
    service = build_service()
    response = (
        service.users()
        .messages()
        .list(
            userId="me",
            maxResults=max_results or settings.gmail_max_results,
            q=query or settings.gmail_query,
        )
        .execute()
    )
    return [m["id"] for m in response.get("messages", [])]


def get_message(message_id: str) -> dict[str, Any]:
    service = build_service()
    return (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )


def get_attachment(message_id: str, attachment_id: str) -> bytes:
    import base64

    service = build_service()
    response = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    data = response.get("data", "")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def send_message(raw_b64: str, thread_id: str | None = None) -> dict[str, Any]:
    """Send an RFC 2822 base64url encoded message via authenticated Gmail API."""
    service = build_service()
    body: dict[str, Any] = {"raw": raw_b64}
    if thread_id:
        body["threadId"] = thread_id
    return (
        service.users()
        .messages()
        .send(userId="me", body=body)
        .execute()
    )
