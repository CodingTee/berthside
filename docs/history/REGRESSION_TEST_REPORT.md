# ShipSync — Fixes + Real-Gmail Regression Report

The five confirmed issues (B1, B2, B3, GET side effects, Gmail Date) are
implemented. This round adds the **focused pytest regression test that drives the
real `gmail/sync.py` code path with a stubbed Gmail API**, and closes the
acceptance criteria that were not yet explicitly asserted.

No Dashboard, Gmail-UI, architecture, naming or feature change was made.

---

## 1. Which files were changed

Production code was changed **only in the previous round**; this round added
tests. The table shows both, with the markers that prove each fix is in the code.

| File | Change | Present since |
|---|---|---|
| `app/integrations/gmail/sync.py` | oldest-first batch (`_fetch_pending`, L65/103), attachment priority (`to_materialize`, L358-366), `received_at` write (L206) | previous round |
| `app/services/versioning.py` | `_chronology_key` (L252), `_reindex_versions` (L267), authoritative status write (L569) | previous round |
| `app/services/workflow.py` | real attachments before synthetic (`real + synthetic`, L761/770) | previous round |
| `app/services/doc_types.py` | **new** — single document-kind rule used by both layers | previous round |
| `app/integrations/gmail/parser.py` | `_received_iso` (L50) → `metadata["received"]` (L45) | previous round |
| `app/routers/frontend_compat.py` | read-only `GET /api/emails/{id}` (L154 docstring, L202 `processed` flag) | previous round |
| `app/routers/shipments.py` | `status` = authoritative, `resolution_state` split out (L58) | previous round |
| `app/schemas.py` | `resolution_state` field | previous round |
| `web/index.html` | minimal guard for the read-only response + explicit process action | previous round |
| **`tests/test_gmail_sync_versions.py`** | **NEW — focused regression test, 9 tests** | **this round** |

No production file in `app/` was modified this round.

## 2. The five fixes

**B1 — version ordering.** Versions are numbered by chronology, not arrival:
`_chronology_key = (received_at or created_at, id)`, and `_reindex_versions`
renumbers a document's ACTIVE versions 1..n with the highest marked current and
`previous_version_id` chaining forwards. `poll_and_process` also fetches and
parses the whole batch and processes it **oldest-first**, so Gmail's
newest-first return order cannot influence anything. A late-arriving *older*
document is inserted as history and does not take over as current.

**B2 — real attachment priority.** `doc_types` is now the single interpretation
of a filename (`SI` / `BL` / `INVOICE` / `None`, ambiguous names refused), used by
both the Gmail layer and the engine. `_materialize_inline_documents` synthesises a
body document **only for a kind no real attachment covers**, and names it
`<shipment>_<kind>.frombody.txt`; `_find_doc_attachments` walks `real + synthetic`
so a synthetic file can fill a gap but never displace a real attachment.

**B3 — missing-BL status.** `shipment_overview.build_overview` is the single
source of truth, and `_update_shipment_status` now persists *that* value instead
of the issue-count-only `resolution_status` (which called a missing-BL shipment
VERIFIED). `ShipmentSummaryOut.status` carries it; the issue bookkeeping is
exposed separately as `resolution_state`.

**GET side effects.** `frontend_compat.email_detail` no longer calls
`workflow.process_email`. It returns the email with `result: null` and
`processed: false`; processing requires `POST /emails/{id}/process`.

**Gmail Date.** `parser._received_iso` reads the `Date` header via
`email.utils.parsedate_to_datetime`, applies the offset, falls back to
`internalDate`, never raises, and treats a zoneless header as UTC. `sync` stores
it on `EmailRecord.received_at` (normalised to naive UTC) *before* the pipeline
runs, so version chronology has a real signal.

## 3. New tests added

**`tests/test_gmail_sync_versions.py`** — 9 tests. Every test stubs only
`gmail_client.list_message_ids` / `get_message` / `get_attachment`; classification,
attachment handling, extraction, versioning, verification and grouping are all
production code, reached through `sync.poll_and_process`.

| Test | What it asserts |
|---|---|
| `test_three_gmail_emails_become_one_shipment_with_three_si_versions` | **The headline acceptance test, in Gmail's real newest-first order.** Three separate `EmailRecord`s + three `GmailMessageRecord`s (message id, thread id, `PROCESSED`); each report classified `BL_COMPARISON` with `NEEDS_REVIEW` / `missing_attachment`; **exactly one** shipment `REF:SHP-001`; three SI versions numbered 1/2/3 with weights 38,900 / 40,150 / 41,500; exactly one `is_latest` and it is v3; v1/v2 content intact and not duplicates; `previous_version_id` chains forwards; per-version filename and source `email_id` preserved; `received_at` set on every version; `shipper` read from the attachments (the bodies carry no field data, so a body-derived document would extract nothing); `build_overview` → `complete=False`, `missing_documents=['BL']`, `NEEDS_ATTENTION`; a **second poll processes 0, skips 3, creates nothing** |
| `test_versions_are_chronological_even_from_oldest_first` | Same result when Gmail returns oldest-first → order is irrelevant |
| `test_a_late_arriving_older_email_does_not_replace_the_current_version` | A 35,000 KG document dated 17 Sep lands as version 1 while v3 stays current |
| `test_missing_bl_keeps_the_shipment_incomplete` | SI only → `NEEDS_ATTENTION`, `complete=False`, `missing=['BL']`, `REQUEST_BL`, **no BL row created**, `_versions(..., "BL") == []` |
| `test_grouping_does_not_depend_on_the_classified_intent` | An SI-only mail that reads as a plain request (`SI_REQUEST`) still produces the shipment, and still reports the BL missing |
| `test_get_endpoints_do_not_mutate_state` | 3 × 7 read endpoints (incl. an **unprocessed** email) → reports / versions / shipments / `attempts` / `processing_ms` / version numbers all unchanged; the unprocessed email stays unprocessed and the API says `processed: false` |
| `test_gmail_date_header_is_parsed_into_received_at` | `+0800` header → `2026-09-18T01:00:00`, stored naive, version timestamps chronological |
| `test_gmail_internal_date_is_the_fallback_and_bad_dates_are_survivable` | `internalDate` fallback, malformed header tolerated, missing header tolerated, zoneless = UTC, `-0700` offset applied, and a no-date message still ingests |
| `test_a_real_pdf_attachment_wins_over_the_email_body` | A **real PDF** attachment whose body quotes the full SI text → stored filename is the sender's `SI_v1.pdf`, no `frombody` version, fields extracted from the PDF (guarded by `importorskip("fitz")`) |

Also present and still passing (added in earlier rounds):
`scripts/audit_gmail_versions.py` — acceptance A–G, 51 assertions, same stubbed-
Gmail technique plus the file-format and OCR probes.

## 4. Test results

| Suite | Result |
|---|---|
| `pytest tests/` (full) | **137 passed, 0 failed** (128 before + 9 new) |
| `scripts/audit_gmail_versions.py` (A–G) | **51 passed, 0 failed** |
| `scripts/verify_demo_shipments.py` (TEST 1–10) | **42 passed, 0 failed** |
| `scripts/smoke_test.py` (520-email corpus) | **9 passed, 0 failed**, 520/520 emails, 0 pipeline errors |

No test was weakened or deleted. One assertion in the new test was corrected
during development: I had asserted `category == "BL_COMPARISON"` for a fixture
body that contained no "check/verify" verb, so the classifier correctly returned
`SI_REQUEST`. Rather than relax the assertion I made the fixture wording
unambiguous **and** added `test_grouping_does_not_depend_on_the_classified_intent`
to cover the `SI_REQUEST` path explicitly — strictly more coverage than before.

## 5. Acceptance criteria — all met

| Criterion | Result |
|---|---|
| 3 separate Gmail emails | ✅ 3 `EmailRecord` + 3 `GmailMessageRecord` |
| 3 correctly preserved SI versions | ✅ numbered 1/2/3, all ACTIVE, content intact |
| 1 shipment | ✅ `REF:SHP-001`, and a second poll creates nothing |
| v3 is current | ✅ exactly one `is_latest`, on v3 |
| v1/v2/v3 remain accessible | ✅ individually retrievable with filename + source email |
| missing BL correctly reported | ✅ `NEEDS_ATTENTION`, `complete=False`, `missing=['BL']`, no fake row |
| GET requests do not mutate state | ✅ 21 read calls, zero state change |
| existing functionality intact | ✅ 137 + 51 + 42 + 9 green; corpus status distribution unchanged (OK 164 / SKIPPED 303 / MISMATCH 34 / NEEDS_REVIEW 32) |

## 6. Not changed

Dashboard layout / navigation / styling, the Gmail UI, `/ui/` presentation, the
demo dataset and its loader, the scored 520-email corpus and the pipeline that
reads it, the single shared verdict implementation
(`workflow.evaluate_email`), and the data model (no migration — it already
represents Shipment → Document → Version → Source Email).

The four P1/P2 issues from the verification report (document-reference grouping,
signature collision, port-code normalisation, demo button endpoint depth) are
**not** part of this round's scope and remain open, documented in
`VERIFICATION_REPORT.md` §J.
