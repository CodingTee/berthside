# BerthSide Architecture Audit

**Scope:** audit and validation only. **No production code was changed.**
Nothing was refactored, renamed or rebuilt. Two audit scripts were added
(`scripts/audit_gmail_versions.py`) — read `§22` for what was actually executed.

Anything marked *Not verified in the current environment* was not tested.

---

## 1. Current end-to-end workflow

There is **one processing core** and **two write paths** into it.

```
                        ┌─────────────────────────┐
  Real Gmail ──────────▶│ integrations/gmail/     │
  (OAuth + API)         │ sync.poll_and_process   │──┐
                        └─────────────────────────┘  │
                                                     │  workflow.process_email(db, email_id)
  Inbox (bundle / demo) ──▶ inbox_service.get_email  │  (db-backed)
                        └─────────────────────────┘  │        │
                                                     ├────────┤
  POST /api/v1/ingest   ──▶ routers/ingest           │        ▼
  POST /api/v1/analyze  ──▶ .analyze_email ──────────┘   workflow._run_pipeline
  POST /api/process     ──▶ routers/integration            │
  (stateless: verdict only, no shipment rows)              ├─ evaluate_email()      ← shared
                                                           │     ├─ ai_service.classify_email
                                                           │     ├─ ai_service.extract_document
                                                           │     │     └─ pdf/docx/xlsx/csv/xml/OCR
                                                           │     └─ comparison.compare
                                                           ├─ versioning.sync_processed_documents
                                                           │     └─ register_document_version
                                                           ├─ _register_invoice_document
                                                           └─ versioning.sync_issues_for_report
                                                                  │
                                                                  ▼
                                                     shipments / documents /
                                                     document_versions / issues

  Reads (no processing):  /api/results  /shipments*  /reports  /api/summary
```

**The verdict logic is single-implementation.** `workflow.evaluate_email`
(`app/services/workflow.py`, def at line 351) is called by exactly two places:
`workflow._run_pipeline` (line 625) and `routers/ingest.analyze_email` (line 228).
Its own docstring records that this is deliberate — both entry points used to
carry their own copy and drifted.

**The grouping logic is not shared.** Shipment identification, version control,
issue creation and invoice registration live only in `_run_pipeline`, i.e. only
behind `workflow.process_email`.

## 2. Simulated / demo email workflow

| Surface | Call | Core reached | Shipment rows created? |
|---|---|---|---|
| ShipMail "Run BerthSide" button | `POST /api/process` (`shipmail/frontend/index.html:613`) | `analyze_email` → `evaluate_email` | ❌ **no** |
| ShipMail inbox / side panel read | `GET /api/results/{id}` (`index.html:588`) | none (cache read) | ❌ no |
| ShipMail demo inbox data | static `shipmail/data/emails.json` | none | ❌ no |
| Curated demo dataset (this repo) | `POST /api/v1/ingest` + `POST /emails/{id}/process` | `evaluate_email` **+** `_run_pipeline` | ✅ yes |
| Scored corpus (build time) | `scripts/precompute_reports.py` → `workflow.process_email` | `evaluate_email` **+** `_run_pipeline` | ✅ yes |
| `/ui/` Operations Console detail | `GET /api/emails/{id}` | auto-runs `process_email` if no report exists | ✅ yes (**side effect on read** — see §10) |

So "simulated" is not one workflow: the **click-through demo path is the weak
one** (verdict only), while the corpus/demo *build* path is the strong one.

## 3. Real Gmail workflow

```
POST /api/gmail/poll
  → sync.poll_and_process                     (sync.py:37)
      → client.list_message_ids()             (client.py:21, no sort)
      → for each id: sync.process_message_id  (sync.py:112)
          → client.get_message + parser.parse_message
          → sync.process_parsed_payload       (sync.py:118)
              → _persist_gmail_attachments    (sync.py:199)
              → _materialize_inline_documents (sync.py:221)   ← body → .txt attachment
              → _attach_counterpart_document  (sync.py:254)   ← cross-email SI/BL pairing
              → upsert EmailRecord, GmailMessageRecord
              → workflow.process_email(db, email_id)          ← SAME db-backed core
              → _cache_sync_result            (sync.py:282)   ← mirror into /api/results
      → _reprocess_completed_pairs            (sync.py:82)    ← second pass
```

Idempotency: `GmailMessageRecord.processing_status == "PROCESSED"` short-circuits
re-sync. Verified: a second `poll_and_process` processed 0, skipped 5, and left
every `ReportRecord.attempts` / `processing_ms` untouched.

## 4. Do simulated and real Gmail use the same processing core?

**Partly. This is the central finding.**

| Stage | Shared? | Owner |
|---|---|---|
| Attachment decode + security gate + ZIP expansion | ✅ | `routers/ingest._decode_all_attachments` |
| Classification | ✅ | `ai_service.classify_email` via `workflow.evaluate_email` |
| OCR (scanned PDF / image) | ✅ | `ai_service` → `services/ocr` |
| Extraction | ✅ | `ai_service.extract_document` |
| Normalization | ✅ | `services/extractor.normalize` |
| SI/BL pairing (within one email) | ✅ | `workflow._find_doc_attachments` |
| Comparison / verification | ✅ | `services/comparison.compare` via `evaluate_email` |
| **Shipment identification** | ❌ | `_run_pipeline` only |
| **Version control** | ❌ | `_run_pipeline` only |
| **Missing-document accounting** | ⚠️ | verdict says `missing_attachment` (per email); shipment-level view rebuilt in `shipment_overview` |
| **Issue records** | ❌ | `_run_pipeline` only |

Answer to the brief's question — *"Simulated Gmail → one processing
implementation, Real Gmail → another"*: **not two implementations of the same
rules, but two different entry points with different persistence depth.** A
`/api/process` call and a Gmail sync of the same email produce the *same verdict*
but *very different database state*: the former leaves no shipment, no document
and no version behind.

## 5. Exact divergence points

| # | Location | Divergence |
|---|---|---|
| D1 | `routers/integration.py` `process_email` (`/api/process`) → `analyze_email` (line 106) | Verdict only; no `_run_pipeline`. **This is the path the demo UI uses.** |
| D2 | `routers/ingest.py` `analyze_email` (line 174) | Same: no shipment/version/issue rows. Used by `/api/v1/analyze` and `/api/v1/ingest`. |
| D3 | `integrations/gmail/sync.py:157` | Calls `workflow.process_email` → full path. |
| D4 | `routers/frontend_compat.py:159` | GET with a write: `if report is None: workflow.process_email(...)` |
| D5 | `integrations/gmail/parser.py` `parse_message` | **Never reads the `Date` header** → `received_at` is NULL in `EmailRecord` for every real Gmail message, and therefore NULL on every document version. |
| D6 | `integrations/gmail/sync.py` `_DOC_TOKEN_RE` / `_PAIR_TOKEN_RE` | Require `_SI.`/`_BL.` immediately before the extension; human filenames (`SI_v1.pdf`) are not recognised *here*, while `workflow._fuzzy_doc_type` (tier 2) does recognise them — the two layers disagree about what counts as an SI/BL. |

## 6. Shipment grouping mechanism

`versioning.identify_shipment(email, si_fields, bl_fields)` (`versioning.py:64`),
three tiers, first match wins:

1. **Reference regex over the haystack** — subject **+ body + email_id + attachment filenames**
   (`REFERENCE_RE`, `versioning.py:31`): `\b(?:SDOC[-_ ]?)?\d{3,8}\b` or
   `\b[A-Z0-9]{3,8}[-_/][A-Z0-9]{3,8}\b` → key `REF:<upper>`.
2. Shipper + port-of-loading + port-of-discharge signature → `SIG:<sha256[:16]>`.
3. Fallback `EMAIL:<email_id>` — one shipment per email.

Sender, subject and filename are **not** the primary key: they are inputs to a
reference *search*, and the extracted document fields are the tier-2 fallback.
That matches the brief's requirement. Weakness: tier 1's generic numeric branch
(`\d{3,8}`) can latch onto an unrelated number in a body (this already produced
one odd key in the real corpus, `REF:914`).

## 7. Version-control mechanism

`versioning.register_document_version` (`versioning.py:119`):

1. `_get_or_create_document(shipment_id, doc_type)` → one row per (shipment, type).
2. Duplicate test — a row is a duplicate if **either**
   `content_hash` matches **or** `normalized_hash` matches anything already stored
   for that document. Duplicates get `document_status="DUPLICATE"`,
   `is_latest=0`, the *same* version number, and do not advance anything.
3. Otherwise `next_number = max(ACTIVE version_number) + 1`; the previous latest
   is demoted (`is_latest=0`) and the new row becomes `is_latest=1`.

**What determines "newer": order of registration. Nothing else.**
Not timestamps (`received_at` is stored but never read for ordering), not a
version token in subject or filename, not the email thread. `_next_version_number`
and `latest_version` both key off `is_latest` / `version_number`, which are
assigned at insert time.

## 8. How v1/v2/v3 are currently detected

**They are not detected.** No code parses `v1`/`v2`/`v3` from a subject or a
filename. `v1, v2, v3` in the audit test are simply the 1st, 2nd and 3rd
documents processed for that shipment. Consequences for the brief's questions:

| Question | Answer |
|---|---|
| How is "same shipment" determined? | §6 — reference → field signature → email id |
| How is "newer" determined? | **Processing order** (§7) |
| Uses timestamps? | No (stored, unused) |
| Uses explicit version numbers? | No |
| Uses document content? | Only to declare a *duplicate*, never to order |
| Uses email order? | **Yes — this is the de-facto rule** |
| Uses filename? | Only for doc-*type* detection, never for ordering |
| Subject without "v2"/"v3"? | Works — versions come from order, not from the subject |
| Filename without a version number? | Works for ordering; but see D6 (naming affects *which file* is read) |
| Same Shipping ID in several emails? | Same shipment, versions accumulate — confirmed |
| Two versions near-simultaneously? | No locking; both get rows, last processed becomes current. Single-process SQLite makes a true write race unlikely, but order is still arbitrary. |

## 9. Can three real Gmail emails become one shipment with three versions?

**Structurally yes — and the version ORDER is currently wrong.**

Executed: `scripts/audit_gmail_versions.py` drives the real
`gmail/sync.py` with a stubbed Gmail API only (classification, extraction,
versioning, verification all real). Three SI emails for SHP-001 (38,900 /
40,150 / 41,500 KG) plus a BL and an invoice.

| Run | filenames | Gmail order | versions created | CURRENT SI | correct? |
|---|---|---|---|---|---|
| 1 | `SI_v1/v2/v3.pdf` | v3,v2,v1 (newest first) | v1=v3email, v2=v2email, v3=v1email | **38,900 KG (the oldest)** | ❌ |
| 2 | `SI_v1/v2/v3.pdf` | v1,v2,v3 | v1,v2,v3 | 38,900 KG | ❌ |
| 3 | `SHP-001_SI.pdf` ×3 | v3,v2,v1 (newest first) | v1=41500, v2=40150, v3=38900 | **38,900 KG (the oldest)** | ❌ |
| 4 | `SHP-001_SI.pdf` ×3 | v1,v2,v3 | v1,v2,v3 | 41,500 KG | ✅ |

Three separate emails → three separate `EmailRecord` rows ✅, one shipment
`REF:SHP-001` ✅, three SI version rows ✅, no overwrite of old versions ✅.
**But `is_latest` follows processing order, and `client.list_message_ids` returns
Gmail's default (newest-first) with no sort** (`sync.py:41`), so in runs 1 and 3
the *first-issued* document became the current one. The expected behaviour in the
brief (v3 = current) is only produced when messages happen to be processed
oldest-first.

Two further defects visible in the same runs:

- **The attached PDF is bypassed when the filename is human-style.**
  `_materialize_inline_documents` (`sync.py:221`) skips only when
  `_DOC_TOKEN_RE` matches the existing attachment. `SI_v1.pdf` does not match, so
  the **email body** was written as `SHP-001_SI.txt`, and tier-1 in
  `workflow._find_doc_attachments` prefers that `.txt` over the real PDF. Result:
  every extracted field `None`, every report `NEEDS_REVIEW`, on runs 1 and 2 —
  with the real PDF sitting unread on disk. Runs 3–4 (`_SI.` token) extract
  correctly (38,900 / 40,150 / 41,500).
- **BL versions = 4 for one BL email**, because counterpart pairing re-attaches
  the same file to other emails and each re-attach is recorded as a version row
  (3 × `DUPLICATE`). Noise, not corruption.

## 10. Do Gmail/source records stay separate?

✅ Yes. One `EmailRecord` per message (`email_id = GMAIL-<messageId>`), one
`GmailMessageRecord` per message id, attachments written to
`ingest_dir/gmail/<email_id>/`. Nothing merges emails. The audit run stored 5
emails for 5 messages.

## 11. How Dashboard aggregation works

Read-only, from stored rows:
- `/shipments` → `_shipment_summary` (`routers/shipments.py:44`), per shipment.
- `/shipments/overview`, `/shipments/{id}/overview`, `/shipments/by-key/{code}`
  → `shipment_overview.build_overview` (line 175) — documents + full version
  history, current version, completeness, missing documents, issues, per-field
  verification, source emails, actions.
- `/api/results/{id}` → the process-once cache; `/api/summary`,
  `/api/review-queue`, `/reports/*` → aggregates.

Measured cost (91 shipments, SQLite): `/shipments` 0.40–0.90 s,
`/shipments/overview` 0.35–0.41 s. Verified read-only: 9 shipment reads changed
no `attempts`/`processing_ms`.

## 12. Where source-email traceability lives

| Item | Stored on | Populated for real Gmail? |
|---|---|---|
| sender | `EmailRecord.sender`, `GmailMessageRecord.sender` | ✅ |
| subject | `EmailRecord.subject`, `GmailMessageRecord.subject` | ✅ |
| message / thread id | `GmailMessageRecord` | ✅ |
| attachment filename | `DocumentVersionRecord.filename` | ✅ |
| email reference | `DocumentVersionRecord.email_id` | ✅ |
| **received timestamp** | `EmailRecord.received_at`, `DocumentVersionRecord.received_at` | ❌ **NULL** — `parser.parse_message` never reads the `Date` header (D5). Verified on the audit run: Gmail messages carried a `Date` header and every version row had `received_at=None`. |

## 13. OCR / extraction duplication analysis

Call sites (whole `app/`):

- `ai_service.extract_document` → `workflow.py:431`, `:432` (SI, BL — once per pair),
  `workflow.py:710` (standalone SI/BL, only when the pair is incomplete).
- `ai_service.document_text` → `workflow.py:611` (invoice only).
- `ocr_pdf` → `ai_service.py:198` (3rd rung of the PDF ladder, only if the text
  layer is empty). `ocr_image` → `ai_service.py:393`. **Single implementation,
  single entry point.**
- `compare` → `workflow.py:518` (`evaluate_email`), `workflow.py:649`
  (`_run_pipeline` fallback), `versioning.py:298` (`latest_pair_comparison`),
  `workflow.py:822` (`apply_review` re-comparison after a human correction).

**No document is OCR'd or extracted twice within one processing run.** The one
repeated work is `compare` (pure dict arithmetic, no IO) — for a paired email it
runs once in `evaluate_email` and again via `latest_pair_comparison`. Harmless cost.

Cross-email repetition does occur when SI and BL arrive in separate Gmail
messages: the SI file of the *other* email is extracted when the counterpart is
attached, and the pairing layer copies files across email directories so the same
document can exist under two paths. Bounded, but it is why one BL produced 4
version rows.

## 14. Verification duplication analysis

Two computations, one of which overwrites the other:

1. `evaluate_email` compares **this email's own SI against its own BL** → verdict.
2. `_run_pipeline` (lines 646–656) then calls `latest_pair_comparison(db, shipment.id)`
   — **latest stored SI vs latest stored BL** — and, if it returns an outcome,
   **overwrites** `verdict.status / has_defect / defect_fields / review_reason / field_results`
   with it before persisting.

Consequence, visible in audit run 3: all three SI emails stored
`status=MISMATCH`, although each email's own SI/BL pair was internally consistent
(the SI used was the shipment's then-current one, which belonged to a different
email). So `reports.status` is a **snapshot of mutable shipment state at
processing time**, not a property of that email — and it goes stale when a newer
version arrives later. `/shipments/*/overview` recomputes the shipment truth, so
the API is self-consistent; the per-email report is not.

## 15. Missing-document logic

- Per email: `evaluate_email` returns `status=NEEDS_REVIEW`,
  `review_reason="missing_attachment"` with `extracted.missing_documents` /
  `present_documents` (`workflow.py:409-426`).
- Per shipment: `shipment_overview.build_overview` derives
  `missing_documents` from which document types have an ACTIVE version —
  `REQUIRED = SI, BL`, `OPTIONAL = INVOICE`. No fake BL row is created, and
  "Missing BL" is emitted as a **reason string**, never a `doc_type`. Confirmed
  on SHP-002: `BL.present=False, version_count=0`, reason
  `"Missing Bill of Lading"`, action `REQUEST_BL`.
- Cost: one query over `document_versions` per shipment. **No reprocessing pass.**
- Gap: a shipment with only an SI is created by
  `_register_documents_without_counterpart` (added in the previous iteration), so
  `versioning.resolution_status` sees *zero issues* and calls it VERIFIED — see §16.

## 16. Shipment-status logic analysis — three competing models

Demonstrated on the same shipment (SHP-002, id 92):

| Source | Value | Correct? |
|---|---|---|
| `shipment_overview.build_overview` (document-aware) | `NEEDS_ATTENTION` | ✅ |
| `versioning.resolution_status` (issue-count only) | `VERIFIED` | ❌ |
| `ShipmentRecord.status` (stored, written by `_update_shipment_status`) | `NEEDS_REVIEW` | ❌ |
| `ReportRecord.status` of the last email | `NEEDS_REVIEW` | n/a (email-level) |

Root cause: `resolution_status` (`versioning.py:454`) infers status from
`IssueRecord` rows only; `total == 0 → "VERIFIED"`. A missing document is not an
issue row, so an incomplete shipment reads as verified. `_update_shipment_status`
(line 494) persists that value onto `ShipmentRecord.status`, and one vocabulary
(`VERIFIED / RESOLVED / PENDING_APPROVAL / OPEN / …`) coexists with another
(`PROCESSING / INCOMPLETE / NEEDS_ATTENTION / VERIFIED`).

`ShipmentRecord.status` is therefore written but not authoritative for reading —
`/shipments*` uses `build_overview`. Nothing currently contradicts itself *in the
`/shipments` API*, but the stored column and `/shipments/{id}/resolution-status`
disagree with it.

## 17. API / data-flow: where each fact lives

| Fact | Owner | Stored? | Recalculated? |
|---|---|---|---|
| Raw email | `EmailRecord` / `GmailMessageRecord` / bundle JSON | ✅ | – |
| Attachment | disk (`ingest_dir/...`, `simulated-gmail/data/attachments`) + path on `EmailRecord.attachments` | ✅ | – |
| Document type | `DocumentRecord.doc_type`, `DocumentVersionRecord.doc_type` | ✅ | re-derived at read time in `shipmail` UI from filenames |
| Extracted fields | `DocumentVersionRecord.extracted_fields`, `ReportRecord.extracted` | ✅ | – |
| Confidence | `ReportRecord` (via verdict) | ✅ | – |
| Shipping ID | `ShipmentRecord.shipment_key` / `reference_number` + `ReportRecord.extracted.shipment_key` | ✅ | – |
| Version number / current | `DocumentVersionRecord.version_number` / `is_latest` | ✅ | – |
| Verification result | `ReportRecord.status/has_defect/defect_fields/field_results` | ✅ | recomputed by `latest_pair_comparison` on write |
| Mismatch | `IssueRecord` (+ `ReportRecord.defect_fields`) | ✅ | – |
| Missing document | *derived* (no column) | ⚠️ per-email snapshot only | recomputed per read by `build_overview` |
| Shipment status | *derived* | ⚠️ also written to `ShipmentRecord.status` | recomputed per read (§16) |
| Source email | `DocumentVersionRecord.email_id` → join `EmailRecord` | ✅ | – |

**Can the Dashboard get everything without triggering processing?** For
`/shipments*`, `/api/results/*`, `/api/summary`, `/reports/*` — ✅ yes, verified.
For `GET /api/emails/{id}` — ❌ no: it calls `process_email` when a report is
missing (proven empirically: 0 reports before, 1 report with
`attempts=1, status=SKIPPED` after a single GET).

## 18. Redundancies and unnecessary complexity

| # | Redundancy | Where | Why | Harmful? | Change now? |
|---|---|---|---|---|---|
| R1 | Verdict computed twice per paired email | `workflow.py:518` + `:649`/`versioning.py:298` | `_run_pipeline` prefers the shipment's latest pair over the email's own | Low CPU. **But it silently rewrites the per-email report** (§14) | **A — fix now** (keep one authoritative verdict; make the overwrite explicit or drop it) |
| R2 | Two status models + one stored column | `versioning.resolution_status` + `shipment_overview.build_overview` | different questions asked by different features | Yes — same shipment reports VERIFIED and NEEDS_ATTENTION | **A — fix now** (one vocabulary; keep `resolution_status` for the resolution workflow only) |
| R3 | `/shipments` computes status twice per row | `routers/shipments.py:45` and `:48` | `_shipment_summary` calls `resolution_status` *and* `build_overview`, each doing its own queries | Mild; `/shipments` is the slower of the two endpoints (0.40–0.90 s vs 0.35–0.41 s) | B — compute once, reuse |
| R4 | N+1 queries per shipment in list/overview | `build_overview` called once per row | no batching | Grows linearly with shipments (91 today) | B |
| R5 | Two different doc-type detectors | `sync._DOC_TOKEN_RE` vs `workflow._fuzzy_doc_type` | Gmail layer wants an exact token, engine accepts fuzzier names | **Yes — causes D6/§9 (PDF bypassed)** | **A — fix now** |
| R6 | GET with write side effect | `frontend_compat.py:159` | "so the detail view always works" | Yes — a refresh can trigger classification + extraction + OCR | **A — fix now** |
| R7 | Counterpart pairing duplicates documents across email dirs | `sync._attach_counterpart_document` | SI/BL sent as separate emails | Causes spurious DUPLICATE version rows (BL ×4) | B |
| R8 | `/api/process`, `/api/v1/analyze`, `/api/v1/ingest` all wrap `analyze_email` with different persistence | 3 routers | historical layering | Confusing, but each has a distinct contract; **not** a rule duplication | C — keep, document |
| R9 | Frontend re-derives document labels/counts from filenames | `shipmail/frontend/index.html` | presentation | No — display only, no business rule | Leave |
| R10 | The curated demo dataset is separate from the ShipMail inbox dataset | `demo-shipments/` vs `shipmail/data/` | the inbox mirrors the scored bundle | No | Leave (documented) |

No unnecessary polling found. No repeated OCR found. No frontend-triggered
document processing beyond R6.

## 19. Bugs and architectural risks

| # | Severity | Finding |
|---|---|---|
| B1 | **High** | **Current version is decided by processing order, and Gmail returns newest-first.** Proven: 3 SI emails in one sync → the oldest document became current (`is_latest=1`). `sync.py:41` does not sort; `versioning.py:210` takes last-registered as latest; `received_at` is never used for ordering. |
| B2 | **High** | **A human-named attachment is silently ignored.** With `SI_v1.pdf`, `_materialize_inline_documents` writes the email *body* as `SHP-001_SI.txt`, and tier-1 doc-type detection prefers it over the real PDF → all fields `None`, everything `NEEDS_REVIEW`, real document unread. |
| B3 | **High** | **`resolution_status` reports an incomplete shipment as VERIFIED** (no issue rows ⇒ `total == 0 ⇒ VERIFIED`), and that value is persisted to `ShipmentRecord.status`. |
| B4 | **Medium** | `GET /api/emails/{id}` runs the pipeline as a side effect (proven). A Dashboard refresh can therefore trigger classification/extraction/OCR. |
| B5 | **Medium** | Per-email `ReportRecord` verdicts are overwritten with the shipment's then-current pair outcome, so they are not stable properties of the email and disagree with the shipment view later. |
| B6 | **Medium** | `received_at` is NULL for every real Gmail message (parser ignores the `Date` header), so source-email traceability is missing its timestamp and no time-based ordering is possible even if wanted. |
| B7 | **Low** | Demo click-through (`/api/process`) creates no shipment/document/version rows — the demo does not exercise the business layer that the future Dashboard needs. |
| B8 | **Low** | Shipment identity tier 1 can latch onto an unrelated number (e.g. `REF:914` in the real corpus). |
| B9 | **Low** | One BL produced 4 version rows (pairing + duplicates). Cosmetic but confusing in a version history UI. |
| B10 | **Low** | `/shipments` and `/shipments/overview` duplicate work per row (R3/R4). |

## 20. Recommended changes

### A. Must fix now
1. **B1 — order versions by arrival, not by registration.** Sort the messages in
   `sync.poll_and_process` oldest-first before processing (cheap, contained), and
   make `register_document_version` refuse to demote an already-current version
   when the incoming document is *older* (compare `received_at` when both are
   known; fall back to registration order when not).
2. **B2 — make the Gmail layer and the engine agree on doc-type detection.** Have
   `_materialize_inline_documents` guard on the same rules the engine uses
   (`_fuzzy_doc_type`) and never prefer a synthesised `.txt` over a real
   attachment for the same document type.
3. **B3 — one status vocabulary.** Keep `shipment_overview` as the shipment
   status owner; stop writing `resolution_status` into `ShipmentRecord.status`
   (or rename that column to `resolution_state` so it can no longer be mistaken
   for the shipment status).
4. **B4 — remove the write from `GET /api/emails/{id}`.** Return the email with
   `report: null` and let the client call the explicit process endpoint.
5. **B6 — parse the `Date` header in `parser.parse_message`** and populate
   `EmailRecord.received_at`; the field already exists and `build_overview`
   already surfaces it.

### B. Recommended, not urgent
6. **B5 — stop overwriting the email verdict** with `latest_pair_comparison`;
   store the pair outcome on the shipment (it already is, via issues) and let the
   report describe the email it belongs to.
7. **R3/R4** — compute `build_overview` once per shipment per request; batch the
   per-shipment queries.
8. **R7/B9** — stop recording a DUPLICATE version when the counterpart pairing
   simply re-attaches a file already registered for that shipment.
9. **B8** — tighten the reference regex's numeric branch (require a letter prefix
   or a nearby `SHP`/`OC`/`BL` context word).

### C. Future Dashboard work (not this iteration)
10. Shipment-centred UI over `/shipments/overview` — the payload already carries
    shipment id, document types, current version, full version history,
    verification, mismatches, missing documents, actions and source emails.
11. Decide whether the demo click-through should call the stateful endpoint so the
    demo demonstrates grouping and versioning (§B7).
12. Consider making the *shipment* — not the email — the unit that carries the
    verdict, once the Dashboard renders shipments.

## 21. File / function index for each finding

| Finding | File | Function / symbol |
|---|---|---|
| Core verdict (shared) | `app/services/workflow.py` | `evaluate_email` (351), `_run_pipeline` (624) |
| Stateless entry (no grouping) | `app/routers/ingest.py` | `analyze_email` (174) |
| `/api/process` (no grouping) | `app/routers/integration.py` | `process_email` (59) |
| Real Gmail entry | `app/integrations/gmail/sync.py` | `poll_and_process` (37), `process_parsed_payload` (118) |
| B1 order bug | `app/integrations/gmail/sync.py` / `app/services/versioning.py` | `poll_and_process` loop (41) / `register_document_version` (210-231), `latest_version`, `_next_version_number` (263) |
| B2 naming mismatch | `app/integrations/gmail/sync.py` / `app/services/workflow.py` | `_DOC_TOKEN_RE` (32), `_materialize_inline_documents` (221) / `_find_doc_attachments`, `_fuzzy_doc_type` (706) |
| B3 status conflict | `app/services/versioning.py` | `resolution_status` (454), `_update_shipment_status` (494) |
| B4 read-triggered processing | `app/routers/frontend_compat.py` | `email_detail` (152, process at 160) |
| B5 verdict overwrite | `app/services/workflow.py` / `app/services/versioning.py` | `_run_pipeline` (646-656) / `latest_pair_comparison` (294) |
| B6 missing timestamp | `app/integrations/gmail/parser.py` | `parse_message` (never reads `Date`) |
| Missing documents | `app/services/shipment_overview.py` | `build_overview` (175), `REQUIRED_DOC_TYPES` (38) |
| Shipment status (authoritative) | `app/services/shipment_overview.py` | status block (192-203) |
| Version/dup rule | `app/services/versioning.py` | `register_document_version` (119), `_normalized_hash` (253) |
| Shipment identity | `app/services/versioning.py` | `identify_shipment` (64), `REFERENCE_RE` (31) |
| OCR (single entry) | `app/services/ai_service.py` | `document_text` (407), `ocr_pdf` call (198), `ocr_image` call (393) |
| Comparison (single impl) | `app/services/comparison.py` | `compare`, called from `workflow.py:518/649/822`, `versioning.py:298` |
| Shipment API | `app/routers/shipments.py` | `_shipment_summary` (44), `/shipments/overview` (79), `/shipments/by-key` (90) |
| Demo UI call path | `backend/shipmail/frontend/index.html` | `runBerthSide` (608, POST `/api/process` at 613); read at 588 |

## 22. Tests performed

| Test | Result |
|---|---|
| Static call-graph audit of every entry point (`grep` over `app/`) | ✅ single-implementation confirmed for classification / extraction / OCR / comparison; grouping exists only in `_run_pipeline` |
| `scripts/audit_gmail_versions.py` — 3 SI versions + BL + invoice through the **real** `gmail/sync.py` (only the Gmail API stubbed), 4 combinations of filename style × Gmail order | ✅ emails stay separate, 1 shipment, 3 preserved versions; ❌ current version inverted when Gmail order is newest-first; ❌ PDF bypassed with human filenames |
| Gmail idempotency — second `poll_and_process` | ✅ `processed=0, skipped=5`, all `attempts`/`processing_ms` unchanged |
| Shipment endpoints read-only check (9 reads) | ✅ no `attempts` change, ~1.2 s total |
| `GET /api/emails/{id}` side effect, isolated DB | ✅ report created by a GET (`attempts=1`) |
| Competing status models on SHP-002 | ✅ reproduced: `NEEDS_ATTENTION` vs `VERIFIED` vs `NEEDS_REVIEW` |
| Latency: `/shipments` vs `/shipments/overview` (91 shipments) | `/shipments` 0.40–0.90 s, `/shipments/overview` 0.35–0.41 s |
| Regression gates (unchanged code, re-run) | `pytest` **128/128**, `smoke_test` **9/9**, `verify_demo_shipments` **42/42** |

**No production file was modified.** The only new files are this report and
`scripts/audit_gmail_versions.py`.

## 23. Cannot currently be verified in this environment

1. **The Gmail API itself.** The audit drove `gmail/sync.py` with a stubbed
   `client` module. Pagination, `historyId` deltas, real MIME shapes, attachment
   size limits, quota/back-off, token refresh and Gmail's default `q=in:inbox`
   result ordering are therefore **not verified in the current environment**.
   The newest-first assumption comes from the Gmail API's documented default
   behaviour, not from a live call.
2. **A live end-to-end send-and-sync** (`POST /api/gmail/poll` against the real
   mailbox) — not run in this audit; it requires sending mail from a second
   account.
3. **Whether Google returns `messages.list` in a *guaranteed* order.** If it does,
   B1's impact depends on that order; the fix is required either way because the
   code makes no ordering guarantee of its own.
4. **Legacy `.doc` / `.xls` reading** — unchanged from the previous iteration's
   note: readers exist, no genuine sample available.
5. **Postgres behaviour.** All timings and the write-race discussion are SQLite,
   single-process. `DATABASE_URL` can point at Postgres on Render, where
   concurrent syncs could interleave differently.
6. **`/ui/` in a browser** — the `/api/emails/{id}` side effect was proven at the
   route-function level and by code inspection; the click-through itself was not
   reproduced in a browser this pass.
