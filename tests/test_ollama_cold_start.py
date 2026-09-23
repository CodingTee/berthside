"""Ollama cold-start budgets.

A remote GPU behind a tunnel answers slowly while it wakes, and loading a model
into VRAM is paid on /api/chat, not on the probe. Both budgets are configurable
so the engine does not read "still warming up" as "offline" and silently fall
back to the rule classifier.
"""
import json
import socket
import urllib.error

from app.config import Settings
from app.services.llm_gateway import LLMGateway, gateway


class FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def tags_payload(model: str) -> dict:
    return {"models": [{"name": model}]}


def test_probe_retries_on_the_cold_start_budget(monkeypatch):
    """First probe times out, second answers: the GPU is warming, not offline."""
    monkeypatch.setattr(gateway.settings, "ollama_probe_timeout_seconds", 0.5)
    monkeypatch.setattr(gateway.settings, "ollama_cold_start_timeout_seconds", 42.0)
    model = gateway.settings.ollama_model
    budgets = []

    def fake_urlopen(req, timeout=None):
        budgets.append(timeout)
        if len(budgets) == 1:
            raise urllib.error.URLError(socket.timeout("timed out"))
        return FakeResponse(tags_payload(model))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    status = gateway.check_ollama_status()
    assert budgets == [0.5, 42.0]
    assert status["online"] is True
    assert status["model_ready"] is True
    assert status["cold_start"] is True
    assert status["probe_seconds"] >= 0


def test_probe_does_not_retry_on_an_http_error(monkeypatch):
    """A 404 answers immediately; waiting longer cannot improve it."""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(timeout)
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    status = gateway.check_ollama_status()
    assert len(calls) == 1
    assert status["online"] is False
    assert "404" in status["error"]


def test_probe_accepts_an_explicit_budget(monkeypatch):
    """Callers can pin one budget (startup warm-up uses the quick one)."""
    monkeypatch.setattr(gateway.settings, "ollama_probe_timeout_seconds", 0.5)
    monkeypatch.setattr(gateway.settings, "ollama_cold_start_timeout_seconds", 42.0)
    budgets = []

    def fake_urlopen(req, timeout=None):
        budgets.append(timeout)
        raise urllib.error.URLError(socket.timeout("timed out"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    gateway.check_ollama_status(timeout=1.25)
    assert budgets == [1.25]


def test_chat_uses_the_request_budget(monkeypatch):
    """Model load happens in /api/chat, so that call owns the long budget."""
    monkeypatch.setattr(gateway.settings, "ollama_request_timeout_seconds", 123.0)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        seen["url"] = req.full_url
        return FakeResponse({"message": {"content": "ok"}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert gateway._call_ollama("ping") == "ok"
    assert seen["timeout"] == 123.0
    assert seen["url"].endswith("/api/chat")


def test_budgets_are_configurable_from_the_environment(monkeypatch):
    monkeypatch.setenv("OLLAMA_PROBE_TIMEOUT_SECONDS", "3")
    monkeypatch.setenv("OLLAMA_COLD_START_TIMEOUT_SECONDS", "240")
    monkeypatch.setenv("OLLAMA_REQUEST_TIMEOUT_SECONDS", "600")
    settings = Settings(_env_file=None)
    assert settings.ollama_probe_timeout_seconds == 3
    assert settings.ollama_cold_start_timeout_seconds == 240
    assert settings.ollama_request_timeout_seconds == 600


def test_probe_is_online_when_warm(monkeypatch):
    """Warm path stays on the quick budget, no extra round trip."""
    monkeypatch.setattr(gateway.settings, "ollama_probe_timeout_seconds", 0.5)
    monkeypatch.setattr(gateway.settings, "ollama_cold_start_timeout_seconds", 42.0)
    budgets = []

    def fake_urlopen(req, timeout=None):
        budgets.append(timeout)
        return FakeResponse(tags_payload(gateway.settings.ollama_model))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    status = gateway.check_ollama_status()
    assert budgets == [0.5]
    assert status["online"] is True
    assert status["cold_start"] is False


def test_gateway_copies_budgets_into_a_new_instance(monkeypatch):
    """A fresh gateway (proxy restart) reads the same configured budgets."""
    monkeypatch.setattr(gateway.settings, "ollama_cold_start_timeout_seconds", 77.0)
    monkeypatch.setattr("app.services.llm_gateway.get_settings", lambda: gateway.settings)
    fresh = LLMGateway()
    assert fresh.settings.ollama_cold_start_timeout_seconds == 77.0
