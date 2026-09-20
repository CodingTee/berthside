#!/usr/bin/env python3
"""Mock P3 AI service — lets P2 prove the Backend → AI → Backend path NOW,
before P3's real service exists.

Implements exactly the contract documented in app/services/ai_service.py:

    POST /classify  {"email": {...}}                       -> {"category": "...", "confidence": 0.9}
    POST /extract   {"doc_type":"SI","filename":"...","content_base64":"..."}
                    -> {"fields": {7 keys}, "readable": true}

It delegates to the same rule engine the backend uses as fallback, so the
verdict is identical — the point of this service is to prove the *wiring*
(HTTP, auth header, retries, base64 payloads), not to be smarter.

Run:
    python scripts/mock_ai_service.py --port 8001

Then point the backend at it:
    AI_PROVIDER=hybrid  AI_SERVICE_URL=http://127.0.0.1:8001
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Make the backend's own services importable so the mock behaves identically.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.services import extractor  # noqa: E402
from app.services.classifier import classify as rule_classify  # noqa: E402

FIELDS = list(extractor.COMPLETENESS_FIELDS)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return

        handler = {
            "/classify": self._classify,
            "/extract": self._extract,
        }.get(self.path.rstrip("/"))
        if handler is None:
            self._send(404, {"error": f"unknown path {self.path}"})
            return
        try:
            self._send(200, handler(payload))
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("", "/health"):
            self._send(200, {"status": "ok", "service": "mock-ai"})
        else:
            self._send(404, {"error": "not found"})

    # -- endpoints -----------------------------------------------------
    @staticmethod
    def _classify(payload: dict) -> dict:
        email = payload.get("email") or {}
        result = rule_classify(email)
        return {"category": result.category, "confidence": result.confidence,
                "reason": result.reason}

    @staticmethod
    def _extract(payload: dict) -> dict:
        doc_type = (payload.get("doc_type") or "SI").upper()
        filename = payload.get("filename") or ""
        content = base64.b64decode(payload.get("content_base64") or "")

        if filename.lower().endswith(".txt"):
            text = content.decode("utf-8", errors="replace")
        else:
            text = ""  # pretend we OCR'd nothing

        result = extractor.extract_fields(text, doc_type)
        return {
            "doc_type": doc_type,
            "filename": filename,
            "fields": result.fields,
            "missing": result.missing,
            "readable": result.readable,
        }

    # -- plumbing ------------------------------------------------------
    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):  # keep the console readable
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    print(f"mock P3 AI service on http://{args.host}:{args.port}"
          f"  (/classify, /extract)")
    HTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
