# ShipSync: Architecture and Design Notes

This document explains *why* the engine behaves the way it does: determinism
rules, the noise-robustness contract, human-review behaviour, status semantics
and the AI integration contract. For how to run, test and deploy the project,
see DEVELOPMENT.md.

## Determinism: non-negotiable

`app/services/comparison.py` compares normalized values, never asks an LLM:

- `container_count`: parse `6 x 40'HC` → `6`
- `gross_weight_kg`: parse `131,058 KG` → `131058.0`
- ports: UN/LOCODE (`NANTONG, CHINA (CNNTG)`) decides; else normalized name
- names: uppercased, punctuation stripped, legal suffixes removed, address cut

`SI container_count = 3` vs `BL = 4` → `3 != 4` → **MISMATCH**. End of story.

## Messier-input robustness (advanced stage)

The extractor aligns fields by *meaning* via synonym label sets, not exact
headers, so `Load Port` ≡ `Port of Loading`, `Cnee` ≡ `Consignee`, and weight
strings like `22,000 kg` / `22000kgs` / `22.000 KG` all normalize the same.
This is what the advanced stage scores: recognising that two documents express
the same field differently, and telling a real discrepancy from a formatting
difference.

## Robustness: noise that must not change the answer

```bash
python scripts/stress_evaluate.py      # ~30 s, uses the official scorer
```

Injects six meaning-preserving perturbations into the 94 SI/BL text pairs and
re-scores each perturbed submission. No gold answers are read; only the
aggregate official score.

| perturbation | verdict changes | fields lost | score |
|---|---|---|---|
| random whitespace / tabs | 0 | 0 | 1.0 |
| non-breaking spaces (PDF artefact) | 0 | 0 | 1.0 |
| blank line between every line | 0 | 0 | 1.0 |
| ALL-CAPS document | 0 | 0 | 1.0 |
| whole document on one line | 10 | 0 | 1.0 |
| reworded field labels | 0 | 0 | 1.0 |

Four real bugs came out of this harness; none were visible on clean data:
whitespace-variant labels, weight glued to following text (21 577 kg → 21 kg),
a collapsed document losing six of seven fields, and `Notify Party/Intermediate
Consignee` being split as if it were two fields.

Known remaining limitation: when line breaks are lost entirely, company names
absorb the address line. SI and BL degrade symmetrically, so end-to-end is
unaffected.

## Reliability & human review (advanced stage)

- When a document is **unreadable**, a value is **missing**, the sender attached
  the **wrong document**, or the result is **uncertain**, the case is escalated to
  `NEEDS_REVIEW` **with the reason and the partial evidence** (which attachment is
  missing, what was extracted); a person confirms or corrects it, and the report
  is updated.
- **Visible failures:** AI-service outages / timeouts are caught, surfaced as a
  clear status + error text, and retried with exponential backoff (never silent).
- `POST /emails/{id}/process` is idempotent: calling it again is the retry.
- **Review queue grouped by reason:** `GET /api/review-queue` buckets
  escalations by `review_reason`; the ops board at `/ops/` renders them.

### Finding the SI and BL among the attachments

Two tiers, strictly ordered. Tier 1 reads the naming convention this corpus
uses (`email_123_SI.pdf`). Tier 2 runs **only** when tier 1 left a gap, and reads
the names senders actually write: `Draft_BL_v2.pdf`,
`Shipping_Instruction_PO123.pdf`, `SI.xlsx`, `BL-102938.pdf`. A name is accepted
only when it names exactly one of the two types, never when it names both or
names neither, and tier 1 always wins.

Without tier 2 an unrecognised name is indistinguishable from an absent file, so
the reviewer is handed `missing_attachment`. That blames the sender for a file
that was attached, and the reason on an escalation is exactly what the
reliability axis measures. The corpus never exercises tier 2 (all 250
attachments follow the convention), so this is not a scoring fix. It is what
keeps the escalation reason honest on any other inbox.

### Scanned documents: OCR reads them, humans confirm them

Of the 58 non-txt documents, 8 (emails 511–515) have **no embedded text
layer**: they literally say `SCANNED COPY - NO TEXT LAYER`. Two are also
truncated, so nothing can recover those; the rest are read by OCR.

The PDF reader is an escalating ladder: each rung runs only when the previous
one recovers too few of the 7 compared fields:

| Rung | Reader | Handles |
|---|---|---|
| 1 | `pypdf` text layer | machine-generated PDFs (exact, fast) |
| 2 | `pdfplumber` tables/layout | PDFs whose content is a **table** (pypdf scrambles cell order) |
| 3 | `pypdfium2` + OCR | **scanned / image-only** PDFs |
| - | escalate `unreadable` | truncated / genuinely unrecoverable files |

**OCR output is not trusted blindly.** Transcription errors (`VALPARAISO` →
`VALPARAISQ`, `CHINA` → `CHIMA`) look exactly like real discrepancies. So any
document read by an OCR rung (`pdf-ocr`, `image-ocr`) that is missing a compared
field is escalated with the transcription attached as evidence, rather than
silently compared:

```
scanned PDF → pypdf yields nothing → OCR transcribes → fields incomplete
            → NEEDS_REVIEW (evidence: OCR text + which fields are unreadable)
```

The test is on the reader, not on the file type: the same incomplete
transcription must reach a human whether it came from a scanned PDF or a photo
of an SI.

Every reader is optional. If `pdfplumber` / the OCR engine are not installed
the ladder degrades to rung 1 and the document escalates as `unreadable`,
never a wrong answer.

Legacy `.doc` (Word 97-2003) is the one reader that cannot be exact: the file is
an OLE container mixing binary structures with text, so any reading of it is an
interpretation. Word stores that text as UTF-16LE, and decoding the stream as
latin-1 leaves a NUL between every character, which used to yield a document
that counted as readable with all seven fields empty. The reader now tries both
decodings, keeps whichever one the extractor can actually parse, and requires at
least 2 of the 7 fields as evidence before trusting it. Below that it returns
nothing and the case escalates as `unreadable`: a value invented out of binary
noise is indistinguishable from a real discrepancy.

Measured: `OCR_ENABLED=0` and `OCR_ENABLED=1` produce **identical output on all
520 emails**. Not just the same status and `defect_fields`: the same
`review_reason` too, with 0 emails differing in any of the three. The switch
buys time, not accuracy. 2.1 s versus 23.0 s for the corpus.

**One switch, obeyed by both readers.** `ocr_available()` is the only thing that
reads `OCR_ENABLED`, and it reads it live. Two bugs lived here: the PDF rung was
gated by an import-time snapshot of the settings (so a change after startup did
nothing), and the image path consulted no switch at all, transcribing images
with `OCR_ENABLED=0` set. The container said OCR off and ran OCR anyway.

**Resolution is a measured choice, not a default.** Rendering at 300 DPI costs
about 4 s more than 200 DPI, and 200 DPI loses 1 to 2 of the 7 compared fields on
4 of the 6 scanned PDFs while fragmenting the heading into
`SHIPPING INSTRUC TION`. The transcription is the evidence a human has to verify,
so the fields are worth more than the seconds. `_RENDER_DPI` stays at 300; see
`tests/test_ocr_controls.py` before lowering it.

**Where the time actually goes.** Rendering is not the cost. Of the OCR work
inside a full run, page rendering is about 1.4 s for 8 render calls, and ONNX
inference on the 6 scanned pages is the rest (measured at 20 to 30 s depending
on machine load). Engine construction was a third cost that should not have
existed: `RapidOCR()` was rebuilt on every call, 9 times per run at about 0.6 s
each. Caching the engine and renderer probes and building the engine lazily
(a corrupt PDF no longer loads models it cannot use) is what took the end-to-end
OCR premium from about 63 s to the 21 s measured above.

Inference is already the cheapest honest configuration. Concurrency does not
help: threads and processes measured 0.86x to 0.99x, because inference already
saturates the CPU. Page count is capped at 20, which costs nothing on a corpus
where nothing exceeds one page and bounds a pathological input.

### The attachment security gate

`POST /api/v1/analyze` and `POST /api/v1/ingest` are externally reachable, so
every attachment passes `services/security.py` before anything opens it. It
refuses executables and scripts (by extension), a dangerous label sitting
directly before a document extension (`Draft_BL.exe.pdf`), real PE/ELF/Mach-O
bodies regardless of name, and a `.pdf` or `.docx` whose container header does
not match its extension.

Two decisions there are deliberate:

* **There is no whitelist of legitimate extensions.** Refusing everything
  unusual turns an unsupported format into a security incident, while the
  reader ladder already escalates it honestly as `unreadable`. The gate refuses
  a file for what it *is*, not for being unfamiliar.
* **The size cap is enforced on the encoded payload**, before it is decoded.
  base64 expands three bytes into four characters, so the decoded size is known
  from the length alone; decoding first would spend the memory the cap exists to
  protect.

A quarantine verdict also means the payload is **not persisted**: `/ingest`
returns `persisted: false` instead of writing the bytes to `INGEST_DIR`.

The gate guards the external entry points only. The corpus pipeline
(`workflow.evaluate_email`) reads its own trusted dataset and is unaffected, so
a change here cannot move the score.

## Statuses

| Status | Meaning |
|---|---|
| `OK` | BL_COMPARISON, all 7 fields match |
| `MISMATCH` | ≥1 field differs → `has_defect`, `defect_fields` |
| `NEEDS_REVIEW` | `missing_attachment` · `unreadable` · `missing_value` · `wrong_doc_type` (with evidence) |
| `SKIPPED` | Not a comparison request: classification only |
| `ERROR` | Pipeline exception; message stored, re-`POST` to retry |

### Why the escalation reason is what it is

The four reasons answer four different questions, and the reviewer's next action
depends on getting the right one:

| reason | what it means | what the reviewer does |
|---|---|---|
| `missing_attachment` | the SI/BL pair is not in the email | ask the sender to attach it |
| `wrong_doc_type` | the file **names itself** as something else | ask the sender for the right document |
| `unreadable` | we could not read the file | escalate to OCR / a human transcription |
| `missing_value` | read fine, but a field is not in it | weigh the gap in the comparison |

`wrong_doc_type` rests on a **self-declaration read from the heading**, so a
document is only called the wrong type when it says so. Five corpus emails
qualify: two carry a `PACKING LIST` in the BL slot, two a `CERTIFICATE OF
ORIGIN`, one a `COMMERCIAL INVOICE`, and the escalation now names the file for
the reviewer.

Two corpus facts shaped that rule. Ten PDFs are titled `BILL OF LADING
INSTRUCTION` and sit in the **SI** slot: they are shipping instructions, so the
instruction forms are matched before the plain ones, or a naive
"mentions BILL OF LADING" test would call all ten the wrong type. And 15
documents carry no heading at all (spreadsheet exports), so a silent heading is
not a finding and those are still compared on their fields.

The rule used to read `not res.fields or (...)`, which filed a PDF nobody could
open under the same reason as a packing list sent in place of a bill of lading.
Those two need opposite follow-up, and ground truth separates them: the
unopenable ones are `unreadable`. Measured after the change: `review_reason`
agrees with ground truth on **20/20** escalated emails (was 14/20), with
`defect_fields` still exact on 520/520.

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

