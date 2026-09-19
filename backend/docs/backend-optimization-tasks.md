# Backend optimization tasks

Two backend tasks, no frontend work involved. Pick either one — they don't overlap.

---

## Task 1 — Cold start is too slow (it can break the live demo)

**Priority: high · Risk: low · Owner: Loong (deploy) or anyone comfortable with Docker**

### What happens now

When the API container boots, `app/main.py` runs `workflow.process_all()` over the
whole inbox — **520 emails**. The handful of scanned PDFs go through the OCR rung,
which is much slower than plain text extraction. On Render's free tier the service
sleeps after ~15 minutes of inactivity, so every cold start pays this cost again:
tens of seconds, sometimes long enough that Render's health check times out and the
page never loads.

### Why it matters

This is not a scoring problem — the engine is already at **FINAL 1.0000**. It is a
*demo* problem: if the judges open the URL and it hangs or 502s, the whole
human-in-the-loop UI never gets seen.

### Fix

Bake the processed results into the image at build time, so the container starts
with a populated database instead of computing it on boot:

- In `backend/Dockerfile`, after the dataset is staged, run the pipeline once
  (e.g. `RUN python scripts/run_pipeline.py` or a small inline script that calls
  `workflow.process_all`) and keep the resulting SQLite file in the image.
- Keep the existing guard in `app/main.py` — it only processes when
  `ReportRecord` count is `0`, so a pre-built DB means startup is instant.
- Alternative (better for persistence): point `DATABASE_URL` at a Supabase /
  Postgres instance so results survive restarts. SQLite on Render's free tier is
  ephemeral — the DB is wiped on every redeploy.

### Acceptance criteria

- Container reaches `/health` in a few seconds from a cold start.
- `/api/summary` returns the full 520 emails immediately after boot.
- `python scripts/tune_eval.py` still reports **1.0000**.

---

## Task 2 — Company names swallow the address line (the one real generalization gap)

**Priority: medium · Risk: medium (this is the scoring engine) · Owner: backend dev**

### What happens now

Under the `collapse_lines` perturbation in `scripts/stress_evaluate.py`
(all line breaks removed, the whole document becomes one line), **10 emails change
verdict**. The score stays at 1.0000 only because SI and BL degrade *symmetrically* —
both sides lose the same way, so the comparison still agrees. On a differently
formatted evaluation set that symmetry is not guaranteed.

Measured impact of that perturbation: **307 of 658 extracted fields differ** from the
clean-document extraction.

### Root cause

When line breaks disappear, the parser cannot tell where a company name ends and the
address begins, so the company name absorbs the address text:

```
SHIPPER: ACME PAPER LTD 12 INDUSTRIAL ROAD SINGAPORE 639798
         ^^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
         company name    should be a separate address value
```

### Fix

Add a boundary heuristic in `app/services/extractor.py` (`_clean_name` / the
value-capture logic):

- **Company-name terminators** — cut after `LTD`, `LIMITED`, `INC`, `LLC`,
  `CO.,`, `CO.,LTD`, `GMBH`, `PTE`, `PTY`, `CORP`, `CORPORATION`, `SDN BHD`,
  `TRADING`, `INTERNATIONAL` when followed by more text.
- **Address starters** — treat `ROAD`, `RD`, `STREET`, `ST`, `FLOOR`, `FL`, `NO.`,
  `UNIT`, `BLK`, `BLOCK`, `BUILDING`, `JALAN`, `AVENUE`, `AVE`, `SUITE`, `ROOM`,
  a bare long digit run (postcode), or a city/country name as the beginning of a new
  value.
- Keep it conservative: only split when both sides are non-trivial (e.g. address
  part has ≥ 2 tokens), so short names never get truncated.

### Acceptance criteria — all three gates must pass

```
python scripts/tune_eval.py        # must stay at FINAL 1.0000
python scripts/stress_evaluate.py  # collapse_lines verdict changes should drop toward 0
python -m pytest -q                # 46 tests, all pass
```

If `tune_eval.py` drops below 1.0000, **revert the change** — the clean-set score is
sacred; the perturbation result is only a robustness signal.

### Notes

- Do not touch `backend/web/` — that is the frontend teammate's area.
- `backend/Dockerfile`, `docker-compose.yml`, `requirements.txt`, `.env.example`,
  `render.yaml` and `.dockerignore` are shared/deploy files owned by Loong. If you
  need one of those changed, ask first.
