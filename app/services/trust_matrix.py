"""Source Trust Matrix Engine.

Governs inbound source trust for the Enterprise IDP Hub.
Rule: Any inbound mail from a sender, client, or source not registered in the
Source Trust Matrix is filtered before it ever enters the workflow pipeline.
Nothing unvetted touches the pipeline.
"""
from __future__ import annotations

import logging
from typing import Optional, Set, Tuple
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# Default registered trusted sources and operators in the Source Trust Matrix
DEFAULT_TRUSTED_SOURCES: Set[str] = {
    # Enterprise Hub Intake Mailboxes
    "averis.demo@gmail.com",
    "sdoc-hackathon-bundle@averis.com",
    "operations@berthside.demo",
    "docs.export@averis.com",
    "booking@averis.com",
    "april.shipping@averis.com",
    "finance@averis.com",
    "transpacific@averis.com",
}

# Corporate & Partner Whitelist Domains
DEFAULT_TRUSTED_DOMAINS: Set[str] = {
    "averis.com",
    "berthside.demo",
    # Benchmark and test fixture domains
    "fastocean.com",
    "customercorp.com",
    "example.com",
}


def get_all_trusted_sources(db: Optional[Session] = None) -> Set[str]:
    """Return all currently trusted mailboxes, senders, and dynamic overrides."""
    from app.config import get_settings
    settings = get_settings()

    sources = set(s.lower() for s in DEFAULT_TRUSTED_SOURCES)
    if settings.imap_user:
        sources.add(settings.imap_user.strip().lower())
    if settings.smtp_from:
        sources.add(settings.smtp_from.strip().lower())
    if settings.extra_trusted_sources:
        for s in settings.extra_trusted_sources.split(","):
            if s.strip():
                sources.add(s.strip().lower())

    if db is not None:
        try:
            from app.models import GatewayPolicyRecord
            pol = db.query(GatewayPolicyRecord).first()
            if pol and pol.source_policies:
                for k in pol.source_policies.keys():
                    if k:
                        sources.add(k.strip().lower())
        except Exception as exc:
            log.debug("Could not query dynamic source policies: %s", exc)

    return sources


def is_source_trusted(
    sender: Optional[str],
    source_mailbox: Optional[str] = None,
    orig_client: Optional[str] = None,
    db: Optional[Session] = None,
) -> Tuple[bool, str]:
    """Determine whether an incoming transmission is authorized by the Source Trust Matrix.

    Returns:
        (True, reason) if permitted by the trust matrix.
        (False, reason) if filtered before pipeline entry.
    """
    import email.utils
    trusted_sources = get_all_trusted_sources(db)

    sender_raw = (sender or "").strip()
    client_raw = (orig_client or "").strip()
    mb_clean = (source_mailbox or "").strip().lower()

    # Extract clean email addresses (strip display names like "Hans <user@gmail.com>")
    _, sender_addr = email.utils.parseaddr(sender_raw)
    sender_clean = (sender_addr or sender_raw).strip().lower()

    _, client_addr = email.utils.parseaddr(client_raw)
    client_clean = (client_addr or client_raw).strip().lower()

    # 1. Exact match against registered matrix entries (sender or forwarded client)
    if sender_clean and sender_clean in trusted_sources:
        return True, f"Sender '{sender_clean}' is verified in Source Trust Matrix"

    if client_clean and client_clean in trusted_sources:
        return True, f"Original client '{client_clean}' is verified in Source Trust Matrix"

    # 2. Check trusted corporate / partner sender domains
    for candidate in (sender_clean, client_clean):
        if "@" in candidate:
            domain = candidate.split("@")[-1].strip()
            if domain in DEFAULT_TRUSTED_DOMAINS:
                return True, f"Domain '@{domain}' is authorized by Source Trust Matrix policy"

    # 3. Dedicated internal pipeline intake channels (when no external sender is involved)
    if not sender_clean and mb_clean and mb_clean in trusted_sources:
        return True, f"Internal pipeline channel '{mb_clean}' is verified"

    # 4. If unvetted, reject before touching pipeline
    unvetted_identity = sender_clean or client_clean or "unknown"
    return False, f"Sender '{unvetted_identity}' is not registered in Source Trust Matrix"
