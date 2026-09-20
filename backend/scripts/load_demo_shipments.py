"""Load the curated demo shipments through the running backend.

The demo dataset is static files; this script hands each demo email to
``POST /api/process`` — the same endpoint the Dashboard uses — so everything
downstream (classification, extraction, comparison, versioning, shipment
grouping) is the real pipeline, not a fixture loaded straight into the tables.

It is idempotent: ``/api/process`` already refuses to run twice for unchanged
content, so re-running only touches what actually changed. ``--force`` asks for
a genuine re-run.

The Dataset Is Loaded In Two Steps
----------------------------------
1. ``POST /api/v1/ingest`` persists the email + attachments and upserts the
   EmailRecord, so the mail exists in the backend the same way an external
   gateway's mail does.
2. ``POST /emails/{id}/process`` runs the *db-backed* core
   (``workflow.process_email``). This is the step that creates shipments,
   documents and version history — ``/api/process`` deliberately runs the
   stateless core and writes no shipment rows, which is why the loader cannot
   stop at step 1.

It is idempotent: re-running does not duplicate work that already succeeded.
``--force`` asks for a genuine re-run of the processing step.
"""
from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _attachment_payload(name: str, data: bytes) -> dict:
    # Binary documents must travel as base64; plain text docs could travel as
    # content_text, but one path is simpler and exercises the same code as the
    # real Gmail sync.
    return {"filename": name, "content_base64": base64.b64encode(data).decode()}


class _HttpClient:
    """Post through a real HTTP server."""

    def __init__(self, base: str) -> None:
        self._base = base.rstrip("/")

    def post(self, path: str, payload: dict, timeout: int = 300) -> dict:
        import urllib.request

        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._base}{path}", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())


def _spawn_server(port: int, timeout_s: float = 120.0):
    """Start the app on a private port and wait until it answers /health.

    Used by the Docker build, where there is no server to talk to. A real
    subprocess is used rather than FastAPI's TestClient because TestClient
    depends on an unpinned ``httpx`` version and breaks when it moves.
    """
    import subprocess
    import sys
    import urllib.error
    import urllib.request

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(BACKEND_ROOT),
    )
    deadline = time.perf_counter() + timeout_s
    url = f"http://127.0.0.1:{port}/health"
    while time.perf_counter() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("backend exited while starting up")
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return proc
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("backend did not become healthy in time")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--data", default=str(BACKEND_ROOT / "demo-shipments"))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--spawn", action="store_true",
                    help="start a private server, load, then stop it "
                         "(for the Docker build)")
    args = ap.parse_args()

    proc = None
    if args.spawn:
        proc = _spawn_server(8123)
        base_url = "http://127.0.0.1:8123"
    else:
        base_url = args.base
    client = _HttpClient(base_url)

    try:
        _load(client, args)
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=30)


def _load(client, args) -> None:
    data_dir = Path(args.data)
    emails = json.loads((data_dir / "emails.json").read_text(encoding="utf-8"))["emails"]
    att_dir = data_dir / "attachments"
    print(f"loading {len(emails)} demo emails")
    for email in emails:
        payload = {
            "email_id": email["email_id"],
            "from": email["from"],
            "subject": email["subject"],
            "body": email["body"],
            "attachments": [
                _attachment_payload(a["filename"], (att_dir / a["filename"]).read_bytes())
                for a in email["attachments"]
            ],
            "metadata": {"from_name": email["from_name"],
                         "received": email["received"]},
            "source": "demo-shipments",
        }
        ingest = client.post("/api/v1/ingest", payload)
        if not ingest.get("persisted"):
            print(f"  {ingest.get('email_id'):12s} NOT PERSISTED "
                  f"(status={ingest.get('status')}) alerts={ingest.get('security_alerts')}")
            continue
        processed = client.post(f"/emails/{ingest['email_id']}/process",
                                {} if not args.force else {"force": True})
        print(f"  {processed.get('email_id', ingest['email_id']):12s} "
              f"status={processed.get('status')!s:14s} "
              f"category={processed.get('category')!s:16s} "
              f"shipment={processed.get('shipment_id')}")
    print("done. Inspect with: GET /shipments/overview")


if __name__ == "__main__":
    main()
