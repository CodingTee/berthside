# ShipSync P0/P1 Fixes — Final Report

**No architecture was rebuilt.** The Dashboard layout, navigation and styling are
untouched. The simulated/demo workflow is intact. Every change below is the
smallest one that makes the identified behaviour correct.

---

## A. Files changed

| File | Kind |
|---|---|
| `app/services/doc_types.py` | **new** — one canonical document-kind detector |
| `app/services/workflow.py` | edited — real attachments outrank synthetic ones |
| `app/services/versioning.py` | edited — chronological versioning, authoritative status |
| `app/integrations/gmail/sync.py` | edited — batch ordering, attachment priority, received_at |
| `app/integrations/gmail/parser.py` | edited — `Date` header → received timestamp |
| `app/routers/frontend_compat.py` | edited — `GET /api/emails/{id}` is read-only |
| `app/routers/shipments.py` | edited — one status in the payload |
| `app/schemas.py` | edited — `resolution_state` split out |
| `web/index.html` | edited — guard for the new read-only response + explicit process action |
| `scripts/audit_gmail_versions.py` | rewritten — acceptance tests A–G with PASS/FAIL |

Not touched: `shipment_overview.py` (it was already the document-aware owner),
`app/routers/ingest.py`, the ShipMail UI, any Dashboard layout/navigation/CSS.

## B. What changed

**`app/services/doc_types.py` (new).** The Gmail layer and the engine each had
their own idea of what an SI is. The Gmail layer demanded `_SI.` immediately
before the extension; the engine accepted fuzzy names. This module holds the
engine's rules once — `detect_si_bl`, `is_invoice`, `detect`, `is_synthetic` —
and both layers call it. Deliberately unchanged: an ambiguous name
(`SI_and_BL.pdf`) still resolves to `None`, and a non-document suffix is still
not a document.

**`app/services/workflow.py`.**
- The duplicated `_DOC_SUFFIXES` / `_FUZZY_TOKENS` / `_FUZZY_PHRASES` tables are
  gone; they now come from `doc_types` (the old `_fuzzy_doc_type` name is kept as
  a thin alias so nothing else breaks). `_INVOICE_NAME_RE` is `doc_types.INVOICE_RE`.
- `_find_doc_attachments` considers **real attachments before synthetic ones**,
  so a body-derived file can fill a genuine gap but can never displace the PDF
  the sender actually attached.

**`app/services/versioning.py`.**
- `register_document_version` no longer nominates the last-registered document as
  current. After inserting, `_reindex_versions` orders the document's ACTIVE
  versions by `_chronology_key` = `(received_at or created_at, id)`, numbers them
  1..n and marks the highest as current; `previous_version_id` walks the chain
  forwards. No row is deleted and no content rewritten.
- `_as_naive_utc` normalises every timestamp on write (the column is sorted and
  compared, and mixing aware/naive datetimes makes those comparisons raise).
- `_update_shipment_status` now stores the document-aware status from
  `shipment_overview` instead of the issue-count-only `resolution_status`.

**`app/integrations/gmail/parser.py`.** `_received_iso` reads the `Date` header
(`email.utils.parsedate_to_datetime`), applies the timezone offset, and falls back
to Gmail's `internalDate`. A missing, malformed or zoneless header is handled
without raising; a zoneless header is read as UTC rather than local time.

**`app/integrations/gmail/sync.py`.**
- `poll_and_process` fetches and parses the pending messages, **sorts them
  oldest-first**, and only then processes them. Gmail's newest-first return order
  can no longer decide anything. Messages with no usable timestamp keep the
  position the API gave them (stable sort).
- `_received_at` converts the header/internalDate to naive UTC and stores it on
  `EmailRecord.received_at` before the pipeline runs.
- `_materialize_inline_documents` synthesises a body document **only for a kind
  no real attachment covers**, and names it `<shipment>_<kind>.frombody.txt` so it
  is recognisable as a fallback.
- `_attach_counterpart_document` is keyed on the **shipment reference written in
  the mail** (subject/body) with the filename as fallback, instead of requiring
  the `_SI.`/`_BL.` convention. `_DOC_TOKEN_RE` is deleted — it was the second,
  disagreeing interpretation.
- `_cache_sync_result` uses the shared detector, so the side panel reports the
  documents the engine actually read.

**`app/routers/frontend_compat.py`.** `email_detail` no longer calls
`workflow.process_email`. It returns the email with `result: null` and
`processed: false` when nothing has been stored; `received_at` is now included.

**`app/routers/shipments.py` / `app/schemas.py`.** `ShipmentSummaryOut.status` is
now the authoritative shipment status (it used to be `resolution_status`, which is
why `/shipments` could say VERIFIED about a shipment missing its BL). The
issue-resolution state is available separately as `resolution_state`, and the
overview is computed once instead of twice.

**`web/index.html`.** Minimal, functional only: the detail view now handles
`result: null` with a "Not processed yet" panel plus an explicit **Process this
email** button (`processEmail()`, POST `/emails/{id}/process`), and the command
palette tolerates a missing result. No layout, navigation or styling change.

## C. B1 — Version ordering: **PASS**

`scripts/audit_gmail_versions.py` TEST A runs the real `sync.py` with only the
Gmail API stubbed, both Gmail orders and both filename styles:

| Run | filenames | Gmail order | versions | CURRENT |
|---|---|---|---|---|
| 1 | `SI_v1/v2/v3.pdf` | v3, v2, v1 (newest first) | 38900 / 40150 / 41500 | **41500 ✅** |
| 2 | `SHP-001_SI.pdf` ×3 | v1, v2, v3 (oldest first) | 38900 / 40150 / 41500 | **41500 ✅** |

TEST A2: a late-arriving **older** document (35,000 KG, dated 17 Sep after v3 was
already current) is inserted as **version 1** and the current version is
**unchanged** at 41,500 KG — the old "last registered wins" behaviour is gone.

Three emails → one shipment, three version rows, no overwrite, each version
traced to its own `email_id`. Timestamps come from the `Date` header
(`received_at`); when a timestamp is missing the fallback is registration
`created_at` order — documented in `_chronology_key` and unchanged from before,
which is why the scored corpus is unaffected.

## D. B2 — Real attachment priority: **PASS**

TEST B sends three emails whose bodies contain the **entire SI text** *and* a real
`SI_v1/v2/v3.pdf`. Result: the real PDFs were read
(`38900 / 40150 / 41500`), the stored filenames are the sender's own `.pdf` files,
and **no `frombody` version exists**. Detector checks:

`SI_v1.pdf`, `SI_v2.pdf`, `SI_v3.pdf`, `Shipping_Instruction_SHP-001.pdf`,
`SHP-001_SI_Final.pdf` → all `SI`; `SI_and_BL.pdf` → `None` (still refused).

The Gmail layer and the engine now share one detector, which is the definition of
"consistent": a name cannot be a document in one layer and not the other.

## E. B3 — Shipment status / missing documents: **PASS**

Live API for SHP-002 (SI + invoice, no BL):

```
/shipments/by-key/SHP-002   status=NEEDS_ATTENTION
                            missing=['BL']
                            reasons=['Missing Bill of Lading', ...]
                            documents.BL.present=False, version_count=0
                            actions=['REQUEST_BL', 'MANUAL_REVIEW']
```

and in the `/shipments` list:
`SHP-002 status=NEEDS_ATTENTION shipment_status=NEEDS_ATTENTION resolution_state=VERIFIED`
— the contradictory `VERIFIED` is now confined to the explicitly-named
`resolution_state` (issue bookkeeping), not the shipment status. No fake BL row
exists. TEST C asserts all of this.

## F. GET side effect: **PASS**

TEST E, isolated database, an email that has never been processed:
`reports 14 → 14` after **six** GETs, no report row for the email, and the
response is still complete (`processed: false`, `result: null`, email payload
intact). `/ui/` still renders (500 rows listed, opening an email shows the
comparison) and now offers an explicit **Process this email** action instead of
processing on load.

## G. Gmail received_at: **PASS**

TEST G: `Fri, 18 Sep 2026 09:00:00 +0800` → `received_at = 2026-09-18 01:00:00`
(offset applied, stored naive-UTC), and every version carries the same timestamp
as its email. Malformed (`"not a date"`) → `None` without failing ingestion;
missing header → `None`; `internalDate` used as fallback;
`Fri, 19 Sep 2026 23:30:00 -0700` → `2026-09-20T06:30:00+00:00`.

## H. Real Gmail vs simulated Gmail

Both still run the **same** core: `workflow.evaluate_email` (classification,
extraction, OCR via `ai_service`, normalization, comparison) has exactly two
callers — `workflow._run_pipeline` and `ingest.analyze_email` — and the Gmail
path reaches it through `workflow.process_email`, i.e. the full
shipment-grouping/versioning path. No second implementation was added; the only
new shared code is `doc_types`, which *removes* a divergence rather than adding
one. Ingestion source differs, business logic does not.

## I. Test results

| Suite | Result |
|---|---|
| `pytest tests/` | **128 passed, 0 failed** |
| `scripts/smoke_test.py` (520-email corpus) | **9 passed, 0 failed** — status distribution identical to the baseline (OK 164 / SKIPPED 303 / MISMATCH 34 / NEEDS_REVIEW 32), 0 pipeline errors |
| `scripts/audit_gmail_versions.py` (acceptance A–G) | **51 passed, 0 failed** |
| `scripts/verify_demo_shipments.py` (TEST 1–10) | **42 passed, 0 failed** |
| Read safety: 10 × (`/shipments`, `/shipments/overview`, `/emails`, `/api/summary`, `/api/results`) | 533 reports / 785 versions / 96 shipments unchanged; no `attempts` or `processing_ms` change |

No test was weakened or deleted to make anything pass. Three initial failures in
the new acceptance suite were fixture bugs on my side (mismatched dict keys,
double-encoded body, wrong `email_id` prefix for Gmail rows) and were fixed in the
test, not by relaxing an assertion.

## J. Remaining limitations

**NOT VERIFIED IN CURRENT ENVIRONMENT:**

1. **Real Gmail OAuth and token refresh.** `gmail/client.py` was stubbed
   throughout. Only `sync.py` was exercised for real.
2. **Gmail pagination** — `list_message_ids` uses one page (`gmail_max_results`,
   default 10). Whether `nextPageToken` is handled for a larger inbox was not
   tested.
3. **`historyId` incremental sync.** The field is stored on
   `GmailMessageRecord.history_id` but no incremental-delta sync is implemented;
   each poll lists and diffs. Not verified against a live account.
4. **Gmail API rate limits / back-off.** No retry or quota handling in
   `client.py`. Untested.
5. **Real PostgreSQL concurrency.** All timings and the ordering discussion are
   SQLite, single process. `_reindex_versions` renumbers within one transaction;
   two concurrent syncs on Postgres could still interleave.
6. **Whether Gmail returns `messages.list` in a guaranteed order.** The fix
   removes the dependency entirely (the batch is now sorted by timestamp), but the
   assumption that Gmail returns newest-first was never confirmed against the live
   API.
7. **Genuine legacy `.doc` / `.xls` files.** Readers exist (olefile/xlrd) but no
   real sample is available, so they remain unproven and are not demoed.
8. **A live send-and-sync end-to-end run.** The three-email scenario was driven
   through the real code path with the Gmail API stubbed; nobody has sent actual
   mail from a second account and clicked Sync.
9. **The `/ui/` "Not processed yet" panel in a real browser.** The read-only
   contract and the null-result handling were tested at the API level and the page
   loads and opens processed emails; the unprocessed branch was not rendered in a
   browser because every corpus email is already processed.

### Deliberately left alone

- **`ReportRecord.status` is still overwritten** by the shipment's latest-pair
  outcome (`workflow._run_pipeline`), so a per-email report remains a snapshot
  rather than a stable property of that email. This was audit finding B5, classed
  "recommended, not urgent"; it is a semantic question about what an email-level
  verdict should mean, and changing it would alter the scoring surface. The
  shipment-level API is self-consistent.
- **`/shipments` computes `build_overview` per row** (N+1). Fine at 96 shipments.
- **Counterpart pairing can still record a `DUPLICATE` version row** when the same
  file is re-attached; it is now named `frombody` for synthetic files, and BL
  version counts are correct in the acceptance runs.
- **`POST /api/emails/{id}/review` still processes on demand** — that is a write
  action taken deliberately by a reviewer, not a read.
- **Dashboard layout, navigation, `/shipments/overview` UI, styling** — untouched,
  as instructed.
