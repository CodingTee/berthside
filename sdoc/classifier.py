"""Email classification.

Ordered rule chain: the first matching rule wins. The chain is data-driven
so new rules (or an LLM fallback appended after the deterministic ones) can
be registered without touching the orchestrator.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

SPAM_DOMAINS = {"prize-claims.info", "parcel-track.co", "webmail-verify.co",
                "logistics-deals.biz", "crypto-invest.net", "secure-mailbox.org"}

SPAM_MARKERS = ["congratulations", "gift card", "click here", "verify your account",
                "bitcoin", "bank details", "undelivered messages", "hot singles",
                "one weird trick", "avoid suspension", "claim your", "free iphone"]


@dataclass
class Classification:
    category: str
    rule: str                      # which rule fired (audit trail)
    decided_by: str = "rule"       # "rule" | "llm" | "human" -- extensibility hook


@dataclass
class Classifier:
    """rules: ordered (name, predicate(email) -> bool). First hit wins."""
    rules: list[tuple[str, Callable[[dict], bool]]] = field(default_factory=list)

    def __post_init__(self):
        if not self.rules:
            self.rules = self._default_rules()

    def classify(self, email: dict) -> Classification:
        for name, predicate in self.rules:
            if predicate(email):
                return Classification(category=name, rule=name)
        return Classification(category="GENERAL", rule="default")

    # ------------------------------------------------------------------
    @staticmethod
    def _default_rules():
        def spam(email):
            domain = email.get("from", "").lower().split("@")[-1]
            if domain in SPAM_DOMAINS:
                return True
            low = (email.get("subject", "") + " " + email.get("body", "")).lower()
            return any(m in low for m in SPAM_MARKERS)

        def si_request(email):
            s = email.get("subject", "").upper()
            return ("REQUEST SI" in s or "CUST SI" in s or "SI NEEDED" in s
                    or re.search(r"\bSI - ", s) is not None
                    or "Please find Shipping instruction for" in email.get("body", ""))

        def bl_comparison(email):
            s = email.get("subject", "").upper()
            body = email.get("body", "")
            return ("TO CONFIRM DOCS" in s or "REQUEST BL DRAFT" in s
                    or s.startswith("DRAFT BL") or "AMEND BL" in s
                    or re.search(r"draft\s+BL", body, re.I) is not None
                    or re.search(r"draft\s+bill\s+of\s+lading", body, re.I) is not None)

        def invoice_query(email):
            s = email.get("subject", "").upper()
            return ("RAK BILLING" in s or "CANCEL INVOICE" in s or "MISSING GR" in s
                    or "LOCAL CHARGES" in s or "D & D" in s or "D&D" in s
                    or "TOTAL FREIGHT" in s
                    or re.search(r"invoice \d+", email.get("body", ""), re.I) is not None)

        # Order matters: spam first; SI before BL because SI bodies mention
        # "revert with draft BL"; everything else falls through to GENERAL.
        return [
            ("SPAM", spam),
            ("SI_REQUEST", si_request),
            ("BL_COMPARISON", bl_comparison),
            ("INVOICE_QUERY", invoice_query),
        ]
