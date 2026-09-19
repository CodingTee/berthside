# SDOC Backend — Shipping Document Verification API

Averis × Monash Hackathon 2026 — **P2: Backend / API / Cloud / Integration**.

Inbox → classification → SI/BL extraction → **deterministic** 7-field comparison
→ discrepancy report → human-in-the-loop review → deployment.

![architecture](docs/architecture.svg)

```
Frontend (P1)  →  FastAPI (P2)  →  Workflow Controller  →  Comparison Engine (deterministic)
                       ↓                                        ↓  ↕  ↕
                  AI Service (P3, hybrid)                  Database ↔ Human Review
```

## Score (official local scorer, 520 emails)

**FINAL 1.0000** — Stage-1 macro-F1 1.000 · Stage-3 field-F1 1.000 ·
End-to-End **46/46**. Reliability axis is diagnostic only (not weighted).

The same 1.0000 holds after injecting six kinds of meaning-preserving noise
(see [Robustness](#robustness-noise-that-must-not-change-the-answer)) — that,
not the clean-bundle number, is the result that should transfer to the final
evaluation set.

## Quick start

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements.txt
cp .env.example .env                                # set DATA_SOURCE
uvicorn app.main:app --reload --port 8000
# docs: http://localhost:8000/docs
# Windows shortcut: double-click start_backend.bat
```

> If pip cannot reach the default index:
> `pip install -r requirements.txt --index-url https://pypi.org/simple --trusted-host pypi.org --trusted-host files.pythonhosted.org`

## Verify it works (one command)

With the server running:

```bash
python scripts/smoke_test.py            # or: python scripts/smoke_test.py http://localhost:8000
```

It checks health → inbox load → process one known email → assert the
**expected** deterministic verdict (`email_004` must be `MISMATCH` on
`consignee` + `notify_party`) → report persistence → human review →
aggregates → submission shape. All 9 checks must PASS.

### Manual walkthrough / demo script

```bash
# 1. is it alive?
curl http://localhost:8000/health

# 2. one email end-to-end (~15 ms)
curl -X POST http://localhost:8000/emails/email_004/process

# 3. read the full report (per-field SI vs BL evidence)
curl http://localhost:8000/reports/email_004

# 4. run the whole inbox (520 emails, ~90 s)
curl -X POST "http://localhost:8000/emails/process-all"

# 5. what did we find?
curl http://localhost:8000/reports/summary/stats

# 6. human review a flagged case (closed loop — re-runs comparison)
curl -X POST http://localhost:8000/reviews/email_004 \
  -H "Content-Type: application/json" \
  -d '{"decision":"CORRECT","corrected_fields":{"consignee":"UAB NOVAKOPA"},"reviewer":"nicol"}'

# 7. browser: http://localhost:8000/docs  (click any endpoint → Try it out)
```

`.env` essentials:

| Variable | Meaning |
|---|---|
| `DATA_SOURCE` | Folder with `inbox/` + `attachments/` **or** `http://localhost:8080` |
| `DATABASE_URL` | `sqlite:///./sdoc.db` (dev) or Supabase Postgres URL (prod) |
| `AI_PROVIDER` | `rule` (offline, default) · `remote` (P3 service) · `hybrid` |
| `AI_SERVICE_URL` | P3's AI base URL, used when provider is `remote`/`hybrid` |

## API

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Liveness + config echo + counts |
| GET | `/emails?limit=&offset=` | Inbox listing with processing status |
| GET | `/emails/{email_id}` | One email (sender, subject, body, attachments) |
| POST | `/emails/{email_id}/process` | Run the pipeline (idempotent — re-run = retry) |
| POST | `/emails/process-all?limit=` | Process the whole inbox (520 emails, ~2 min) |
| POST | `/emails/retry-failed?statuses=ERROR&statuses=NEEDS_REVIEW` | Retry every email without a verdict |
| GET | `/emails/{email_id}/status` | `PENDING`/`PROCESSING`/`COMPLETED`/`NEEDS_HUMAN`/`FAILED` + stage + attempts + error text |
| GET | `/reports?status=&category=&has_defect=` | Filtered report list |
| GET | `/reports/{id_or_email_id}` | Full report with per-field evidence |
| GET | `/reports/summary/stats` | Aggregates: status/category/defect frequency |
| GET | `/reports/submission/json` | Self-evaluation payload (sample_submission shape) |
| POST | `/reviews/{email_id}` | Human decision: `CONFIRM` / `CORRECT` / `REJECT` |
| GET | `/reviews` · `/reviews/{email_id}` | Review history |
| GET | `/ui/` | Reference client — proves Frontend ↔ Backend |

Frozen contract: [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md) ·
Freeze record: [`docs/MVP_FREEZE.md`](docs/MVP_FREEZE.md) ·
Architecture: [`docs/architecture.svg`](docs/architecture.svg)

### Post a review (closed loop)

```bash
curl -X POST http://localhost:8000/reviews/email_004 \
  -H "Content-Type: application/json" \
  -d '{"decision":"CORRECT","corrected_fields":{"consignee":"UAB NOVAKOPA"},"reviewer":"nicol"}'
```
`CORRECT` re-runs the **deterministic** comparison with the corrected values and
rewrites the report — exactly the "confirm or correct → update the report"
human-in-the-loop the brief asks for.

### Build a submission

```bash
curl http://localhost:8000/reports/submission/json \
  | python -c "import sys,json; d=json.load(sys.stdin); json.dump(d['submission'], open('submission.json','w'), indent=2)"
# with the official dataset server running:
python -c "from loader import Inbox; print(Inbox('http://localhost:8080').submit(json.load(open('submission.json'))))"
```

## Determinism — non-negotiable

`app/services/comparison.py` compares normalized values, never asks an LLM:

- `container_count`: parse `6 x 40'HC` → `6`
- `gross_weight_kg`: parse `131,058 KG` → `131058.0`
- ports: UN/LOCODE (`NANTONG, CHINA (CNNTG)`) decides; else normalized name
- names: uppercased, punctuation stripped, legal suffixes removed, address cut

`SI container_count = 3` vs `BL = 4` → `3 != 4` → **MISMATCH**. End of story.

## Messier-input robustness (advanced stage)

The extractor aligns fields by *meaning* via synonym label sets, not exact
headers — so `Load Port` ≡ `Port of Loading`, `Cnee` ≡ `Consignee`, and weight
strings like `22,000 kg` / `22000kgs` / `22.000 KG` all normalize the same.
This is what the advanced stage scores: recognising that two documents express
the same field differently, and telling a real discrepancy from a formatting
difference.

## Robustness — noise that must not change the answer

```bash
python scripts/stress_evaluate.py      # ~30 s, uses the official scorer
```

Injects six meaning-preserving perturbations into the 94 SI/BL text pairs and
re-scores each perturbed submission. No gold answers are read — only the
aggregate official score.

| perturbation | verdict changes | fields lost | score |
|---|---|---|---|
| random whitespace / tabs | 0 | 0 | 1.0 |
| non-breaking spaces (PDF artefact) | 0 | 0 | 1.0 |
| blank line between every line | 0 | 0 | 1.0 |
| ALL-CAPS document | 0 | 0 | 1.0 |
| whole document on one line | 10 | 0 | 1.0 |
| reworded field labels | 0 | 0 | 1.0 |

Four real bugs came out of this harness; none were visible on clean data —
whitespace-variant labels, weight glued to following text (21 577 kg → 21 kg),
a collapsed document losing six of seven fields, and `Notify Party/Intermediate
Consignee` being split as if it were two fields.

Known remaining limitation: when line breaks are lost entirely, company names
absorb the address line. SI and BL degrade symmetrically, so end-to-end is
unaffected.

## Reliability & human review (advanced stage)

- When a document is **unreadable**, a value is **missing**, or the result is
  **uncertain**, the case is escalated to `NEEDS_REVIEW` **with the reason and
  the partial evidence** (which attachment is missing, what was extracted) — a
  person confirms or corrects it, and the report is updated.
- **Visible failures:** AI-service outages / timeouts are caught, surfaced as a
  clear status + error text, and retried with exponential backoff (never silent).
- `POST /emails/{id}/process` is idempotent — calling it again is the retry.
- **Review queue grouped by reason:** `GET /api/review-queue` buckets
  escalations by `review_reason`; the ops board at `/ops/` renders them.

### Scanned documents: OCR reads them, humans confirm them

Of the 58 non-txt documents, 8 (emails 511–515) have **no embedded text
layer** — they literally say `SCANNED COPY - NO TEXT LAYER`. Two are also
truncated, so nothing can recover those; the rest are read by OCR.

The PDF reader is an escalating ladder — each rung runs only when the previous
one recovers too few of the 7 compared fields:

| Rung | Reader | Handles |
|---|---|---|
| 1 | `pypdf` text layer | machine-generated PDFs (exact, fast) |
| 2 | `pdfplumber` tables/layout | PDFs whose content is a **table** (pypdf scrambles cell order) |
| 3 | `pypdfium2` + OCR | **scanned / image-only** PDFs |
| — | escalate `unreadable` | truncated / genuinely unrecoverable files |

**OCR output is not trusted blindly.** Transcription errors (`VALPARAISO` →
`VALPARAISQ`, `CHINA` → `CHIMA`) look exactly like real discrepancies. So an
OCR-derived document that is missing any compared field is escalated with the
transcription attached as evidence, rather than silently compared:

```
scanned PDF → pypdf yields nothing → OCR transcribes → fields incomplete
            → NEEDS_REVIEW (evidence: OCR text + which fields are unreadable)
```

Every reader is optional. If `pdfplumber` / the OCR engine are not installed
the ladder degrades to rung 1 and the document escalates as `unreadable` —
never a wrong answer.

## Statuses

| Status | Meaning |
|---|---|
| `OK` | BL_COMPARISON, all 7 fields match |
| `MISMATCH` | ≥1 field differs → `has_defect`, `defect_fields` |
| `NEEDS_REVIEW` | `missing_attachment` · `unreadable` · `missing_value` · `wrong_doc_type` (with evidence) |
| `SKIPPED` | Not a comparison request — classification only |
| `ERROR` | Pipeline exception; message stored, re-`POST` to retry |

## Layout

```
backend/
├── app/
│   ├── main.py              # FastAPI app, CORS, startup, /health, /ui
│   ├── config.py            # env-driven settings
│   ├── database.py          # engine + session (SQLite/Postgres)
│   ├── models.py            # emails / reports / reviews tables
│   ├── schemas.py           # Pydantic contract for P1 + P3
│   ├── routers/             # emails.py · reports.py · reviews.py
│   └── services/
│       ├── loader.py        # official dataset loader (verbatim)
│       ├── inbox_service.py # cached inbox access
│       ├── classifier.py    # rule-based email categories
│       ├── extractor.py     # label-synonym field extraction (robust)
│       ├── comparison.py    # deterministic 7-field comparison
│       ├── ai_service.py    # P3 integration (rule / remote / hybrid + backoff)
│       └── workflow.py      # orchestration + persistence + review
├── docs/                   # API_CONTRACT · MVP_FREEZE · architecture.svg
├── Dockerfile · docker-compose.yml · requirements.txt · .env.example
```

## Regression gates — run before every push

```bash
python -m pytest tests -q          # 38 unit/integration tests (no server needed)
python scripts/tune_eval.py        # score with the official scorer (expect 1.0000)
python scripts/stress_evaluate.py  # score under 6 noise perturbations (expect 1.0)
python scripts/smoke_test.py       # 9 E2E checks against the live API
python scripts/check_secrets.py    # nothing sensitive about to be committed
```

## Scoring / tuning loop (no Docker required)

`scripts/tune_eval.py` runs the full pipeline in-process and grades it with the
organisers' `score_cli.py` — the local equivalent of `POST /submit`. ~10 s for
all 520 emails, so tune against the real metric instead of guessing.

```bash
python scripts/tune_eval.py                    # prints the official scoreboard
python scripts/tune_eval.py --limit 40         # quick pipeline check (do not read its score)
```

Tuning log and open questions: [`docs/MVP_FREEZE.md`](docs/MVP_FREEZE.md).

## P3 (AI) contract

Set `AI_PROVIDER=hybrid` and point `AI_SERVICE_URL` at P3:

```
POST {AI}/classify   {"email": {...}}                     -> {"category": "...", "confidence": 0.9}
POST {AI}/extract    {"doc_type":"SI","filename":"...","content_base64":"..."}
                     -> {"fields": {7 keys}, "readable": true}
```

Before P3 exists, run the simulator to prove the wiring end to end:

```bash
python scripts/mock_ai_service.py --port 8001      # terminal 1
AI_PROVIDER=hybrid AI_SERVICE_URL=http://127.0.0.1:8001 uvicorn app.main:app  # terminal 2
```

Hybrid guarantees no regression: if the AI service is down **or** returns
unreadable/incomplete fields for a PDF/Word/Excel file, the local parser
backfills, so the hybrid path is never worse than the rule path.

## Deployment (Phase 5)

1. Supabase Postgres → set `DATABASE_URL`, redeploy (tables auto-create).
2. Render: **New Web Service** → Docker → port `8000`; env vars from above.
3. Vercel: frontend only; set `VITE_API_BASE` to the Render URL.
4. `CORS_ORIGINS` on the backend must include the Vercel origin.
5. Never commit `.env`; verify no API keys in git before submission.
