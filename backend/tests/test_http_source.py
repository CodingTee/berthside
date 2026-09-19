"""The HTTP branch of the data source must keep working.

Everything we develop and score against is a *local folder*, but the official
dataset container serves the same inbox over HTTP (``DATA_SOURCE=http://...``).
That branch goes through ``loader.Inbox`` and is therefore easy to break
without noticing — the folder tests stay green while the container path rots.

These tests spin up ``scripts/serve_dataset.py`` against the real bundle and
drive the loader through it. They skip (loudly) when the bundle is absent.
"""
from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

BUNDLE = Path("C:/Users/nicol/Downloads/sdoc-hackathon-bundle")


def _free_server():
    """Start the dataset HTTP server on an ephemeral port; return (srv, url)."""
    from scripts.serve_dataset import Handler

    Handler.root = BUNDLE
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture(scope="module")
def http_source():
    if not (BUNDLE / "inbox").is_dir():
        pytest.skip(f"dataset bundle not present at {BUNDLE}")
    srv, url = _free_server()
    yield url
    srv.shutdown()


def test_http_source_is_detected():
    from app.services.loader import Inbox

    assert Inbox("http://localhost:8080").is_http is True
    assert Inbox(str(BUNDLE)).is_http is False


def test_http_source_lists_the_whole_inbox(http_source):
    from app.services.loader import Inbox

    emails = Inbox(http_source).emails()
    assert len(emails) > 0
    assert "email_id" in emails[0]


def test_http_source_serves_attachments_and_submission(http_source):
    """The three endpoints the pipeline depends on at runtime."""
    from app.services.loader import Inbox

    inbox = Inbox(http_source)
    email = next(e for e in inbox.emails() if e.get("attachments"))
    payload = inbox.read_bytes(email["attachments"][0])
    assert payload, "attachment came back empty over HTTP"

    sample = inbox.sample_submission()
    assert isinstance(sample, dict) and sample


def test_http_source_runs_through_the_service_layer(http_source, monkeypatch):
    """`inbox_service` is what the app actually calls — cache must not leak."""
    from app.services import inbox_service

    monkeypatch.setenv("DATA_SOURCE", http_source)
    inbox_service._inbox.cache_clear()

    emails = inbox_service.all_emails()
    assert len(emails) > 0
    assert inbox_service.get_email("does_not_exist") is None  # 404 -> None, not a crash

    inbox_service._inbox.cache_clear()
