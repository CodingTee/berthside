# J1 / J2 / J3 / J6 — Targeted Fix Report

Scope: **four issues only** (port normalization, document reference extraction,
false-merge prevention, ShipMail "Run BerthSide" persistence). J4/J5/J7/J8 were
**not** touched. No Dashboard / Gmail UI / demo dataset / ShipMail UI changes.

---

## 1. Files changed (this round)

| File | Change |
|---|---|
| `app/services/extractor.py` | J3 — new `_port_name()` + `ports_match(a, b, raw_a, raw_b)` rule |
| `app/services/comparison.py` | J3 — `_match_port` uses the raw values + `ports_match` |
| `app/services/identifiers.py` | **NEW** — J1/J2: reads `Booking No` / `Shipment ID` / `OC No.` / `B/L No.` / container / vessel+voyage from document text |
| `app/services/versioning.py` | J1/J2 — `identify_shipment` now takes the document's own reference + discriminators; defensive timezone-safe `_chronology_key` |
| `app/routers/ingest.py` | J6 — `analyze_email` returns the already-computed `verdict` |
| `app/services/workflow.py` | J6 — `_run_pipeline` accepts a pre-computed verdict |
| `app/routers/integration.py` | J6 — `/api/process` persists attachments + runs grouping/versioning after the verdict |
| `tests/test_comparison.py` | J3 — updated port rule expectations + new cases |
| `tests/test_identifiers.py` | **NEW** — J1 (8 tests) |
| `tests/test_shipment_grouping.py` | **NEW** — J2 (11 tests) |
| `tests/test_process_endpoint_persists.py` | **NEW** — J6 (4 tests) |

## 2. What changed per issue

- **J3** — `ports_match` now: identical strings match; when **both** sides print a
  UN/LOCODE the code decides; when only one side prints a code the **names** are
  compared (a missing code is formatting, not a routing change). The LOCODE is read
  from the raw value because normalization drops the parentheses. "conflicting
  codes" still MISMATCH; "genuinely different names" still MISMATCH.
- **J1** — `identify_shipment` searches mail subject/body/filename first, then the
  **document's own reference** (via `identifiers`), then a field signature, then
  email id. The internal `email_id` is no longer fed to the reference search, so
  `REF:DOC-ONLY`-style keys cannot happen.
- **J2** — the tier-2 signature is now `shipper | POL | POD | container | vessel+voyage`
  when the documents carry them, so two bookings on the same route no longer collapse.
- **J6** — `POST /api/process` runs the verdict once, then persists the attachments
  and executes shipment grouping/versioning, so `/shipments` immediately shows the
  resulting shipment. No second extraction is performed.

## 3. Before / after smoke distribution

**Live `by_status`** (what `smoke_test.py` check #7 prints):

```
before  OK 164  SKIPPED 303  MISMATCH 34  NEEDS_REVIEW 32   (533 reports)
after   OK 167  SKIPPED 303  MISMATCH 33  NEEDS_REVIEW 31   (534 reports)
```

**That delta is not caused by the four fixes.** Controlled A/B on a clean
520-email corpus (fresh scratch DB, toggling each fix):

```
J3 off   -> OK 154  SKIPPED 300  MISMATCH 46  NEEDS_REVIEW 20
J3 on    -> OK 154  SKIPPED 300  MISMATCH 46  NEEDS_REVIEW 20   (identical)
J1/J2 off-> OK 154  SKIPPED 300  MISMATCH 46  NEEDS_REVIEW 20   (identical)
J1/J2 on -> OK 154  SKIPPED 300  MISMATCH 46  NEEDS_REVIEW 20   (identical)
```

So **J1, J2 and J3 change the corpus verdict distribution by zero**; J6 is
irrelevant to the corpus (the build uses `workflow.process_all`, not
`/api/process`). The small live-DB movement is from (a) one demo email
(`shipmail-demo-900`) added during J6's live verification, and (b) the live DB
holding **stale** corpus reports built by earlier code (process-once dedup
preserves them).

> Transparency note: a clean rebuild of the current code yields `154/300/46/20`
> for the 520 corpus, while the stored corpus reports read `161/300/32/27`. That
> gap **predates this round** (it comes from the round-1 extractor/versioning
> changes, not J1–J6) and is masked by the process-once cache. It is out of scope
> here and worth a separate look before a forced corpus rebuild.

## 4. Test results

```
pytest (full suite)              164 passed, 0 failed
  test_identifiers.py             8 passed   (new, J1)
  test_shipment_grouping.py      11 passed   (new, J2)
  test_process_endpoint_persists  4 passed   (new, J6)
  test_comparison.py             16 passed   (J3 updated)
scripts/verify_demo_shipments    42 passed, 0 failed
scripts/audit_gmail_versions     51 passed, 0 failed   (A–G still green)
scripts/smoke_test (520)          9 passed, 0 failed
```

## 5. Acceptance checks

- **A–G 51/51 remains green** — confirmed (the J1/J2/J3/J6 changes do not touch
  the Gmail sync or versioning paths those tests exercise).
- **No unrelated UI files modified** — `backend/web/index.html` (mtime 19:59) and
  `backend/shipmail/frontend/index.html` (mtime 17:57) are both from earlier
  rounds; this round touched only the backend `.py` and test files listed above.

## 6. Remaining limitations (not verified in this environment)

Real Gmail API is still stubbed (OAuth/token refresh, pagination, `historyId`
incremental sync, rate limits, real send-then-sync end-to-end). OCR binaries are
absent locally, so image documents degrade to the escalate path. PostgreSQL
concurrency and legacy `.doc`/`.xls` are untested.
