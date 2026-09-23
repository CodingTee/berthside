"""Classification source tracking + runtime provider override."""
import sys
import types

from app.models import ReportRecord
from app.services import ai_service
from app.services.workflow import EmailVerdict, _apply_verdict


def test_current_provider_falls_back_to_settings():
    ai_service.set_runtime_provider(None)
    assert ai_service.current_provider() == ai_service.settings.ai_provider


def test_runtime_override_takes_precedence():
    ai_service.set_runtime_provider("rule")
    try:
        assert ai_service.current_provider() == "rule"
        cls = ai_service.classify_email(
            {"subject": "buy now", "body": "cheap meds", "attachments": []})
        assert cls.source == "rule-classifier"
    finally:
        ai_service.set_runtime_provider(None)


def test_runtime_override_rejects_unknown_provider():
    ai_service.set_runtime_provider("banana")
    try:
        assert ai_service.current_provider() == ai_service.settings.ai_provider
    finally:
        ai_service.set_runtime_provider(None)


def test_ollama_path_marks_source(monkeypatch):
    fake = types.ModuleType("app.services.llm_gateway")
    fake.gateway = types.SimpleNamespace(
        check_ollama_status=lambda: {"online": True, "model_ready": True},
        classify_ambiguous_email=lambda email, backend=None: {
            "category": "SPAM", "confidence": 0.88,
            "reason": "llm said so", "source": "llm-text (ollama-x)"},
    )
    monkeypatch.setitem(sys.modules, "app.services.llm_gateway", fake)
    ai_service.set_runtime_provider("ollama")
    try:
        cls = ai_service.classify_email(
            {"subject": "hi", "body": "x", "attachments": []})
        assert cls.source == "llm-text (ollama-x)"
        assert cls.reason == "llm said so"
    finally:
        ai_service.set_runtime_provider(None)


def test_apply_verdict_writes_classify_source():
    verdict = EmailVerdict(
        category="SPAM", confidence=0.9, classification_reason="x",
        classify_source="llm-text (ollama-qwen2.5vl:7b)", status="SKIPPED")
    report = ReportRecord(email_id="T-CS-1")
    _apply_verdict(report, verdict)
    assert report.classify_source == "llm-text (ollama-qwen2.5vl:7b)"


def test_email_verdict_defaults_to_rule_source():
    v = EmailVerdict(category="GENERAL", confidence=0.5,
                     classification_reason="r", status="SKIPPED")
    assert v.classify_source == "rule-classifier"
