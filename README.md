# SDOC: Shipping Document Verification System

Averis x Monash Hackathon 2026 entry. An automated pipeline that reads a shipping
operations inbox, classifies every email, extracts freight fields from Shipping
Instruction (SI) and Bill of Lading (BL) attachments, compares them field by field,
and escalates anything ambiguous to a human reviewer through a web-based Review Desk.

The rule-based engine achieves a perfect score (1.000) on the official local
self-evaluation CLI (`score_cli.py`) over the full 520-email dataset.

## Team and ownership

| Member | Role | Where to work | Current tasks |
|---|---|---|---|
| **Loong** | Backend / Deployment | `app/Dockerfile`, cloud platform config, `pipeline/` | Deploy the Review Desk to a cloud platform (Render / Railway / HF Spaces) and get the public prototype link. The Dockerfile and `requirements.txt` are ready; this is a mandatory deliverable. |
| **Quiab** | Backend / API | `app/main.py`, `sdoc/engine.py` | Extend the FastAPI endpoints if the frontend needs more data, improve the human review flow (confirm / override), keep the submission export correct. |
| **Tee** | Algorithm | `sdoc/classifier.py`, `sdoc/extractor.py`, `sdoc/comparator.py` | LLM fallbacks (see "Where AI plugs in"), robustness tests |
| **Wang** | Frontend | `app/static/index.html` | UI polish, attachment viewer, screenshots for slides |
| **Hioman** | Floater | Anywhere help is needed | Pick an open task from any row, coordinate first |

Ground rules:

1. Work on your own branch (`backend/...`, `frontend/...`, `algorithm/...`),
   merge into `main` via pull request.
2. The `sdoc/` engine currently scores 1.000 on the official local evaluator.
   Before changing anything inside it, run
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
                        +---------------------------------------------+
                        |                Review Desk (web)            |
                        |   inbox list / SI vs BL diff / review form  |
                        +--------------------------^------------------+
                                                   | JSON over HTTP
                        +--------------------------v------------------+
                        |           FastAPI app (app/main.py)         |
                        |   summary, list, detail, review, submit     |
                        +--------------------------+------------------+
                                                   | python imports
    +------------------+                +----------v-----------+
    |  Email inbox     |                |   sdoc engine        |
    |  (JSON + files)  |--------------->|  classifier.py       |
    +------------------+                |  extractor.py        |
                                        |  comparator.py       |
                                        |  engine.py           |
                                        +----------------------+
```

Three clean layers:

1. **Engine (`sdoc/`)**: pure Python, zero web dependencies. Takes an inbox
   directory, returns per-email verdicts. This is where the algorithm lives.
2. **Service (`app/main.py`)**: thin FastAPI wrapper. Caches engine results in
   memory at startup, serves them as JSON, and persists human review decisions.
3. **Frontend (`app/static/index.html`)**: dependency-free HTML/CSS/JS single
   page. Talks to the API only; contains no business logic.

## System surfaces (ShipMail + Dashboard)

The product now has three user-facing surfaces, all served by the **same
single backend** (one Render service, no extra split):

```
              ┌────────────────────────────────────────────┐
              │  ShipMail                 /shipmail/        │
              │  inbox · email detail · attachments        │
              │  + ShipSync side panel                     │
              │  (static demo data in                      │
              │   backend/shipmail/data/)                  │
              └───────────────────┬────────────────────────┘
                                  │ ShipSync logo → opens in NEW tab
              ┌───────────────────v────────────────────────┐
              │  ShipSync Dashboard     /ui/               │
              │  operations console (existing web app)     │
              └───────────────────┬────────────────────────┘
                                  │
              ┌───────────────────v────────────────────────┐
              │              ShipSync API                  │
              │  GET  /api/health                          │
              │  POST /api/process      (cached, deduped)  │
              │  GET  /api/results      (stored only)      │
              │  GET  /api/results/{key}                    │
              └───────────────────┬────────────────────────┘
                                  │
                       Existing ShipSync core (unchanged)
              classification → extraction → verification
```

Key behaviours (implemented in `backend/app/routers/integration.py` and
`backend/app/services/result_cache.py`):

* **Process once, store, reuse.** `POST /api/process` deduplicates on
  `email_id` / `message_id` / `thread_id` plus a content hash, stores the
  result in the `process_results` table, and returns the stored result on
  every repeat call (`"cached": true`). Only `force: true` (explicit
  re-process) or actually-changed content runs the core again.
* **Reading never processes.** `GET /api/results` and `GET /api/results/{key}`
  only return stored results. Opening the inbox, refreshing, switching emails
  or reopening the Dashboard therefore costs no OCR / classification /
  extraction / verification.
* **No polling.** Neither frontend contains a `setInterval` or background
  refresh loop. API calls happen only on explicit user actions.
* **Static ShipMail data.** The demo inbox lives in
  `backend/shipmail/data/` (`emails.json`, `shipments.json`,
  `attachments/`) and is served as plain files — opening the inbox never
  touches the backend.
* **Side panel and Dashboard share results.** The Dashboard's *Integrations*
  panel and the ShipMail side panel read the same `process_results` rows.
* **Real Gmail** stays a separate integration (`/api/gmail/*`, OAuth — no
  username/password) feeding the same API and core.


## Repository layout (and who owns what)

```
sdoc/
  __init__.py        exports Engine                       [AI + backend]
  classifier.py      email classification rule chain      [AI/algorithm, backend]
  extractor.py       txt/pdf/xlsx/docx field extraction   [AI/algorithm, backend]
  comparator.py      field comparison + defect flags      [AI/algorithm, backend]
  engine.py          orchestration, review overlay        [backend]

app/
  main.py            FastAPI endpoints                    [backend]
  static/index.html  Review Desk UI                       [frontend]
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
| Frontend | `app/static/index.html` | Wang | Working: stats cards, filterable inbox, SI vs BL diff table, review form | Polish UX, add attachment viewer, screenshots for slides |
| Backend / API | `app/main.py`, `sdoc/engine.py` | Quiab | Working: 6 endpoints, review persistence, submission export | Extend endpoints on frontend request, improve review flow |
| Deployment | `app/Dockerfile`, `app/requirements.txt` | Loong | Dockerfile ready, not yet deployed | Push to a cloud platform, verify the public link works, keep it up during judging |
| Algorithm (deterministic) | `sdoc/classifier.py`, `sdoc/extractor.py`, `sdoc/comparator.py` | Tee | Working: full score on local eval | Robustness testing on regenerated datasets |
| AI integration | hooks already exist, see below | Tee (owner), Hioman (support) | Plugs into existing slots | LLM fallbacks (see "Where AI plugs in") |
| Docs / video | README, slides, demo video | Hioman | TODO | 5 min video, slide deck, submission form |

## Where AI plugs in

The engine was designed so that LLM capabilities extend it without rewriting it:

1. **Classification fallback** (`sdoc/classifier.py`): the classifier is an
   ordered rule chain and the `Classification.decided_by` field already supports
   `"rule" | "llm" | "human"`. An LLM arbiter can be appended after the
   deterministic rules to handle ambiguous emails; the orchestrator needs no
   changes. This also feeds the hackathon requirement that the solution
   incorporates AI technology as a key component.
2. **Extraction fallback** (`sdoc/extractor.py`): scanned or image-only PDFs
   currently route to NEEDS_REVIEW. A vision/OCR model can attempt extraction
   first and only escalate when confidence is low.
3. **Review assist** (`sdoc/engine.py`): for NEEDS_REVIEW emails, an LLM can
   draft the reviewer note (what is wrong, what to check) shown in the Review
   Desk form, so the human confirms instead of investigating from scratch.

## Setup

Requirements: Python 3.11+ (tested on 3.13).

```bash
# 1. create a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. install dependencies
pip install -r app/requirements.txt

# 3. dataset: sdoc-hackathon-bundle/ is already in the repo root.
#    If you removed it, download the official bundle and place it at the root.

# 4a. batch mode: produce submission.json
python pipeline/pipeline.py --data sdoc-hackathon-bundle

# 4b. web mode: run the Review Desk
cd app
uvicorn main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

Configuration (optional environment variables):

| Variable | Default | Purpose |
|---|---|---|
| `SDOC_DATA_ROOT` | `../sdoc-hackathon-bundle` | inbox dataset location |
| `SDOC_REVIEWS` | `app/data/reviews.json` | human review persistence |
| `PORT` | `8000` | server port |

### Docker

Build from the repository root (the Dockerfile needs both `sdoc/` and `app/`):

```bash
docker build -f app/Dockerfile -t sdoc-desk .
docker run -p 8000:8000 \
  -v /path/to/sdoc-hackathon-bundle:/srv/data:ro \
  sdoc-desk
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
Current local result via `score_cli.py`: **1.000**.

## Important note on the dataset

This repository commits the static evaluation bundle (`sdoc-hackathon-bundle/`)
so the project runs out of the box. The docker variant of the dataset
(`sdoc-hackathon-docker/`) is intentionally excluded from version control via
`.gitignore` and must never be committed: it contains evaluation material that
is not meant to be public.
