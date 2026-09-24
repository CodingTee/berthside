# API Contract — v1 (FROZEN at MVP freeze, 20 Sep)

Frontend (P1) and AI (P3) build against this document. Field names and status
values are frozen until after submission; additive changes only.

Base URL (dev): `http://127.0.0.1:8000` · Local UI: `/ui/`
Interactive docs: `/docs` · All responses: `application/json`

---

## GET /health

```json
{"status":"ok","app_name":"BerthSide API","version":"0.1.0",
 "environment":"development","database":"sqlite","data_source":"<backend>/data/corpus",
 "ai_provider":"hybrid","emails_cached":520,"reports_stored":520}
```

## GET /emails?limit=50&offset=0

```json
{"total":520,"limit":50,"offset":0,
 "emails":[{"email_id":"email_004","from":"docs@vitalsolutions.sg",
            "subject":"REQUEST BL DRAFT …","attachments":["attachments/email_004_SI.txt",
            "attachments/email_004_BL.txt"],"has_attachments":true,
            "processed":true,"status":"MISMATCH"}]}
```

## GET /emails/{email_id}

Adds `body`. 404 if unknown.

## GET /emails/{email_id}/status

Processing state for progress indicators and failure badges.

| `state` | Meaning |
|---|---|
| `PENDING` | never processed |
| `COMPLETED` | `OK` / `MISMATCH` / `SKIPPED` |
| `NEEDS_HUMAN` | `NEEDS_REVIEW` |
| `FAILED` | `ERROR` (see `error_message`, re-run to retry) |

```json
{"email_id":"email_004","processed":true,"state":"COMPLETED","status":"MISMATCH",
 "category":"BL_COMPARISON","attempts":4,"error_message":null,
 "processing_ms":337.5,"reviewed":true,"updated_at":"2026-09-18T14:52:41.478645"}
```

## POST /emails/{email_id}/process

Idempotent. Re-run = retry (increments `attempts`).

```json
{"email_id":"email_004","category":"BL_COMPARISON","status":"MISMATCH","has_defect":true,
 "defect_fields":["consignee","notify_party"],"review_reason":null,
 "field_results":[{"field":"consignee","si_value":"EAST BRIGHT FZ-LLC",
                   "bl_value":"UAB NOVAKOPA","match":false}],
 "error_message":null,"attempts":1,"processing_ms":13.6}
```

## POST /emails/process-all?limit=0

`{"requested":520,"processed":520,"succeeded":520,"failed":0,"by_status":{…},"by_category":{…},"elapsed_ms":91188.2}`

## POST /emails/retry-failed?statuses=ERROR&statuses=NEEDS_REVIEW&limit=0

`{"targets":5,"retried":5,"recovered":0,"still_failing":5,"by_status":{…}}`

## GET /reports?status=&category=&has_defect=&limit=&offset=

`{"total":…,"limit":…,"offset":…,"reports":[{report_id,email_id,category,status,has_defect,defect_fields,review_reason}]}`

## GET /reports/{report_id}

Accepts a numeric report id **or** an `email_id`. Full report with per-field
evidence.

## GET /reports/summary/stats

`{"total_reports":520,"by_status":{…},"by_category":{…},"defect_fields_frequency":{…},"needs_review":123,"errors":0}`

## GET /reports/submission/json

`{"submission":{"email_001":{"category":…,"status":…,"review_reason":…,"has_defect":…,"defect_fields":[…]}, …},"emails_covered":520,"total_inbox":520}`

Matches `sample_submission.json` exactly — POST it to the official server's
`/submit` for self-evaluation.

## POST /reviews/{email_id}

```json
// request
{"decision":"CORRECT","corrected_fields":{"consignee":"UAB NOVAKOPA"},
 "corrected_category":null,"reviewer":"nicol","notes":"checked by hand"}
// response
{"id":1,"report_id":4,"email_id":"email_004","reviewer":"nicol","decision":"CORRECT",
 "corrected_fields":{"consignee":"UAB NOVAKOPA"},"corrected_category":null,
 "notes":"checked by hand","created_at":"2026-09-18T14:14:07.630984"}
```

`CORRECT` re-runs the **deterministic** comparison with the corrected values and
rewrites the report. 409 if the email has not been processed yet.

## GET /reviews · GET /reviews/{email_id}

Review history (newest first).

---

## Enums (frozen)

- `category`: `BL_COMPARISON` | `SI_REQUEST` | `INVOICE_QUERY` | `GENERAL` | `SPAM`
- `status`: `OK` | `MISMATCH` | `NEEDS_REVIEW` | `SKIPPED` | `ERROR`
- `review_reason`: `wrong_doc_type` | `missing_attachment` | `unreadable` | `missing_value` | `low_confidence`
- compared fields: `shipper`, `consignee`, `notify_party`, `port_of_loading`, `port_of_discharge`, `container_count`, `gross_weight_kg`

## Errors

`404` unknown email/report · `409` review before processing · `422` invalid
payload · `500` unexpected (message is also stored on the report as
`status=ERROR`).
