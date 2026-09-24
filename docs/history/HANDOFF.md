# Handoff — P1 (frontend) & P3 (AI)

Everything below is live right now at **http://127.0.0.1:8000**.
Read this first; it should be enough to connect without asking questions.

- Interactive API docs: **http://127.0.0.1:8000/docs**
- Working reference UI (view source to copy calls): **http://127.0.0.1:8000/ui/**
- Full contract: [`API_CONTRACT.md`](API_CONTRACT.md)

---

## P1 — Frontend (deadline: tonight, 19 Sep)

### 1. Point your app at the backend

```js
const API = "http://127.0.0.1:8000";   // dev
// production later: https://<render-service>.onrender.com
```

CORS is fully open in development — no proxy needed.

### 2. The four calls you need

```js
// a) inbox list (add ?limit=&offset= for paging)
const inbox = await (await fetch(`${API}/emails?limit=50`)).json();
// → { total, limit, offset, emails: [{ email_id, from, subject, attachments,
//                                      has_attachments, processed, status }] }

// b) run the pipeline for one email (idempotent — safe to call twice)
const res = await fetch(`${API}/emails/${id}/process`, { method: "POST" });
const verdict = await res.json();
// → { email_id, category, status, has_defect, defect_fields, review_reason,
//     field_results: [{ field, si_value, bl_value, match }], processing_ms }

// c) full report for display
const report = await (await fetch(`${API}/reports/${id}`)).json();

// d) human review (your "confirm / correct" buttons)
await fetch(`${API}/reviews/${id}`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ decision: "CONFIRM", reviewer: "operator" }),
});
```

### 3. How to render the result

`status` is one of `OK` · `MISMATCH` · `NEEDS_REVIEW` · `SKIPPED` · `ERROR`.

- `MISMATCH` → show `field_results` as a **two-column SI vs BL table**, highlight
  rows where `match === false`.
- `NEEDS_REVIEW` → show `review_reason` and a human-decision form.
- `SKIPPED` → classification only; `category` still matters, no table.

Optional but nice: `GET /emails/{id}/status` gives `PENDING | COMPLETED |
NEEDS_HUMAN | FAILED` plus a human-readable `stage` (e.g.
`"escalated: missing_attachment"`, `"completed (human-reviewed)"`) and
`attempts`/`error_message` for progress + visible-failure indicators, and
`GET /reports/summary/stats` gives counts for a dashboard.

### 4. Please don't

- Don't call `/emails/process-all` from the UI on load — it takes ~90 s.
- Don't invent field names; the seven compared fields are fixed:
  `shipper, consignee, notify_party, port_of_loading, port_of_discharge,
  container_count, gross_weight_kg`.

---

## P3 — AI service

Your service plugs in behind `AI_SERVICE_URL`. Until it exists the backend runs
its own deterministic rule engine, so nothing is blocked on you.

### Contract (two endpoints)

```
POST {YOUR_URL}/classify
  in : { "email": { email_id, from, subject, body, attachments } }
  out: { "category": "BL_COMPARISON|SI_REQUEST|INVOICE_QUERY|GENERAL|SPAM",
         "confidence": 0.0-1.0 }

POST {YOUR_URL}/extract
  in : { "doc_type": "SI" | "BL",
         "filename": "email_004_SI.txt",
         "content_base64": "<base64 bytes>" }
  out: { "fields": {
           "shipper": "...", "consignee": "...", "notify_party": "...",
           "port_of_loading": "...", "port_of_discharge": "...",
           "container_count": 6, "gross_weight_kg": 131058.0
         },
         "readable": true }
```

Rules:
- Return **only** fields you actually found — omit unknowns rather than guessing.
  Missing values escalate for human review instead of becoming fake matches.
- `readable: false` for anything you cannot parse (scanned PDFs etc.).
- Values: `container_count` integer, `gross_weight_kg` number in kilograms,
  ports as printed (normalisation is the backend's job).

### Try it without writing a server

```bash
python scripts/mock_ai_service.py --port 8001     # reference implementation
```

### Switching the backend over to you

```bash
AI_PROVIDER=hybrid AI_SERVICE_URL=http://127.0.0.1:8001 \
  uvicorn app.main:app --port 8000
```

`hybrid` means: your answer wins; if your service is down **or** returns
unreadable/incomplete fields for a file, the local parser backfills — so the
demo survives a failure on either side.

### Important

The comparison itself stays **deterministic** in the backend
(`app/services/comparison.py`). Your service proposes values; it never decides
"is this a mismatch". That is a hard requirement from the problem statement.

---

## Current state (19 Sep morning)

| Metric | Value |
|---|---|
| Official score (local scorer) | **0.9044** |
| End-to-end defect catch | 45 / 46 |
| Field-level F1 | 0.993 |
| Emails processed | 520 / 520, 0 errors |
