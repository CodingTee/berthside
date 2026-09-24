# BerthSide: Development Guide

How to run, verify, test, score and deploy the project. Design rationale lives
in ARCHITECTURE.md.

## Score (official local scorer, 520 emails)

**FINAL 1.0000**: Stage-1 macro-F1 1.000 · Stage-3 field-F1 1.000 ·
End-to-End **46/46**. Reliability axis is diagnostic only (not weighted).

The same 1.0000 holds after injecting six kinds of meaning-preserving noise
(see [Robustness](#robustness-noise-that-must-not-change-the-answer)); that,
not the clean-bundle number, is the result that should transfer to the final
evaluation set.

## Quick start

```bash
cd <repo root>
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements.txt
cp .env.example .env                                # set DATA_SOURCE
uvicorn app.main:app --reload --port 8000
# docs: http://localhost:8000/docs
# Windows shortcut: double-click start_server.bat
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

# 6. human review a flagged case (closed loop: re-runs comparison)
curl -X POST http://localhost:8000/reviews/email_004 \
  -H "Content-Type: application/json" \
  -d '{"decision":"CORRECT","corrected_fields":{"consignee":"UAB NOVAKOPA"},"reviewer":"nicol"}'

# 7. browser: http://localhost:8000/docs  (click any endpoint → Try it out)
```

`.env` essentials:

| Variable | Meaning |
|---|---|
| `DATA_SOURCE` | Folder with `inbox/` + `attachments/` **or** `http://localhost:8080` |
| `DATABASE_URL_ENTERPRISE` | Hub database (520 corpus, gateway, audits): `sqlite:///./sdoc_enterprise.db`, or a Supabase Postgres URL in production |
| `DATABASE_URL_OAUTH` | ShipMail's own database (operator Gmail + its verdicts): `sqlite:///./sdoc_oauth.db` |
| `AI_PROVIDER` | `rule` (offline, default) · `remote` (P3 service) · `hybrid` |
| `AI_SERVICE_URL` | P3's AI base URL, used when provider is `remote`/`hybrid` |

## API

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Liveness + config echo + counts |
| GET | `/emails?limit=&offset=` | Inbox listing with processing status |
| GET | `/emails/{email_id}` | One email (sender, subject, body, attachments) |
| POST | `/emails/{email_id}/process` | Run the pipeline (idempotent: re-run = retry) |
| POST | `/emails/process-all?limit=` | Process the whole inbox (520 emails, ~2 min) |
| POST | `/emails/retry-failed?statuses=ERROR&statuses=NEEDS_REVIEW` | Retry every email without a verdict |
| GET | `/emails/{email_id}/status` | `PENDING`/`PROCESSING`/`COMPLETED`/`NEEDS_HUMAN`/`FAILED` + stage + attempts + error text |
| GET | `/reports?status=&category=&has_defect=` | Filtered report list |
| GET | `/reports/{id_or_email_id}` | Full report with per-field evidence |
| GET | `/reports/summary/stats` | Aggregates: status/category/defect frequency |
| GET | `/reports/submission/json` | Self-evaluation payload (sample_submission shape) |
| POST | `/reviews/{email_id}` | Human decision: `CONFIRM` / `CORRECT` / `REJECT` |
| GET | `/reviews` · `/reviews/{email_id}` | Review history |
| GET | `/ui/` | Reference client, proves Frontend ↔ Backend |

Frozen contract: [`docs/API_CONTRACT.md`](docs/API_CONTRACT.md) ·
Freeze record: [`docs/history/MVP_FREEZE.md`](docs/history/MVP_FREEZE.md) ·
Architecture: [`docs/architecture.svg`](docs/architecture.svg)

### Post a review (closed loop)

```bash
curl -X POST http://localhost:8000/reviews/email_004 \
  -H "Content-Type: application/json" \
  -d '{"decision":"CORRECT","corrected_fields":{"consignee":"UAB NOVAKOPA"},"reviewer":"nicol"}'
```
`CORRECT` re-runs the **deterministic** comparison with the corrected values and
rewrites the report: exactly the "confirm or correct → update the report"
human-in-the-loop the brief asks for.

### Build a submission

```bash
curl http://localhost:8000/reports/submission/json \
  | python -c "import sys,json; d=json.load(sys.stdin); json.dump(d['submission'], open('submission.json','w'), indent=2)"
# with the official dataset server running:
python -c "from loader import Inbox; print(Inbox('http://localhost:8080').submit(json.load(open('submission.json'))))"
```

## Layout

```  (repository root)
├── app/                     # FastAPI package (import root)
│   ├── main.py              # app factory, CORS, static mounts, /health
│   ├── config.py            # env-driven settings, paths anchored to repo root
│   ├── database.py          # engine + session (SQLite/Postgres)
│   ├── models.py            # emails / reports / reviews / shipments tables
│   ├── schemas.py           # Pydantic contracts
│   ├── routers/             # 11 modules: emails · gateway · ingest · gmail ·
│   │                        #   outlook · integration · shipments · reviews ·
│   │                        #   reports · ai_assist · frontend_compat
│   └── services/            # 30 modules, core ones:
│       ├── loader.py        #   official dataset loader (verbatim)
│       ├── classifier.py    #   rule-based email categories
│       ├── extractor.py     #   label-synonym field extraction
│       ├── comparison.py    #   deterministic 7-field comparison
│       ├── workflow.py      #   orchestration + persistence + review
│       ├── versioning.py    #   document versions per shipment
│       ├── llm_gateway.py   #   LLM -> Ollama -> rules fallback chain
│       ├── trust_matrix.py  #   per-source trust gate
│       ├── imap_poller.py   #   mailbox ingestion
│       └── ...              #   format parsers, OCR, dispatch, submission
├── data/                    # corpus/ (dataset) · demo-shipments/ · *.db (runtime)
├── web/                     # hub/ (Master Console) · shipmail/ (demo client)
├── scripts/                 # eval · tuning · smoke · oneoff/
├── tests/                   # pytest suite (334 passed / 3 skipped)
├── docs/                    # ARCHITECTURE · DEVELOPMENT · API_CONTRACT + history/
├── Dockerfile · docker-compose.yml · render.yaml · requirements.txt · .env.example
```

## Regression gates: run before every push

```bash
python -m pytest tests -q          # 334 unit/integration tests (no server needed)
python scripts/tune_eval.py        # score with the official scorer (expect 1.0000)
python scripts/stress_evaluate.py  # score under 6 noise perturbations (expect 1.0)
python scripts/smoke_test.py       # 9 E2E checks against the live API
python scripts/check_secrets.py    # nothing sensitive about to be committed
```

`tests/conftest.py` gives every test a throwaway in-memory database and a
temporary ingest directory, so running the suite never writes to the hub database or
`data/ingested/`. `pytest.ini` pins collection to `tests/`, so a bare `pytest`
is safe as well: `scripts/smoke_test.py` matches pytest's `*_test.py` pattern
but is a live-server script, not a unit test. Keep new tests inside `tests/`,
since that is the only directory the harness covers.

## Scoring / tuning loop (no Docker required)

`scripts/tune_eval.py` runs the full pipeline in-process and grades it with the
organisers' `score_cli.py`, the local equivalent of `POST /submit`. ~10 s for
all 520 emails, so tune against the real metric instead of guessing.

```bash
python scripts/tune_eval.py                    # prints the official scoreboard
python scripts/tune_eval.py --limit 40         # quick pipeline check (do not read its score)
```

Tuning log and open questions: [`docs/history/MVP_FREEZE.md`](docs/history/MVP_FREEZE.md).

### Where the scorer and the ground truth live

Both ship only inside the organisers' Docker bundle, so they are not in this
repo. `scripts/sdoc_paths.py` looks for them in a few conventional places
(upward from the repo root, then `~/Downloads/sdoc-hackathon-docker`), and scripts
ask that module instead of hardcoding a path of their own. If yours sits
somewhere else, say so once and every script picks it up:

```bash
export SDOC_MATERIALS=/path/to/sdoc-hackathon-docker
```

Or override per run with `--scorer <path>`. Passing `--ground-truth` is
normally unnecessary: `score_cli.py` reads `data_v2/ground_truth.json` from its
own folder, and leaving it out is what guarantees the scorer and the answer key
cannot come from two different bundles.

## Diagnostics: measure a rule before changing it

Comparison rules are easy to argue about and impossible to settle by intuition,
so each open question got a measuring script instead of an opinion. All of them
only read; none of them touches source files or the database. They live in
`scripts/oneoff/` because each was written to answer one specific question.

```bash
python scripts/oneoff/diag_reliability.py     # escalation precision/recall + every false flag listed
python scripts/oneoff/diag_port_locode.py     # how often one document prints a UN/LOCODE and the other does not
python scripts/oneoff/diag_port_lenient.py    # A/B both port rules through the official scorer (~2 min, runs the inbox twice)
python scripts/oneoff/diag_filename_fallback.py  # tier-2 naming: corpus diff + the names that must and must not resolve
```

One maintenance script is not a diagnostic, and it deletes rows, so it is dry
run by default:

```bash
python scripts/oneoff/clean_ingest_pollution.py           # show what it would remove
python scripts/oneoff/clean_ingest_pollution.py --apply   # remove it, after backing it up
```

It exists because `scripts/test_ingest_api.py` used to write to the real
database and the real ingest directory when a bare `pytest` collected it. That
file now lives in `tests/` under the isolation harness, so this cleans up the
one leftover row and folder rather than guarding against a live hazard. Both the
database and the folder are copied to `cleanup-backup-<stamp>/` and the
copy is verified before anything is deleted.

`diag_port_lenient.py` (in `scripts/oneoff/`) doubles as the template for any "should we relax rule X?"
question: patch in the alternative predicate, score both variants with the real
scorer, keep whichever wins. It restores the original function in a `finally`
block, so the measured change never leaks into the working tree. This is how the
decision to keep the strict name+LOCODE comparison was made, and it is why that
rule is still the shipped one.

`diag_filename_fallback.py` (also in `scripts/oneoff/`) is the counterpart for "did this change anything?"
questions: it runs the new code and the old code over the whole corpus and
prints the difference, which is the fastest way to show that a fallback path
never fires where it should not.

## Deployment (Phase 5)

1. Supabase Postgres: set `DATABASE_URL_ENTERPRISE` and `DATABASE_URL_OAUTH`, redeploy (tables auto-create).
2. Render: **New Web Service** → Docker → port `8000`; env vars from above.
3. Vercel: frontend only; set `VITE_API_BASE` to the Render URL.
4. `CORS_ORIGINS` on the backend must include the Vercel origin.
5. Never commit `.env`; verify no API keys in git before submission.
