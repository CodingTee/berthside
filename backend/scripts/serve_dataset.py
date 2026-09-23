#!/usr/bin/env python3
"""Serve a static bundle over HTTP exactly like the official dataset container.

The judge's Docker server exposes:

    GET /emails                 -> list of email records
    GET /emails/{id}            -> one email record
    GET /attachments/{name}     -> attachment bytes
    GET /sample_submission      -> the submission skeleton

Our pipeline only ever talks to it through ``loader.Inbox`` (``DATA_SOURCE``
set to an http:// URL), which means **that path is never exercised when we
develop against the local folder** — a silent single-point-of-failure if the
final evaluation runs against the container instead of a folder.

This script reproduces those endpoints from the evaluation corpus
(``backend/data/corpus``) so the
HTTP branch can be regression-tested locally, without Docker:

    python scripts/serve_dataset.py            # http://127.0.0.1:8099
    DATA_SOURCE=http://127.0.0.1:8099 python scripts/tune_eval.py

Dev-only: it is a stdlib static server, never used in production.
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    root: Path = Path(".")

    # -- helpers ---------------------------------------------------------
    def _send(self, payload: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj) -> None:
        self._send(json.dumps(obj).encode(), "application/json")

    def _not_found(self) -> None:
        self._send(b"not found", "text/plain", 404)

    # -- routes ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        path = self.path.split("?")[0].lstrip("/")

        if path == "emails":
            inbox = sorted((self.root / "inbox").glob("email_*.json"))
            self._json([json.loads(p.read_text()) for p in inbox])
            return

        if path.startswith("emails/"):
            f = self.root / "inbox" / f"{path.split('/', 1)[1]}.json"
            self._json(json.loads(f.read_text())) if f.exists() else self._not_found()
            return

        if path == "sample_submission":
            f = self.root / "sample_submission.json"
            self._json(json.loads(f.read_text())) if f.exists() else self._not_found()
            return

        # anything else = an attachment path such as attachments/email_004_SI.txt
        f = self.root / path
        if f.is_file():
            self._send(f.read_bytes(), "application/octet-stream")
        else:
            self._not_found()

    def log_message(self, *args) -> None:  # keep the console quiet
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="",
                    help="bundle directory; defaults to DATA_SOURCE from the "
                         "application config, so no machine-specific path is baked in")
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()

    if args.root:
        root = Path(args.root).expanduser()
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.config import get_settings
        root = Path(get_settings().data_source)

    if not (root / "inbox").is_dir():
        print(f"no inbox/ under {root}\n"
              f"  pass --root <bundle dir>, or set DATA_SOURCE")
        return 1

    Handler.root = root
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"serving {root} at http://127.0.0.1:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
