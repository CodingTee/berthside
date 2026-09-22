# ShipSync: Shipping Document Verification System

Averis x Monash Hackathon 2026 entry. ShipSync is an enterprise hub for shipping
operations: email comes in, gets filtered by a source trust matrix, classified,
verified against its documents, and the sender gets an answer back, all in one
system. It removes the weekly grind of hand-checking seven fields between the SI
and the BL across hundreds of emails, where one missed digit holds a shipment
at port.

The rule-based engine achieves a perfect FINAL score of 1.0000 on the official
local self-evaluation CLI (`score_cli.py`) over the full 520-email dataset, and
the same 1.0000 holds under six kinds of meaning-preserving noise.

## What ShipSync does

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
5. **Closes the loop over SMTP.** Every verified case gets a receipt drafted
   automatically and sent as a real threaded email (MIME multipart, RFC 5322
   In-Reply-To/References headers). Rejections get a rejection letter. Delivery
   states are honest: `SENT_SMTP`, `SIMULATED` (recorded as such, never faked),
   `SMTP_FAILED`. Every dispatch is audited.

## Team and ownership

| Member | Role | Where to work | Current tasks |
|---|---|---|---|
| **Loong** | Backend / Deployment | `backend/Dockerfile`, cloud platform config | Deploy ShipSync to a cloud platform (Render / Railway / HF Spaces) and get the public prototype link. The Dockerfile and `requirements.txt` are ready; this is a mandatory deliverable. |
| **Quiab** | Backend / API | `backend/app/`, `sdoc/engine.py` | Extend the FastAPI endpoints if the frontend needs more data, improve the human review flow (confirm / override), keep the submission export correct. |
| **Tee** | Algorithm | `sdoc/classifier.py`, `sdoc/extractor.py`, `sdoc/comparator.py` | LLM fallbacks (see "Where AI plugs in"), robustness tests |
| **Wang** | Frontend | `backend/web/`, `backend/shipmail/` | UI polish, screenshots for slides |
| **Hioman** | Floater | Anywhere help is needed | Pick an open task from any row, coordinate first |

Ground rules:

1. Work on your own branch (`backend/...`, `frontend/...`, `algorithm/...`),
   merge into `main` via pull request.
2. The `sdoc/` engine currently scores FINAL 1.0000 on the official local
   evaluator. Before changing anything inside it, run
   `python pipeline/pipeline.py --data sdoc-hackathon-bundle` and compare the
   result with `score_cli.py`. If the score drops, the change does not go in.
3. Not sure where a change belongs? Ask in the group chat before editing.

## Why this matters

Shipping operations teams manually cross-check draft BLs against SIs, field by
field, across hundreds of emails a week. Labels are inconsistent ("Load Port" vs
"POL" vs "Port of Loading"), attachments arrive as txt, PDF, Excel or Word, and
some documents are corrupted, mislabeled, or missing values. This system automates
the triage, the comparison, and the escalation loop, and it always shows its work.

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
              |  FastAPI (app/main.py)   |            |  sdoc engine         |
              |  REST API, persistence,  |----------->|  classifier.py       |
              |  trust matrix, SMTP out  |  imports   |  extractor.py        |
              +--------------------------+            |  comparison.py       |
                                                      +----------------------+
```

Three clean layers:

1. **Engine (`sdoc/`)**: pure Python, zero web dependencies. Takes an inbox,
   returns per-email verdicts. This is where the algorithm lives.
2. **Service (`backend/app/`)**: FastAPI wrapper. Persists verdicts, reviews
   and dispatches in two databases (enterprise hub + ShipMail OAuth), enforces
   the source trust matrix, and runs the SMTP dispatcher.
3. **Frontend (`backend/web/`, `backend/shipmail/`)**: dependency-free
   HTML/CSS/JS. Talks to the API only; contains no business logic.

## System surfaces (two systems, one switcher)

ShipSync ships as two systems behind a single workspace switcher, both served
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


## Repository layout (and who owns what)

```
sdoc/
  __init__.py        exports Engine                       [AI + backend]
  classifier.py      email classification rule chain      [AI/algorithm, backend]
  extractor.py       txt/pdf/xlsx/docx field extraction   [AI/algorithm, backend]
  comparator.py      field comparison + defect flags      [AI/algorithm, backend]
  engine.py          orchestration, review overlay        [backend]

backend/
  app/               FastAPI service: routers, services, models
  web/               hub consoles UI (gateway / review / lifecycle)
  shipmail/          ShipMail client UI
  requirements.txt   runtime dependencies                 [backend]
  Dockerfile         cloud deployment                     [devops]

pipeline/
  pipeline.py        CLI: run engine over dataset,        [backend]
                     write submission.json

sdoc-hackathon-bundle/   official dataset (provided by organizers, committed
                         for convenience; contains no answers)
```

### Work assignment map

| Area | Files | Owner | Current state | Next steps |
|---|---|---|---|---|
| Frontend | `backend/web/`, `backend/shipmail/` | Wang | Working: hub consoles (gateway / review / lifecycle), ShipMail client with OAuth, filters, drafts | Polish UX, screenshots for slides |
| Backend / API | `backend/app/` | Quiab | Working: REST API, review persistence, submission export, trust matrix, SMTP dispatch | Extend endpoints on frontend request, improve review flow |
| Deployment | `backend/Dockerfile`, `backend/requirements.txt` | Loong | Dockerfile ready, not yet deployed | Push to a cloud platform, verify the public link works, keep it up during judging |
| Algorithm (deterministic) | `sdoc/classifier.py`, `sdoc/extractor.py`, `sdoc/comparator.py` | Tee | Working: full score on local eval | Robustness testing on regenerated datasets |
| AI integration | hooks already exist, see below | Tee (owner), Hioman (support) | Plugs into existing slots | LLM fallbacks (see "Where AI plugs in") |
| Docs / video | README, slides, demo video | Hioman | TODO | 5 min video, slide deck, submission form |

## Where AI plugs in

The engine was designed so that LLM capabilities extend it without rewriting it,
and the fallback chain is cloud LLM to local Ollama to rules, so the hybrid
path is never worse than rules alone:

1. **Classification**: the rule chain decides first; an LLM (cloud or local
   Ollama) can be selected as the triage engine in the gateway, and every
   verdict records its provenance (rule, LLM, or OCR) as a badge.
2. **Extraction**: scanned or image-only PDFs route through the PDF ladder
   (text layer, tables, 300 DPI render plus OCR). OCR output is escalated to
   a human with the transcription attached, never silently compared.
3. **Ollama runs locally**: keeping a model in the loop costs nothing in
   model API bills.

## Setup

Requirements: Python 3.11+ (tested on 3.13).

```bash
# 1. create a virtual environment
cd backend
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. install dependencies
pip install -r requirements.txt

# 3. run the backend
uvicorn app.main:app --reload --port 8000
# ShipMail:  http://localhost:8000/shipmail/
# Hub:       http://localhost:8000/ui/
# API docs:  http://localhost:8000/docs
```

Configuration (optional environment variables, see `backend/.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL_ENTERPRISE` | `sqlite:///./sdoc_enterprise.db` | hub database (corpus, gateway, audits) |
| `DATABASE_URL_OAUTH` | `sqlite:///./sdoc_oauth.db` | ShipMail database (operator Gmail + verdicts) |
| `AI_PROVIDER` | `rule` | `rule` (offline) / `ollama` / `remote` / `hybrid` |

### Docker

Build from the repository root:

```bash
docker build -f backend/Dockerfile -t shipsync .
docker run -p 8000:8000 shipsync
```

## API

| Endpoint | Method | Description |
|---|---|---|
| `/api/summary` | GET | dashboard counts by category and status |
| `/api/emails` | GET | list with `category`, `status`, `q`, `limit`, `offset` filters |
| `/api/emails/{id}` | GET | full detail incl. 7-field SI vs BL comparison |
| `/api/emails/{id}/review` | POST | human verdict: `confirm` or `override` |
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
lines, ALL CAPS, collapsed lines, reworded labels). 322 automated tests and
UI probes gate every push; the score has never regressed.

## Important note on the dataset

This repository commits the static evaluation bundle (`sdoc-hackathon-bundle/`)
so the project runs out of the box. The docker variant of the dataset
(`sdoc-hackathon-docker/`) is intentionally excluded from version control via
`.gitignore` and must never be committed: it contains evaluation material that
is not meant to be public.
