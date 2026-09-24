# BerthSide — Backend & Real Gmail Verification Report

**This task changed no production code.** Audit and verification only. Two probe
scripts were added (`scripts/verify_checklist.py`, and the earlier
`scripts/audit_gmail_versions.py`), plus this report.

Status labels used: **VERIFIED WORKING** · **PARTIALLY WORKING** · **NOT
VERIFIED** · **BROKEN** · **NOT IMPLEMENTED**.

Evidence for every claim: a probe run, a live API call, or a named
file/function. Where something could not be exercised, it says
**NOT VERIFIED IN CURRENT ENVIRONMENT**.

---

## A. Current end-to-end workflow

```
POST /api/gmail/poll                                    routers/gmail.py
  └─ sync.poll_and_process                              integrations/gmail/sync.py:37
       ├─ client.list_message_ids                       client.py:21   (Gmail API)
       ├─ _fetch_pending  fetch+parse ALL, SORT OLDEST FIRST          sync.py:82
       │    ├─ client.get_message
       │    └─ parser.parse_message                     parser.py:12
       │         ├─ headers → subject / from / **Date**
       │         └─ _walk_parts → body text + attachments (base64 bytes)
       └─ per message, oldest first:
            process_parsed_payload                      sync.py:139
              ├─ _persist_gmail_attachments             sync.py:225  → disk, paths on EmailRecord
              ├─ _materialize_inline_documents           sync.py:247  body → fallback .frombody.txt
              ├─ _attach_counterpart_document            sync.py:306  cross-email SI/BL pairing
              ├─ upsert EmailRecord (sender/subject/body/attachments/received_at)
              │  + GmailMessageRecord (message_id/thread_id/history_id/status)
              │  + ReportRecord
              └─ workflow.process_email(db, email_id)   workflow.py:88   ← the stateful core
                   └─ workflow._run_pipeline            workflow.py:649
                        ├─ evaluate_email               workflow.py:351  ← THE shared verdict
                        │    ├─ ai_service.classify_email
                        │    ├─ _find_doc_attachments    workflow.py:790
                        │    ├─ ai_service.extract_document  → document_text → pdf/csv/xml/
                        │    │      docx/xlsx/OCR ladder; extractor.extract_fields
                        │    │      → normalize
                        │    └─ comparison.compare       7 fields
                        ├─ versioning.sync_processed_documents  → identify_shipment,
                        │      register_document_version (+ _reindex_versions by chronology)
                        ├─ _register_invoice_document    workflow.py:584
                        └─ versioning.sync_issues_for_report
       └─ _reprocess_completed_pairs (second pass)      sync.py:104

Reads (no processing):  /shipments*  /api/results*  /reports*  /api/summary  /api/emails
```

**`/api/v1/analyze`, `/api/v1/ingest` and `/api/process` stop at `evaluate_email`:**
they return a verdict but create **no** shipment / document / version rows. This
is the only real divergence between the simulated click-through and real Gmail.

## B. Real Gmail capability

| Stage | Status | Evidence |
|---|---|---|
| Gmail list/get/attachment API | **NOT VERIFIED IN CURRENT ENVIRONMENT** | `client.py` was stubbed; no live mailbox, no OAuth/token-refresh test, no pagination or quota test |
| Parsing a Gmail message payload | VERIFIED WORKING | `parser.parse_message` driven with real Gmail-shaped payloads |
| Metadata preservation | PARTIALLY WORKING | see §2 — **recipient is NOT IMPLEMENTED** |
| Attachment storage | VERIFIED WORKING | `_persist_gmail_attachments`; files on disk, paths recorded |
| Attachment auto-processing | VERIFIED WORKING | the stubbed-Gmail run stored and read PDFs without any manual step |
| Classification / extraction / OCR / normalization / comparison | VERIFIED WORKING | single implementation, reached by the Gmail path |
| Shipment grouping + versioning | VERIFIED WORKING | `audit_gmail_versions.py` A/A2 (51/51) |
| Missing documents / status | VERIFIED WORKING | TEST C, live `/shipments/by-key/SHP-002` |
| Unsupported / dangerous files rejected | VERIFIED WORKING | `.exe` → `safe=False "Executable or script extension blocked"`; corrupt PDF → `source='unreadable'`, no exception |
| Processing errors stored | VERIFIED WORKING | `GmailMessageRecord.error_message` + `ReportRecord.error_message`; per-message `failed` count in the poll response |
| **Live send → sync → verify** | **NOT VERIFIED IN CURRENT ENVIRONMENT** | requires a second mailbox; cannot be done from here |

## C. Simulated vs Real Gmail

| Capability | Simulated / demo | Real Gmail | Same processing core? |
|---|---|---|---|
| Email ingestion | `POST /api/v1/ingest` (persists) | `POST /api/gmail/poll` | ✅ same storage model |
| Message metadata | from the demo JSON | from Gmail headers + `internalDate` | ✅ `EmailRecord` + `GmailMessageRecord` |
| Attachment retrieval | base64/`content_text` in the request | Gmail `attachments.get` | ✅ both land as files on disk |
| Attachment priority over body text | ✅ (same rule) | ✅ (same rule) | ✅ `doc_types` + `_find_doc_attachments` |
| PDF processing | ✅ | ✅ | ✅ `ai_service.document_text` |
| OCR (image / scanned PDF) | ✅ | ✅ | ✅ `services/ocr` |
| Classification | ✅ | ✅ | ✅ `workflow.evaluate_email` |
| Extraction | ✅ | ✅ | ✅ `extractor.extract_fields` |
| Shipping ID | ✅ | ✅ | ✅ `versioning.identify_shipment` |
| Shipment grouping | ✅ via `/emails/{id}/process` | ✅ via `process_email` | ✅ same |
| Shipment grouping via `/api/process` | ❌ **verdict only** | n/a | ⚠️ divergence (see §18) |
| SI versioning | ✅ | ✅ | ✅ `register_document_version` |
| BL/Invoice verification | ✅ | ✅ | ✅ `comparison.compare` |
| Missing document detection | ✅ | ✅ | ✅ `shipment_overview` |
| Status calculation | ✅ | ✅ | ✅ `shipment_overview.build_overview` |
| Dashboard/API | ✅ | ✅ | ✅ same read endpoints |

Only two rows differ, both ingestion-source differences rather than logic
differences: the demo click-through endpoint depth, and the Gmail API itself.

## D. Shipment grouping

`versioning.identify_shipment(email, si_fields, bl_fields)` (`versioning.py:97`):

1. `REFERENCE_RE` over a haystack of **subject + body + email_id + attachment
   filenames** → `REF:<code>`;
2. else shipper + port-of-loading + port-of-discharge signature → `SIG:<sha16>`;
3. else `EMAIL:<email_id>`.

Measured priority (`verify_checklist.py`):

| Input | Key produced |
|---|---|
| reference in the subject only | `REF:SHP-AA1` ✅ |
| reference in the **document text only** | `EMAIL:e2` ❌ **document ignored** |
| reference in the filename only | `EMAIL:e3` ❌ (filenames are in the haystack, but `SI_v1.pdf` has no code to find) |
| subject `SHP-DD4`, document `SHP-EE5` | `REF:SHP-DD4` — **subject wins, no conflict flag** |
| no reference, full field signature | `SIG:07131196bbb53d32` |
| nothing at all | `EMAIL:e6` |
| internal `email_id` like `doc-only` | matched as a reference → `REF:DOC-ONLY` ⚠️ |

**Answer to "which source has priority": email subject/body text, then the field
signature, then the email id. The document's own reference is never consulted** —
the extractor produces exactly the 7 compared fields and no reference field, and
`REFERENCE_RE` is applied only to the email haystack (`versioning.py:110`).

**The checklist's "prefer extracted business data" is therefore NOT met for
grouping.** Consequence, demonstrated: two different shipments that share a
shipper and a route but carry no reference text in the mail **collapse into one
key** (`SIG:07131196bbb53d32` for both shipments A and B) → one shipment.

## E. Version control

Ordering source: **`received_at` (Gmail `Date` / `internalDate`) → `created_at`
fallback**, then `id`. Registration order no longer decides anything;
`poll_and_process` also sorts the batch oldest-first.

| Checklist case | Observed |
|---|---|
| **A** `SI_v1/v2/v3` | versions 1/2/3 = 38,900 / 40,150 / 41,500, **v3 CURRENT** ✅ |
| **B** no version token, arrival reversed | versions 1/2/3 = 38,900 / 40,150 / 41,500, **v3 CURRENT** ✅ |
| **C** filename says `SI_v2`, content is the newer revision | name carries **no authority**; the newer-dated content wins; **the conflict is not flagged** |
| **D** same document twice | 2nd row = `DUPLICATE` of the 1st, same version number, `is_latest=0`, **no new version, current unchanged** ✅ |

**No code reads a version token from the subject or the filename** (verified by
scanning `app/services/*.py`). "v1/v2/v3" in a name is cosmetic; numbering is
chronological. An older document arriving late is inserted as history and does
**not** take over as current (acceptance A2).

## F. Verification

- `comparison.compare(si_fields, bl_fields, COMPARED_FIELDS)` — pure dict
  arithmetic, no IO, no OCR. Called from `workflow.evaluate_email` (per-email
  pair) and `versioning.latest_pair_comparison` (latest stored pair).
- 7 compared fields: `shipper, consignee, notify_party, port_of_loading,
  port_of_discharge, container_count, gross_weight_kg`.
- Mismatch is represented as `ReportRecord.status = MISMATCH` +
  `defect_fields` + `field_results[]`, and mirrored into `IssueRecord` rows
  (`sync_issues_for_report`) which `/shipments/{id}/issues` and the resolution
  flow read.
- **Verification never re-extracts.** It reads `DocumentVersionRecord.extracted_fields`.
- Invoice: recognised and versioned, but **never compared field-by-field against
  SI/BL** — by design (`_invoice_fields` stores number/total/currency only).

Normalization is one layer (`extractor.normalize`), consumed by both the
comparison and the version signature. Measured:

| field | inputs | normalized |
|---|---|---|
| gross_weight_kg | `2,450 KG` / `2450 kg` / `2450KG` / ` 2 450 kg ` / `22.5 MT` | `2450.0 / 2450.0 / 2450.0 / 2450.0 / 22500.0` ✅ |
| container_count | `2 x 40'HC` / `2x40HC` / `2 CONTAINERS` | `2 / 2 / 2` ✅ |
| shipper | `ABC Logistics Sdn Bhd` / `ABC LOGISTICS SDN BHD` | `ABC LOGISTICS` ✅ |
| **port_of_loading** | `PORT KLANG, MALAYSIA (MYPKG)` vs `Port Klang, Malaysia` | **different → MISMATCH** ❌ |

That last row is a live false-mismatch generator: the UN/LOCODE is kept on one
side and absent on the other. Full comparison of that pair returned
`status=MISMATCH defects=['port_of_loading','port_of_discharge']`.

## G. Missing documents

- Expected set: `shipment_overview.REQUIRED_DOC_TYPES = ("SI", "BL")`,
  `OPTIONAL_DOC_TYPES = ("INVOICE",)` (`shipment_overview.py:38`).
- Derived, never fabricated: `build_overview` reports
  `documents[type].present` from whether an ACTIVE version exists. SHP-002 live
  shows `BL.present=False, version_count=0` and the reason string
  `"Missing Bill of Lading"` — a shipment *reason*, not a `doc_type`.
- **Missing vs still processing**: `has_any_document` false → `PROCESSING`;
  SI missing → `INCOMPLETE`; required doc missing / mismatch / verdict
  NEEDS_REVIEW → `NEEDS_ATTENTION`; else `VERIFIED`. A shipment that exists but
  is not yet complete is therefore never reported VERIFIED.
- No extra extraction pass: one query over `document_versions` per shipment.

## H. Duplicate processing

| Path | Verdict |
|---|---|
| OCR | **single entry**, `ai_service.document_text`; `ocr_pdf`/`ocr_image` internal | Necessary |
| Extraction | SI + BL each extracted **once** per email (`workflow.py:434-435`) | Necessary |
| Standalone doc extraction | only when the pair is incomplete, on a *different* file | Necessary |
| Invoice text read | once, in `_register_invoice_document` | Necessary |
| `compare()` on a paired email | runs in `evaluate_email` **and** again via `latest_pair_comparison` | **Redundant** (cheap: pure dicts) |
| Classification | once per email | Necessary |
| Shipment grouping | once per email, on write only | Necessary |
| Missing-document detection | derived on read, no processing | Necessary |
| Status calculation | `build_overview` per read (and per row in `/shipments`) | **Redundant at scale** (N+1) |
| Counterpart pairing | re-attaches the same file → records a `DUPLICATE` version row | **Redundant bookkeeping** |
| Gmail re-poll | short-circuited by `GmailMessageRecord.processing_status == "PROCESSED"` | Necessary ✅ |
| Read endpoints | **no processing at all** | ✅ |

No duplicate OCR and no duplicate extraction path exists.

## I. Single source of truth

| Fact | Owner | Stored? |
|---|---|---|
| classification | `ReportRecord.category` | ✅ |
| extracted fields | `DocumentVersionRecord.extracted_fields` (+ `ReportRecord.extracted` for the email view) | ✅ |
| normalized fields | **not stored separately** — derived on demand by `extractor.normalize` | ⚠️ computed |
| verification result | `ReportRecord.status/has_defect/defect_fields/field_results` | ✅ |
| mismatch (structured) | `IssueRecord` (`sync_issues_for_report`) | ✅ |
| shipment status | `shipment_overview.build_overview` (authoritative); mirrored to `ShipmentRecord.status` | ✅ (derived + cached) |
| issue-resolution state | `versioning.resolution_status` → API `resolution_state` | derived |
| version information | `DocumentVersionRecord.version_number / is_latest / previous_version_id` | ✅ |
| missing documents | derived from versions; **no column** | ⚠️ derived |
| source email | `DocumentVersionRecord.email_id` → `EmailRecord` → `GmailMessageRecord` | ✅ |

Normalized fields and missing-documents are recomputed rather than stored. Both
are cheap pure functions over stored data, so this is a deliberate trade, not a
gap — but it is the one place where "single source of truth" is "single
*implementation*" rather than "single stored value".

## J. Bugs and architectural risks

### J1. Shipment grouping ignores the document's own reference — **P1**
- **File / function:** `app/services/versioning.py` → `identify_shipment` (97),
  `_first_reference` (127), `REFERENCE_RE` (31); `app/services/extractor.py`
  (no reference field in `LABELS`).
- **Problem:** the haystack is subject + body + `email_id` + attachment
  filenames. Extracted document text is never searched, and no
  reference/shipment-id field is extracted at all.
- **Impact:** a mail whose only reference is inside the PDF is grouped as
  `EMAIL:<id>` (or by signature), contradicting "prefer extracted business
  data". Real forwarders often put the code in the document only.
- **Fix:** add the SI/BL `raw_text` to the `identify_shipment` haystack (or
  extract a `reference` field in the extractor and pass it in).
- **Priority:** P1.

### J2. Unrelated shipments can merge via the field signature — **P1**
- **File / function:** `versioning.identify_shipment`, tier 2.
- **Problem:** key = sha256(shipper | POL | POD). Two different bookings from the
  same shipper on the same route produce the *same* key. Demonstrated: shipment A
  and shipment B both → `SIG:07131196bbb53d32`.
- **Impact:** two shipments silently become one, mixing their documents and
  versions. This is the checklist's Scenario F.
- **Fix:** include a distinguishing field (container number / vessel+voyage) in
  the signature, and/or prefer the document reference (J1) so the signature is
  rarely reached.
- **Priority:** P1 (P0 if the demo relies on real mail where references are
  absent).

### J3. Port comparison fails when one side omits the UN/LOCODE — **P1**
- **File / function:** `extractor.normalize` for `port_of_loading` /
  `port_of_discharge`.
- **Problem:** `(MYPKG)` is retained, so `PORT KLANG, MALAYSIA (MYPKG)` ≠
  `Port Klang, Malaysia`. Measured: both port fields flagged MISMATCH.
- **Impact:** false MISMATCH on entirely consistent documents. The scored corpus
  always prints codes, so this is invisible today and will appear the moment real
  mail is used in the demo.
- **Fix:** normalise a port to its 5-letter code when present, else the city part
  (text before the first comma). `_PORT_CODE` already exists in the extractor.
- **Priority:** P1.

### J4. The internal `email_id` can be mistaken for a business reference — **P2**
- **File / function:** `versioning.identify_shipment` haystack construction.
- **Problem:** `email_id` like `doc-only` matches
  `[A-Z0-9]{3,8}[-_/][A-Z0-9]{3,8}` → `REF:DOC-ONLY`. Observed.
- **Impact:** internal identifiers become shipment codes; the real corpus already
  contains a `REF:914` of the same family.
- **Fix:** drop `email_id` from the haystack, or require the code shape to have a
  letter prefix (`SHP-`, `OC `, `BL `).
- **Priority:** P2.

### J5. Misleadingly named documents are not discovered by content — **P2**
- **File / function:** `workflow._find_doc_attachments` (790) +
  `app/services/doc_types.detect_si_bl`.
- **Problem:** discovery is name-based. `document_final.pdf` **containing** an SI
  → `_fuzzy_doc_type` returns `None` → SI reported missing.
  Content is used only as a *guard* (`workflow.declared_doc_type` → `_wrong_doc`
  correctly rejects a `.doc` named `_BL` that is really an SI).
- **Impact:** neutrally named scans escalate as `missing_attachment` — the
  reason is wrong, though the escalation itself is safe.
- **Fix:** content-based discovery as a third tier for unrecognised names.
- **Priority:** P2.

### J6. `/api/process` and `/api/v1/ingest` create no shipment rows — **P2**
- **File / function:** `app/routers/integration.py:59`,
  `app/routers/ingest.py:174` vs `workflow.process_email`.
- **Problem:** same verdict, different persistence depth. The ShipMail "Run
  BerthSide" button (`shipmail/frontend/index.html:613`) uses `/api/process`.
- **Impact:** the demo click-through does not exercise grouping/versioning; the
  demo *dataset* loader and the corpus build both do.
- **Fix:** point the demo button at `/emails/{id}/process`, or make `/api/process`
  call the stateful core when a database session is available.
- **Priority:** P2.

### J7. `reports.status` is overwritten by the shipment's then-current pair — **P2**
- **File / function:** `workflow._run_pipeline` (649-667).
- **Problem:** the email's own verdict is replaced by
  `latest_pair_comparison`, so a report is a snapshot of mutable shipment state.
- **Impact:** old emails can display a verdict that later became wrong; the
  shipment API remains self-consistent.
- **Fix:** keep the per-email verdict, store the pair outcome only at shipment
  level (it already is, via issues).
- **Priority:** P2.

### J8. `/shipments` recomputes per row (N+1) — **P2**
- `build_overview` per shipment; fine at 96 shipments, linear growth.
- **Priority:** P2.

## K. Required fixes before the hackathon demo

Ordered by demo risk:

1. **J3 — port normalisation.** Highest demo risk: real mail with inconsistent
   port formatting produces false mismatches in front of judges. Small, contained
   fix in `extractor.normalize`.
2. **J1 — consult the document's reference when grouping.** Makes the business
   identifier authoritative as the checklist requires, and removes most
   dependence on email wording.
3. **J2 — make the signature distinct.** Bounded change (add container/vessel to
   the signature); prevents two shipments merging.
4. **J6 — point the demo button at the stateful endpoint.** Makes the demo show
   grouping + versioning, which is the point of the shipment layer.
5. J4, J5, J7, J8 — quality issues, safe to defer.

## L. What should NOT be changed yet

- **Dashboard layout, navigation, styling** — untouched and out of scope.
- **ShipMail and `/ui/` presentation** — stable; only the minimal read-only
  guard was added previously.
- **The simulated/demo dataset and its loader** — working, covers Scenarios A–E.
- **The scored 520-email corpus and the pipeline that reads it** — any
  normalisation change (J3) must be re-validated against the smoke test's status
  distribution (currently OK 164 / SKIPPED 303 / MISMATCH 34 / NEEDS_REVIEW 32),
  which is why J3 should be paired with that check rather than rushed.
- **The single-verdict implementation** (`workflow.evaluate_email`) — it is
  correct and shared; do not fork it per source.
- **Data model** — no migration needed. It already represents
  Shipment → Document → Version → Source Email; the gaps above are logic, not
  schema.

---

## Verification summary

| § | Item | Status |
|---|---|---|
| 1 | Real Gmail end-to-end pipeline | **PARTIALLY WORKING** — all stages verified except the Gmail API itself (**NOT VERIFIED IN CURRENT ENVIRONMENT**) |
| 2 | Source preservation | **PARTIALLY WORKING** — recipient NOT IMPLEMENTED; 3 emails stay 3 ✅ |
| 3 | Shipping ID detection | **PARTIALLY WORKING** — J1, J2, J4 |
| 4 | Three SI versions → one shipment | **VERIFIED WORKING** |
| 5 | Version detection, cases A–D | **VERIFIED WORKING** (C: no conflict flag) |
| 6 | Document classification | **PARTIALLY WORKING** — name-based discovery, content as guard (J5) |
| 7 | OCR / extraction / field sets | **VERIFIED WORKING** — no 7-vs-9 mismatch |
| 8 | Normalization | **PARTIALLY WORKING** — J3 |
| 9 | Verification logic | **VERIFIED WORKING** |
| 10 | Missing-document detection | **VERIFIED WORKING** |
| 11 | Shipment status | **VERIFIED WORKING** — one authority |
| 12 | Duplicate processing | **No duplicate OCR/extraction**; 3 redundant-but-cheap paths (H) |
| 13 | Dashboard refresh safety | **VERIFIED WORKING** — 50 reads, zero writes |
| 14 | Source traceability | **VERIFIED WORKING** — 2 hops, no direct doc→message_id FK |
| 15 | Simulated vs Real Gmail | comparison table in §C — parity except ingestion depth |
| 16 | End-to-end real Gmail send/sync | **NOT VERIFIED IN CURRENT ENVIRONMENT** |
| 17 | Data model / SSOT | **VERIFIED WORKING** — already Shipment→Document→Version→Source |
| 18 | API audit | 54 endpoints (15 writes, 39 reads) — no read triggers processing |
| 19 | Demo scenarios A–F | **A–E VERIFIED**; **F VERIFIED for reference-bearing mail**, J2 is the caveat |

### Field-set clarification (§7, explicitly requested)

**There is no 7-vs-9 mismatch.**

| Constant | Count | Contents |
|---|---|---|
| `extractor.LABELS` | **9** | the 7 compared fields + `etd` + `eta` |
| `extractor.COMPLETENESS_FIELDS` | **7** | compared fields |
| `schemas.COMPARED_FIELDS` | **7** | compared fields |
| `schemas.INFO_COMPARED_FIELDS` | **2** | `etd`, `eta` — extracted and displayed, **never** in the verdict |

`etd`/`eta` are deliberately excluded from completeness: including them would mark
every document incomplete and fire the OCR escalation gate (the extractor's own
comment says so, and the corpus prints neither field). `scripts/smoke_test.py:96`
asserts `len(fields) == 7` on `report.field_results`, which is the 7 comparison
rows — correct, not a stale assumption. Live check on `email_001`:
`field_results` = exactly the 7 compared fields; `extracted.etd/eta = None`;
status OK.

### Not verified in this environment

The Gmail API (list/get/attachments, OAuth, token refresh, pagination,
`historyId` deltas, rate limits/back-off); a live send→sync→verify run;
PostgreSQL concurrency (all testing was SQLite, single process); genuine legacy
`.doc`/`.xls` files; the `/ui/` "not processed yet" branch rendered in a browser;
Gmail's real `messages.list` ordering (the fix removed the dependency, but the
assumption itself was never confirmed live).
