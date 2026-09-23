# Shipment-Centred Data Layer — Iteration Report

Scope rule honoured: **the Dashboard layout, navigation, page structure and
overall frontend UI were not touched.** `backend/shipmail/frontend/index.html`
was not modified in this iteration. All work is backend, data/model, document
processing, demo dataset and API.

---

## 1. Files modified

| File | Change |
|---|---|
| `backend/app/services/workflow.py` | Lone SI/BL now files its own shipment before the invoice is registered; `DOCUMENT_CARRYING_CATEGORIES`; invoice can attach to an existing shipment |
| `backend/app/services/versioning.py` | New `register_standalone_document()`; `_normalized_hash()` no longer collapses every invoice into one signature |
| `backend/app/services/ai_service.py` | New `_xml_tag_to_label()` so camel-case XML tags match the extractor's label rules |
| `backend/app/services/inbox_service.py` | DB-fallback `get_email()` now returns `received_at` |
| `backend/app/routers/ingest.py` | ZIP expansion shared by analyse + ingest; honours `metadata["received"]` |
| `backend/app/routers/shipments.py` | `/shipments/overview`, `/shipments/{id}/overview`, `/shipments/by-key/{code}`; extended summaries |
| `backend/app/schemas.py` | `ShipmentSummaryOut` extended; new `ShipmentOverviewOut` / `ShipmentOverviewListOut` / `ShipmentDocumentOut` / `ShipmentSourceEmailOut` |
| `backend/Dockerfile` | Loads the curated demo shipments at build time |

## 2. Files added

| File | Purpose |
|---|---|
| `backend/app/services/shipment_overview.py` | Shipment-level reading of stored data (completeness, current version, issues, source emails, actions) |
| `backend/app/services/archive.py` | Safe ZIP expansion for attachments |
| `backend/scripts/build_demo_shipments.py` | Generates the curated demo dataset |
| `backend/scripts/load_demo_shipments.py` | Loads it through the real routes (`--spawn` for the Docker build) |
| `backend/scripts/verify_demo_shipments.py` | TEST 1–10, currently 42/42 |
| `backend/demo-shipments/` | `emails.json`, `attachments/` (18 real documents), `expectations.json` |

## 3. Files deleted

None.

## 4. Backend changes

- **A shipment now exists as soon as any one document arrives.** Previously the
  pair was the unit of existence, so a shipment whose BL never came was
  invisible — and "Missing BL" had nothing to report against.
- **Shipment-level status** derived from stored rows only:
  `PROCESSING` / `INCOMPLETE` / `NEEDS_ATTENTION` / `VERIFIED`, with
  `reasons`, `missing_documents`, `mismatch_fields`, `actions`.
  Missing documents are **shipment reasons, never document types** — no fake BL
  row is created.
- **Reads never re-process.** All shipment endpoints read
  `document_versions` / `reports` / `issues`. Verified: 9 shipment reads in
  ~1.2 s changed no `attempts` counter and no `processing_ms` — no OCR, no
  extraction, no verification.
- **ZIP attachments are expanded before classification** (member/size caps, path
  traversal rejected, nested archives refused, each member re-checked by the
  existing safety gate). Members become ordinary attachments, so doc-type
  detection, pairing and versioning keep working unchanged.
- **INVOICE** is now versioned alongside SI/BL, and an invoice-only email
  attaches to an existing shipment (it never invents one).
- Two extraction bugs fixed: camel-case XML tags never matched any label rule
  (`<PortOfLoading>` → null), and every document without compared fields (every
  invoice) shared one normalised signature, freezing invoice version numbers
  at 1.

## 5. Frontend changes

**None.** The Dashboard, ShipMail and navigation are untouched; `/shipmail/`,
`/ui/` still serve 200. Frontend work is deferred to the next iteration as
instructed.

## 6. Data/model changes

No new tables. The existing `shipments` / `documents` / `document_versions` /
`issues` / `resolutions` model already represented the hierarchy; what was
missing was the shipment-level view over it and the rule that made a shipment
exist. `documents.doc_type` now also carries `INVOICE`.

```
Shipment ──┬── SI       (versions 1..n, one current)
           ├── BL       (versions 1..n, one current)
           ├── INVOICE  (versions 1..n, one current)
           ├── verification        (SI vs BL, per-field evidence)
           ├── issues / mismatches
           ├── missing_documents   (shipment state)
           ├── actions             (REQUEST_BL / REQUEST_CORRECTION / …)
           └── source_emails       (sender, subject, received, attachments)
```

## 7. New demo/test data

`backend/demo-shipments/` — deliberately separate from the scored 520-email
bundle, which is unmodified.

| Shipment | Scenario | Documents | Result |
|---|---|---|---|
| SHP-001 | complete | SI.pdf, **BL.pdf (2 pages)**, Invoice.pdf | VERIFIED |
| SHP-002 | missing BL | SI.csv, Invoice.xlsx, *no BL ever* | NEEDS_ATTENTION · REQUEST_BL |
| SHP-003 | 3 SI versions | SI v1 → v2 → v3 (.txt/.docx), BL, Invoice | VERIFIED (BL matches v3 only) |
| SHP-004 | mismatch | SI.xml, BL.txt, Invoice.docx | MISMATCH `container_count` (3 vs 4) |
| SHP-005 | many emails | ZIP(SI.pdf + BL.pdf + Invoice.xlsx) + invoice scan from a 2nd sender | VERIFIED, 2 senders, invoice v2 |
| SHP-006 | OCR | SI.txt + scanned **TIFF** BL | NEEDS_REVIEW `possible_match` |

## 8. New file formats actually supported

Verified by reading each file back through the real reader — nothing claimed
that was not observed:

| Format | Reader | Verified |
|---|---|---|
| TXT | text | ✅ |
| CSV | `csv` (quoted, label\|value) | ✅ |
| XML | `xml` (camel-case tag → label) | ✅ |
| XLSX | openpyxl | ✅ |
| DOCX | python-docx | ✅ |
| PDF | pypdf → pdfplumber → OCR ladder | ✅ (incl. multi-page) |
| PNG / JPEG / TIFF | rapidocr OCR | ✅ |
| ZIP | safe expansion → members re-read | ✅ |

**Not claimed:** legacy `.doc` (OLE2) and `.xls` — readers exist and the
libraries are installed, but no genuine legacy sample was available to prove
them, so no demo file fakes success for either. OCR-off environments report
images honestly as `unreadable`.

## 9. Tests performed

| Gate | Result |
|---|---|
| `pytest tests/` | **128 / 128 passed** |
| `scripts/smoke_test.py` (520-email corpus) | **9 / 9 passed**, 520/520 emails, 0 pipeline errors |
| `scripts/verify_demo_shipments.py` (TEST 1–10) | **42 / 42 passed** |
| Live pages `/shipmail/`, `/ui/`, `/health`, `/shipments/overview` | 200 |

TEST 1 complete shipment · TEST 2 missing BL · TEST 3 SI versions · TEST 4
mismatch · TEST 5 multiple emails/senders · TEST 6 file formats (all real reads)
· TEST 7 multi-page (shipper is only on page 2 of the BL) · TEST 8 no
re-processing on read · TEST 9 SHP-003 payload completeness · TEST 10 SHP-002
missing-BL payload shape.

## 10. Remaining limitations

1. **No shipment UI yet** — intentional. The API is populated and stable;
   the Layout work is the next phase.
2. **Legacy `.doc` / `.xls` unverified** — see §8.
3. **A shipment is only created for `BL_COMPARISON` / `SI_REQUEST` emails.**
   A stray SI attached to a `GENERAL` mail is not filed, to avoid reading
   documents out of unrelated mail.
4. **Shipment identity follows the reference in the subject/body/filenames.**
   When no reference exists it falls back to a shipper+port signature, then to
   the email id — so reference-free mail can still split into two shipments.
5. **SHP-006 is expected to need review.** A 140 dpi scan mis-reads a character
   (`GDANSK TIMBER SPZOO`), which the engine reports as `possible_match` rather
   than guessing. That is the designed behaviour, not a defect.
6. **`/api/process` runs the stateless core** and creates no shipment rows; the
   stateful pair is `/api/v1/ingest` + `/emails/{id}/process`. Worth
   remembering when wiring the future Dashboard.
