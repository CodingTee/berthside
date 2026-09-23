"""Microsoft Graph API client for fetching Outlook messages and attachments."""
from __future__ import annotations

import base64
from typing import Any

import httpx

from app.integrations.outlook.auth import get_valid_access_token

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _headers() -> dict[str, str]:
    token = get_valid_access_token()
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def get_profile() -> dict[str, Any]:
    with httpx.Client(timeout=10.0) as client:
        res = client.get(f"{GRAPH_BASE}/me", headers=_headers())
        res.raise_for_status()
        return res.json()


def list_messages(limit: int = 50) -> list[dict[str, Any]]:
    """List inbox messages ordered by receivedDateTime descending."""
    params = {
        "$top": min(limit, 100),
        "$select": "id,subject,bodyPreview,body,from,receivedDateTime,hasAttachments,isRead",
    }
    with httpx.Client(timeout=15.0) as client:
        res = client.get(
            f"{GRAPH_BASE}/me/mailFolders/inbox/messages",
            params=params,
            headers=_headers(),
        )
        res.raise_for_status()
        data = res.json()
        return data.get("value", [])


def get_attachments(message_id: str) -> list[dict[str, Any]]:
    """Fetch file attachments for a message."""
    with httpx.Client(timeout=20.0) as client:
        res = client.get(
            f"{GRAPH_BASE}/me/messages/{message_id}/attachments",
            headers=_headers(),
        )
        if res.status_code != 200:
            return []
        data = res.json()
        items = data.get("value", [])
        out = []
        for it in items:
            # Only process file attachments (skip itemAttachment or referenceAttachment)
            if it.get("@odata.type") == "#microsoft.graph.fileAttachment" or "contentBytes" in it:
                raw_bytes = b""
                if it.get("contentBytes"):
                    try:
                        raw_bytes = base64.b64decode(it["contentBytes"])
                    except Exception:
                        pass
                out.append({
                    "id": it.get("id"),
                    "name": it.get("name", "attachment"),
                    "contentType": it.get("contentType", "application/octet-stream"),
                    "size": it.get("size", len(raw_bytes)),
                    "contentBytes": raw_bytes,
                })
        return out
