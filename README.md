# BerthSide: Shipping Document Verification System

Averis x Monash Hackathon 2026 entry. BerthSide is an enterprise hub for shipping
operations: email comes in, gets filtered by a source trust matrix, classified,
verified against its documents, and the sender gets an answer back, all in one
system. It removes the weekly grind of hand-checking seven fields between the SI
and the BL across hundreds of emails, where one missed digit holds a shipment
at port.

The rule-based engine achieves a perfect FINAL score of 1.0000 on the official
local self-evaluation CLI (`score_cli.py`) over the full 520-email dataset, and
the same 1.0000 holds under six kinds of meaning-preserving noise.

## What BerthSide does

1. **Filters before ingestion.** Mail from a sender that is not registered in
   the source trust matrix is filtered before it ever enters the pipeline.
   A security gate blocks executables and spoofed attachments before they are
   opened.
2. **Classifies every email.** Five categories (BL_COMPARISON, SI_REQUEST,
   INVOICE_QUERY, GENERAL, SPAM) with provenance badges showing whether the
   verdict came from rules, an LLM, or OCR.
3. **Extracts and compares seven fields** (shipper, consignee, notify party,
   port of loading, port of discharge, container count, gross weight kg)
   between the SI and the BL with a deterministic engine: never an LLM. A
   missing counterpart document is itself flagged as a defect, never skipped.
4. **Escalates anything ambiguous** to a human with the reason and partial
   evidence attached; a person confirms or corrects, and the report updates.
5. **Closes the loop over email.** Every verified case gets a receipt drafted
   automatically and sent as a real threaded email (MIME multipart, RFC 5322
   In-Reply-To/References headers). Rejections get a rejection letter. Delivery
   states are honest: `SENT_SMTP`, `SIMULATED` (recorded as such, never faked),
   `SMTP_FAILED`. Every dispatch is audited.

## Why this matters

Shipping operations teams manually cross-check draft BLs against SIs, field by
field, across hundreds of emails a week. Labels are inconsistent ("Load Port" vs
"POL" vs "Port of Loading"), attachments arrive as txt, PDF, Excel or Word, and
some documents are corrupted, mislabeled, or missing values. This system automates
the triage, the comparison, and the escalation loop, and it always shows its work.

## Try it yourself in five minutes

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

1. Open the hub at `http://localhost:8000/ui/` and go to **Secure and
   Gateway**. The Source Trust Matrix lists every sender allowed into the
   pipeline.
2. **Register the mailbox you will send from**: add it in the console, or set
   `EXTRA_TRUSTED_SOURCES=you@gmail.com` in `.env` and restart. This
   step is not optional decoration; an unregistered sender is quarantined
   before classification, which is exactly the behaviour the system advertises.
3. Send an email **to `averis.demo@gmail.com`** from that mailbox with an SI
   and a draft BL attached (txt, pdf, xlsx, docx all work). The IMAP poller
   stages it in the gateway buffer. No mailbox credentials at hand? Use the
   demo injector in the same console to stage ready-made cases instead.
4. Approve the staged email. **Review and Reply** shows the classification,
   the provenance badge (rule / LLM / OCR), and the seven-field SI vs BL
   ledger with any mismatch flagged.
5. Send the reply back. The sender receives a threaded verification receipt,
   or a rejection letter for mismatches. Without SMTP/Resend/Gmail credentials
   the delivery is recorded as `SIMULATED`, never faked as sent, and every
   dispatch lands in the audit history.
6. For the personal side, open **ShipMail** at `http://localhost:8000/shipmail/`,
   log in with Google OAuth, and read the same threads, attachments and
   verdicts from your own inbox.

## How admission works: the source trust matrix

Nothing unvetted touches the pipeline. Mail from an unregistered source never
reaches the classifier: it is marked `UNTRUSTED_SOURCE` and quarantined in the
gateway buffer, where it can be inspected or disposed of by an operator.

Three admission checks, first match wins:

1. **Registered mailbox.** The sender address appears in the matrix. Display
   names are parsed away first (`Hans <you@gmail.com>` is read as
   `you@gmail.com`), so a pretty From header cannot smuggle an identity past
   the gate.
2. **Forwarded original client.** When an operator forwards mail into the hub,
   the original sender inside the body is what gets checked, so a forwarded
   thread from a verified client stays verified.
3. **Corporate domain.** The sender domain is on the partner whitelist
   (`averis.com` and friends), which covers role mailboxes without listing
   every address.

Where entries live:

| Source | Scope | Survives restart |
|---|---|---|
| `DEFAULT_TRUSTED_SOURCES` in `app/services/trust_matrix.py` | role mailboxes only (hub intake, booking, finance, ...) | yes, in code |
| `EXTRA_TRUSTED_SOURCES` env var | your own demo/testing mailboxes, comma-separated; same behaviour locally and on Render | yes, per environment |
| Hub console (Secure and Gateway) | operator-added sources | yes, persisted in the database |

Personal mailboxes are deliberately **not** committed to the repository
defaults: keep them in your local `.env` or add them at runtime, and the public
repo stays clean. Quarantine is recoverable by a human: if the attachments are
clean, an operator can approve a trust-held email from the console and it
enters the pipeline with a manual clearance. Malware-blocked mail can never be
approved.

## The AI engine: cloud LLM, local Ollama, deterministic rules

The engine is designed so LLM capabilities extend it without rewriting it, and
the fallback chain is cloud LLM to local Ollama to deterministic rules, so an
AI failure is never worse than rules alone:

| `AI_PROVIDER` | Behaviour |
|---|---|
| `rule` | deterministic classifier plus regex extraction, fully offline. Default, and what the 1.0000 score is measured with. |
| `ollama` | a local or remote Ollama endpoint answers first, rules as fallback. No cloud key needed. |
| `cascade` | cloud LLMs first (Gemini, Zhipu, DashScope), then Ollama, then rules. |
| `hybrid` / `remote` | forward to an external AI microservice; `hybrid` falls back to rules on failure. |

Every verdict records which engine answered, and the Review console shows it
as a provenance badge (RULE / LLM / OCR).

**How the Ollama integration works.** `OLLAMA_BASE_URL` is the only thing that
decides where inference runs: `http://localhost:11434` for a local install, a
LAN GPU box, or a public HTTPS endpoint (tunnel / reverse proxy / cloud GPU).
No code change is needed to move between them. `OLLAMA_MODEL` must match the
model name on the host (`ollama list`), and `OLLAMA_API_KEY` is sent as a
Bearer token when an authenticating reverse proxy sits in front.

Cold start is budgeted, not ignored. Three knobs in `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_PROBE_TIMEOUT_SECONDS` | `2` | liveness probe on `/api/tags`, quick so a warm endpoint answers instantly |
| `OLLAMA_COLD_START_TIMEOUT_SECONDS` | `90` | when the probe times out, it retries once on this budget, so a tunnel still waking up is not reported as offline |
| `OLLAMA_REQUEST_TIMEOUT_SECONDS` | `180` | `/api/chat` budget; loading a model into VRAM is paid here, not in the probe |

A probe only escalates to the long budget after a timeout; a 404 or connection
refusal answers immediately. The engine selector in the hub console switches
`AI_PROVIDER` at runtime and the choice persists in the database.

Why keep a local model in the loop at all: it costs nothing in model API
bills, and images (scanned BLs, faxes) route to the vision model exactly the
way text routes to the text model. The extraction ladder itself stays
deterministic: text layer, tables, 300 DPI render plus OCR, and OCR output is
escalated to a human with the transcription attached, never silently compared.

## Architecture

```
   +---------------------------+         +------------------------------------+
   |  ShipMail   /shipmail/    |         |  Centralized hub   /ui/            |
   |  OAuth inbox · compose    | <-----> |  Gateway · Review&Reply ·          |
   |  threads · attachments    | switcher|  Shipment Lifecycle                |
   +-------------+-------------+         +-----------------+------------------+
                 |                       JSON over HTTP     |
                 +-----------+-----------------------------+-----------+
                             v                                         v
              +--------------------------+            +----------------------+
              |  FastAPI (app/main.py)   |            |  BerthSide engine    |
              |  REST API, persistence,  |----------->|  classifier.py       |
              |  trust matrix, SMTP out  |  imports   |  extractor.py        |
              +--------------------------+            |  comparison.py       |
                                                      +----------------------+
```

Three clean layers:

1. **Engine (`app/services/`)**: pure-Python verification core. Takes an inbox,
   returns per-email verdicts. This is where the algorithm lives.
2. **Service (`app/`)**: FastAPI wrapper. Persists verdicts, reviews
   and dispatches in two databases (enterprise hub + ShipMail OAuth), enforces
   the source trust matrix, and runs the SMTP dispatcher.
3. **Frontend (`web/hub/`, `web/shipmail/`)**: dependency-free
   HTML/CSS/JS. Talks to the API only; contains no business logic.

## System surfaces (two systems, one switcher)

BerthSide ships as two systems behind a single workspace switcher, both served
by the same backend:

* **ShipMail (`/shipmail/`)**: the personal side. A real email client with
  single-point OAuth login (Gmail, plus Outlook via PKCE): real message
  threads, real attachments, filters by verdict and attachment type, compose.
* **Centralized hub (`/ui/`)**: the operations side, with three consoles:
  * Secure and Gateway: source trust matrix, per-source ingestion policy,
    security gate, simulate-drop scenarios.
  * Review and Reply (unified): triage, reply queue, failed queue, history,
    provenance badges, human corrections that re-run the comparison.
  * Shipment Lifecycle: shipment view with document versioning, so a third
    revision of a BL is compared against the right counterpart.

Inbound rule: mail from a sender that is not in the source trust matrix is
filtered before it ever enters the system. Nothing unvetted touches the
pipeline. The SMTP reply loop (receipt drafting, threading headers, dispatch
audit) is demonstrated in the hub.

Everything is also a documented REST API at `/docs` (FastAPI Swagger).

## Repository layout

```
app/                FastAPI service: routers, services, models
web/                both consoles: hub/ (gateway / review / lifecycle /
                    outstream) and shipmail/ (ShipMail client UI)
data/
  corpus/           static evaluation corpus: 520 emails with SI/BL attachments
  *.db              local SQLite databases (gitignored)
tests/              pytest suite and UI probes
scripts/            evaluation and demo tooling (tune_eval, stress_evaluate)
docs/               ARCHITECTURE.md, DEVELOPMENT.md, API_CONTRACT.md, history/
requirements.txt    runtime dependencies
Dockerfile          cloud deployment (Render blueprint in render.yaml)
```

Deeper documentation: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (design
rationale), [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) (run, test, score,
deploy), [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md).

## Setup

Requirements: Python 3.11+ (tested on 3.13).

```bash
# 1. create a virtual environment (from the repository root)
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. install dependencies
pip install -r requirements.txt

# 3. run the server
uvicorn app.main:app --reload --port 8000
# ShipMail:  http://localhost:8000/shipmail/
# Hub:       http://localhost:8000/ui/
# API docs:  http://localhost:8000/docs
```

Windows shortcut: double-click `start_server.bat`.

Configuration (optional environment variables, see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL_ENTERPRISE` | `sqlite:///./data/sdoc_enterprise.db` | hub database (corpus, gateway, audits) |
| `DATABASE_URL_OAUTH` | `sqlite:///./data/sdoc_oauth.db` | ShipMail database (operator Gmail + verdicts) |
| `AI_PROVIDER` | `rule` | `rule` / `ollama` / `cascade` / `hybrid` / `remote` |
| `EXTRA_TRUSTED_SOURCES` | *(empty)* | comma-separated mailboxes added to the source trust matrix |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | where Ollama listens: local, LAN, tunnel or cloud GPU |
| `OLLAMA_MODEL` | `qwen2.5vl:7b` | must match the model name on the Ollama host |
| `OLLAMA_PROBE_TIMEOUT_SECONDS` | `2` | liveness probe budget |
| `OLLAMA_COLD_START_TIMEOUT_SECONDS` | `90` | probe retry budget after a timeout |
| `OLLAMA_REQUEST_TIMEOUT_SECONDS` | `180` | `/api/chat` budget (model load happens here) |
| `RESEND_API_KEY` / `SMTP_*` / `IMAP_*` / `GMAIL_*` | *(empty)* | outbound (Resend, Gmail REST API or SMTP) and inbound (IMAP) email channels |

### Docker

Build from the repository root:

```bash
docker build -t berthside .
docker run -p 8000:8000 berthside
```

## API

| Endpoint | Method | Description |
|---|---|---|
| `/api/summary` | GET | dashboard counts by category and status |
| `/api/emails` | GET | list with `category`, `status`, `q`, `limit`, `offset` filters |
| `/api/emails/{id}` | GET | full detail incl. 7-field SI vs BL comparison |
| `/api/emails/{id}/review` | POST | human verdict: `confirm` or `override` |
| `/api/emails/{id}/disposition` | POST | per-email auto/manual reply policy (outstream buffer) |
| `/api/emails/{id}/return` | POST | draft and send the return receipt (`dry_run` to preview) |
| `/api/v1/gateway/*` | GET/POST | quarantine buffer, approve/return staged mail, engine config, trust policies |
| `/api/submission` | GET | merged submission.json (machine + human verdicts) |
| `/api/attachments/{path}` | GET | serve an attachment file (path-traversal safe) |

## The seven compared fields

shipper, consignee, notify party, port of loading, port of discharge,
container count, gross weight (kg). All seven match: OK. Any differ: MISMATCH
with the offending fields listed. Ambiguity (wrong document type, missing
attachment, unreadable file, missing value): NEEDS_REVIEW, routed to the
human review queue.

## Scoring

The official formula: 50% end-to-end accuracy + 30% classification macro-F1 +
20% defect-F1, with NEEDS_REVIEW handling scored separately as reliability.
Current local result via `score_cli.py`: **FINAL 1.0000** (e2e 46/46,
macro-F1 1.000, defect-F1 1.000), and **1.0000 again** after injecting six
kinds of meaning-preserving noise (whitespace, non-breaking spaces, blank
lines, ALL CAPS, collapsed lines, reworded labels). 334 automated tests and
UI probes gate every push; the score has never regressed.

## A note on the evaluation corpus

This repository commits the static evaluation corpus (`data/corpus/`,
520 emails with SI/BL attachments) so the project runs out of the box. The
official hackathon materials (rubric, rules, problem statement) and the scoring
bundle stay outside version control: they are evaluation material that is not
meant to be public.
