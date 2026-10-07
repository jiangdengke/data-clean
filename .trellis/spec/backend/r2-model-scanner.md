# R2 Model Scanner: Explicit Sync and Incremental Routing

## Feature: Multi-source R2 model scanning, explicit synchronization, and incremental routing

### Layers Involved

- [x] Server Component / dashboard status projection
- [x] Client Component / monitoring and operator controls
- [x] API routes / authenticated FastAPI procedures
- [x] Middleware / session and same-origin checks
- [x] Database / SQLite WAL durable index and work queues
- [x] Background workers / queue pull, classification, routing, reconciliation, and backfill
- [x] External services / R2 S3-compatible API and Cloudflare Queue HTTP pull API

### Data Flow

The system has two deliberately separate flows:

```text
Browser -> authenticated API -> SQLite app state/mappings
R2 object-create -> Cloudflare Queue -> HTTP pull -> validate allowlist
  -> SQLite queue_events + objects commit -> Queue ack
  -> bounded classifier -> model associations -> routing_tasks
  -> explicit server-side CopyObject, never overwrite
ListObjectsV2 metadata reconciliation --------------------------^
```

Queue delivery is at-least-once and unordered. The Queue is an ingestion boundary, not the archive-processing lease: the consumer persists/coalesces an event in SQLite and only then acknowledges its lease. Classification and routing continue from durable SQLite work after acknowledgement. The periodic metadata reconciliation is a correctness backstop for missed, expired, malformed, or prematurely acknowledged events.

The existing explicit scan and synchronization contract remains in force. A normal scan is read-only and discovers model names from `.tar.gz` member paths. A sync is still an administrator-triggered, preflighted operation using server-side `CopyObject`; it does not create buckets, delete source objects, or overwrite target keys. Incremental routing may enqueue the same kind of copy only after a saved `(source_bucket, model_name, target_bucket)` mapping exists and the continuous mode is enabled.

### Format at Each Layer

| Layer | Format |
| --- | --- |
| Browser request | JSON validated by Pydantic; connection is `{endpoint, source_buckets}` only; no runtime secret fields |
| API response | Credential-free JSON status, mappings, scan reports, or explicit sync reports |
| SQLite | Transactional rows in `objects`, `routing_tasks`, `queue_events`, `app_state`, and `backfill_state`; SQLite WAL enabled |
| Queue pull | Provider envelope with `result.messages`; each message has a `lease_id`, body, and metadata/content encoding |
| R2 event | Validated `object-create` event: account, action, allowlisted bucket, object key, size, and optional ETag |
| Object identity | `(source_bucket, object_key, fingerprint)`, where `fingerprint = ETag + size`; ETag is a version clue, not a generic MD5 |
| Scanner result | Sorted unique model names matching `roots/primary/<model-name>/...`, or a credential-free classification |
| Routing task | Source bucket/key, target bucket, model names, state, attempts, retry time, and safe error code |
| Dashboard status | Serializable booleans, counts, timestamps, backfill checkpoint/status, and non-secret readiness/error fields |

### Transformation Points

| From | To | Who |
| --- | --- | --- |
| Browser connection form | normalized endpoint and deduplicated source bucket allowlist | API validation and storage |
| Queue provider envelope | decoded JSON event | Queue client, accepting only documented `result.messages` and ack envelopes |
| Event/list metadata | versioned SQLite object row | transactional incremental store |
| Archive stream | sorted unique model associations | existing scanner; no extraction to disk |
| Saved mappings + model associations | deduplicated `routing_tasks` | incremental store |
| R2 target existence + source fingerprint | copied, conflict, superseded, or retry state | routing worker and existing sync policy |
| SQLite rows | status JSON | authenticated API response; never expose secret columns or provider bodies |

### Auth Strategy

- Middleware: protected API endpoints require the existing server-side session cookie; state-changing requests also require the existing same-origin check.
- API: `/api/connection`, `/api/connection/test`, mappings, scans, sync, and all incremental endpoints require the authenticated administrator session.
- Runtime credentials: R2 and Queue secrets are deployment-injected process environment values only. They are not accepted from the browser, persisted in SQLite/JSON, returned by an API, displayed in the UI, or written to logs.
- Provider validation: readiness checks only confirm that required environment values are present. Connection testing may perform the existing explicit read-only source-bucket check when the administrator asks for it. This specification does not claim live R2 or Cloudflare provider validation.

### Edge Cases Considered

- [x] Empty/null or malformed Queue messages
- [x] Duplicate and out-of-order object-create events
- [x] Missing or non-allowlisted source buckets
- [x] Object overwrite with a changed ETag/size
- [x] Invalid archive, non-archive object, timeout, and exhausted retry
- [x] Queue ack after durable acceptance only
- [x] Restart while an object or route claim is active
- [x] Missing runtime secrets at startup or after a persisted toggle
- [x] Paused backfill and backfill/listing overlap with new events
- [x] Existing target key and no-overwrite conflict
- [x] Double submission of operator actions
- [x] No secret or raw provider error in any data outlet

## 1. Scope and explicit synchronization boundary

- The scanner reads configured source buckets and discovers model names from `.tar.gz` archive member paths. It does not extract archive contents to disk.
- `POST /api/sync/preflight` remains a read-only target access check. `POST /api/sync` remains an explicit authenticated administrator action that starts a background server-side copy task.
- Only the explicit sync worker and an enabled incremental routing worker may call server-side `CopyObject`, and both must preserve the no-overwrite rule. `PutObject`, multipart writes, deletes, bucket creation, and implicit overwrite are forbidden.
- Existing scan, mapping, preflight, report, and synchronization behavior must not be reinterpreted as continuous processing. Manual full scan remains a recovery/audit tool.

## 2. API signatures and request contract

### Existing endpoints

- `POST /api/login` accepts `{ "password": string }` and establishes an opaque server-side session cookie.
- `GET /api/connection` returns credential-free configuration/readiness.
- `POST /api/connection` accepts **only** `{ "endpoint": string, "source_buckets": string[] }`. It must reject or ignore no secret-bearing browser contract: `access_key_id` and `secret_access_key` are not request fields and must never be accepted from browser input.
- `POST /api/connection/test` performs the existing administrator-requested read-only source-bucket checks; it does not validate or return provider secrets.
- `POST /api/scans` returns HTTP 202 with `{ "job_id": string, "status": "queued" }`.
- `GET /api/scans/{job_id}` and `/report` return credential-free progress and results.
- `GET/POST /api/mappings` persists `(source_bucket, model_name, target_bucket)` mappings without credentials.
- `POST /api/sync/preflight` validates complete mappings and target access.
- `POST /api/sync` starts explicit server-side copy; `GET /api/sync/{sync_job_id}` and `/report` return safe status/results.
- `extract_model_names(member_paths)` returns sorted unique names matching `roots/primary/<model-name>/...`.

### Incremental endpoints

All endpoints below require authentication and state-changing POSTs require same-origin validation.

- `GET /api/incremental/status` returns:

  ```json
  {
    "continuous_enabled": true,
    "paused": false,
    "runtime_ready": true,
    "readiness_message": "...",
    "backfill": {
      "status": "running|paused|draining|completed|failed|idle",
      "source_bucket": "source-a",
      "continuation_token": "opaque-or-null",
      "total_seen": 1000,
      "total_classified": 900,
      "listing_complete": 0,
      "started_at": "ISO-8601-or-null",
      "updated_at": "ISO-8601-or-null",
      "error_code": null
    },
    "counts": { "queued": 10, "scanning": 2, "failed": 1, "route_pending": 3 },
    "queue_last_pull_at": "ISO-8601-or-null",
    "queue_last_ack_at": "ISO-8601-or-null",
    "queue_backlog_count": 42,
    "reconcile_last_run_at": "ISO-8601-or-null",
    "last_error": null
  }
  ```

  `counts` may include durable object states and `route_<state>` counts. No credential, raw event body, or provider response is a status field.

- `POST /api/incremental/continuous` accepts `{ "enabled": boolean }`. Enabling requires all runtime environment keys to be present; the persisted toggle is restored on restart when ready. Disabling pauses/cancels continuous queue, reconciliation, and routing consumers without deleting accepted work.
- `POST /api/incremental/backfill` accepts `{ "action": "start" | "pause" | "resume" }`. Start is an explicit administrator action; pause preserves the checkpoint and durable work; resume continues it. A repeated start/resume is idempotent and must not reset a running checkpoint or create a second worker.
- `POST /api/incremental/retry` accepts `{ "object_id": integer | null }`. `null` retries all failed object rows; an ID retries only that failed object. It resets the bounded local retry state, never silently retries a conflict or changes the no-overwrite policy.

## 3. Runtime environment and secret boundary

Continuous runtime secrets are deployment-injected environment values only. The required keys are:

| Key | Purpose | Browser/data-volume policy |
| --- | --- | --- |
| `R2_ENDPOINT` | S3-compatible R2 endpoint | never accepted, returned, logged as a secret, or persisted as a credential |
| `R2_ACCESS_KEY_ID` | R2 read/copy client credential | never accepted from browser or stored in SQLite/JSON/logs |
| `R2_SECRET_ACCESS_KEY` | R2 secret credential | never accepted from browser or stored in SQLite/JSON/logs |
| `CLOUDFLARE_ACCOUNT_ID` | Queue API account scope | process environment only; never returned or persisted in business data |
| `CLOUDFLARE_QUEUE_ID` | HTTP pull Queue identity | process environment only; never returned or persisted in business data |
| `CLOUDFLARE_API_TOKEN` | Queue pull/ack authorization | process environment only; never returned or persisted in business data |

`ADMIN_PASSWORD`, `DATA_DIRECTORY`, and `COOKIE_SECURE` retain their existing contracts. The mounted data directory may contain user-facing JSON plus `incremental.sqlite3` and its WAL files, but no runtime secret. Readiness means values are present, not that a live provider call succeeded. Deployment resource creation, secret-manager wiring, R2 event rule validation, and Cloudflare Queue validation are outside this document's claimed test boundary.

## 4. Incremental R2 routing contract

### 4.1 Discovery and source allowlist

- Configure R2 `object-create` notifications for the source buckets and deliver them to the Cloudflare Queue. `PutObject`, `CopyObject`, and `CompleteMultipartUpload` are accepted create actions; delete events are not routing inputs.
- The event consumer must validate account, action, object key, non-negative size, and optional ETag. The event bucket must be in the saved `source_buckets` allowlist. An event from an unknown account, unsupported action, malformed object, or non-allowlisted bucket is not accepted as application work and must not be acknowledged as a valid object.
- The saved source-bucket allowlist is the authorization boundary for both events and metadata reconciliation. No bucket name from an event may create a new source configuration.
- Periodic `ListObjectsV2` metadata reconciliation compares each listed `(bucket, key, ETag + size)` to SQLite and inserts only unseen/changed versions. It is a low-frequency backstop, not a replacement for the event path and not a provider change cursor.
- Each unseen or changed `.tar.gz` is read once for model discovery. Existing classified fingerprints are not reread. Non-archive objects may be recorded as `non_archive` without archive reading.

### 4.2 Fingerprint, deduplication, and ordering

- Object identity is `(source_bucket, object_key, fingerprint)` with `fingerprint = ETag + size`. ETag must not be interpreted as a universal MD5 checksum.
- Duplicate events for an existing `queue_events.event_key` or existing object fingerprint are idempotent and coalesced. Event order is not trusted.
- A different fingerprint for the same key is a new version candidate. Dated event metadata may assist diagnostics/order, but event delivery time is not itself a provider object version. A stale candidate must not make a newer current row route backward.
- Before classification or copy, compare current R2 metadata to the stored ETag/size when the client supports HEAD. If the source changed, supersede the stale work and enqueue/process the newer fingerprint through normal reconciliation/event handling.
- Persist only safe event fields and error categories. Never persist arbitrary event payloads, tokens, credentials, or raw provider error bodies.

### 4.3 SQLite WAL state model

`incremental.sqlite3` is the durable single-container state store and must enable SQLite WAL. It contains:

- `objects`: versioned source object rows with source bucket/key, fingerprint, ETag, size, classification, model names, current flag, origin (`event`, `backfill`, or `reconcile`), state, attempts, next retry time, safe error code, and timestamps.
- `routing_tasks`: deduplicated `(object_id, target_bucket)` tasks with model names, state, attempts, retry time, safe error code, and timestamps.
- `queue_events`: deduplication/audit rows keyed by an application event key, with source bucket/key/action and sanitized payload only.
- `app_state`: persisted continuous toggle, connection endpoint/source-bucket configuration, queue/reconciliation timestamps, and other non-secret application state.
- `backfill_state`: singleton checkpoint with status, current source bucket, continuation token, totals, listing-complete marker, timestamps, and safe error code.

Object ingestion, event audit, and object-version upsert must be transactional. Claiming a work row must be atomic (`BEGIN IMMEDIATE` or equivalent) so concurrent workers cannot process one object/route twice. On restart, `scanning` and `running` claims are recovered to durable queued/pending states; attempts remain bounded and work is never marked completed merely because the process stopped.

### 4.4 Queue pull and acknowledgement

- Pull uses the Cloudflare HTTP pull endpoint and parses messages from the documented `result.messages` response. It must tolerate empty results without inventing work.
- Acknowledgement uses the documented envelope `{ "acks": [{ "lease_id": "..." }], "retries": [] }`. Lease IDs are transport handles, not object identity keys.
- For every accepted message: decode the documented body/content encoding, validate the R2 event and allowlist, commit `queue_events` plus the object-version row to SQLite, and only then include that lease ID in an ack request.
- Archive reading and target copying happen after this durable acceptance and must not hold the Queue visibility lease for the full archive timeout. A crash after ack is recoverable from SQLite; a crash before commit leaves the message for Queue redelivery/DLQ handling.
- Invalid or non-allowlisted messages are rejected without creating application work. Provider transport failures leave the pull/ack operation retryable and must not record a false successful ack.

### 4.5 Classification, mapping, and routing

- Classification reuses `roots/primary/<model-name>/...` extraction and stores sorted unique model associations. Corrupt archives are visible terminal outcomes; network/read errors and timeouts are retryable under bounded policy.
- A matched object with a saved mapping for `(source_bucket, model_name)` creates a deduplicated routing task per target bucket. A matched object with no mapping remains `awaiting_mapping`/`unmapped`; it never guesses a target, creates a bucket, or copies automatically.
- Saving mappings may enqueue routes for already classified matching objects without rereading their archives.
- A route checks the current source fingerprint and target existence immediately before the existing server-side `CopyObject`. If the target key already exists, it does not overwrite it; record a visible conflict such as `target_exists`/`conflict` and wait for administrator handling. A changed source version supersedes the stale task without copying the stale object.
- Route retries are bounded with exponential backoff and safe error codes. Terminal failures remain visible and can be explicitly selected by the retry endpoint. Retries must not reset or bypass source allowlists, mappings, authentication, or no-overwrite checks.

### 4.6 Backfill lifecycle and overlap

- Event discovery/continuous mode must be enabled before the first historical backfill so new writes during listing are retained. Backfill is never auto-started by readiness or process startup.
- Backfill lists source buckets page by page with a finite page size and durable continuation checkpoint; it upserts metadata as `origin=backfill` and does not put the full bucket in one JSON report.
- Backfill statuses are `idle`, `running`, `paused`, `draining`, `completed`, or `failed`. `running` advances listing and classification; `paused` stops new classification/listing while preserving accepted work and checkpoint; once listing is complete it enters `draining` until all current backfill objects are classified; only then is it `completed`.
- Resume continues the stored bucket/token/totals. Restart recovery reclaims abandoned object/route claims and resumes a running/draining backfill when R2 runtime credentials are ready. Missing credentials produce a visible readiness/failure state, not false completion.
- Backfill classification may route only when continuous routing is enabled and a saved mapping exists. New event/reconcile objects remain independently processable while backfill is paused; backfill claims are explicitly gated by the backfill status.
- After completion, run metadata reconciliation to close the overlap window. Retain manual full scan as a recovery tool.

## 5. UI and monitoring fields

The dashboard must expose, without secrets:

- Continuous routing: enabled/paused, runtime-ready boolean, readiness message, and last safe error.
- Backfill: status, current source bucket, checkpoint presence (not a secret), objects seen/classified, listing-complete/draining indicator, start/update timestamps, and error code.
- Work pressure: counts for queued, scanning, retry-wait, unmapped/awaiting-mapping, failed, copied, conflict, superseded, and route pending/running/failed states.
- Queue/reconciliation: last pull, last ack, the latest provider-reported `message_backlog_count` when available, last metadata reconciliation, and local backlog/failure counts. The provider count is persisted as non-secret app state and may be `null` before a valid pull response; deployment-level retry, oldest-message, and DLQ metrics remain external monitoring inputs and are not fabricated by the API.
- Unmapped models and target conflicts must be actionable status data, not hidden logs. Credentials, authorization headers, raw event bodies, and raw provider errors must never be rendered.

## 6. Validation & Error Matrix

| Condition | Required result |
| --- | --- |
| Missing/invalid administrator password | HTTP 401; no session |
| Protected request without valid session | HTTP 401 |
| Cross-origin state-changing request | HTTP 403 |
| `POST /api/connection` includes access/secret key fields | Request schema rejects unknown secret-bearing contract; secrets are never used or stored |
| Missing/invalid endpoint or empty source bucket list | HTTP 422; no connection state change |
| Continuous enable with any required runtime key missing | HTTP 503 with safe readiness message; toggle/consumer not enabled |
| Unknown account, unsupported action, malformed event, or non-allowlisted bucket | reject as invalid event; no object work or ack as valid application work |
| Queue pull response missing `result.messages` or with malformed message body | ignore/reject malformed message; do not create work; retain safe error/metric |
| SQLite commit fails before ack | do not ack; allow Queue retry/DLQ path |
| Duplicate event/fingerprint | idempotent no-op/coalescing; no duplicate classification or route |
| Current source metadata differs from event/index fingerprint | supersede stale work and enqueue current version; never copy stale content |
| Non-`.tar.gz` source object | record `non_archive`; continue |
| Archive contains no model path | record `unmatched`; continue |
| Corrupt archive | record `archive_corrupt`; terminal, no retry |
| Timeout/network/read failure | bounded retry with backoff; terminal `failed` after max attempts |
| Missing mapping | `awaiting_mapping`/`unmapped`; no guessed target or copy |
| Existing target key | `conflict`/`skipped_existing_target`; never overwrite |
| Queue ack/provider transport failure | safe error and retryable ingestion path; no false ack timestamp |
| Backfill paused | preserve checkpoint and accepted work; do not claim new backfill objects |
| Backfill listing finished with pending classifiers | `draining`, not `completed` |
| Restart with `scanning`/`running` claims | reclaim to queued/pending and resume according to persisted state |
| Unknown object ID on retry | HTTP 200 idempotent status or HTTP 404 per API policy; never alter another row |
| Provider access failure during explicit connection test/reconciliation | credential-free error; do not expose provider details |
| Unknown scan job ID | HTTP 404 |
| Existing active explicit scan | HTTP 409 |

## 7. Good / Base / Bad Cases

### Good

- A valid `.tar.gz` containing `roots/primary/model-a/file.json` produces one sorted `model-a` association and no extracted files on disk.
- An archive containing both `model-a` and `model-b` is included once in each model report and each applicable route task.
- A valid Queue pull under `result.messages` is decoded, its event is committed to `queue_events` and `objects`, and only then is its lease included in the ack envelope.
- A duplicate/out-of-order event for the same `(bucket, key, ETag + size)` reuses durable state without rereading the archive.
- A configured mapping routes a newly classified object through server-side `CopyObject`; a missing mapping remains visibly unmapped.
- A paused backfill resumes at its saved continuation token; a restart reclaims abandoned claims and a listing-complete job drains before completion.
- `POST /api/connection` with exactly `{ "endpoint": "...", "source_buckets": ["source"] }` stores non-secret configuration while runtime credentials remain environment-only.

### Base

- A valid non-archive object is listed as `non_archive` and does not fail the complete scan or backfill.
- A missed event is found by periodic metadata reconciliation through unseen/changed ETag+size without rereading already classified fingerprints.
- A transient read or copy failure enters retry-wait with a bounded attempt count and safe error code.

### Bad

- A request body, response, report, SQLite payload, UI field, or log contains `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `CLOUDFLARE_API_TOKEN`, an administrator password, or any raw provider response.
- Browser input supplies access/secret keys to `/api/connection`; the endpoint accepts only endpoint and source bucket names.
- The consumer acknowledges a Queue lease before the SQLite transaction commits, or acknowledges only after a long archive scan while the visibility lease expires.
- An event from a bucket outside the configured source allowlist creates an object or route task.
- A route calls `PutObject`, creates a target bucket, deletes source data, or overwrites an existing target key.
- A newer source version silently overwrites the old target or a stale out-of-order event routes over the current version.
- The process reports backfill `completed` while listing/classification work is still queued or while it is only `draining`.

## 8. Tests Required

- API contract tests assert `/api/connection` accepts only endpoint/source buckets, does not accept browser secrets, and never returns runtime secrets; readiness is based on environment presence only.
- Authentication tests assert protected endpoints return 401 before login and session cookies are HttpOnly, SameSite, and conditionally Secure; state-changing endpoints reject cross-origin requests.
- Environment/config tests cover every required runtime key, missing-key readiness, secret absence from SQLite/JSON/report/log fixtures, and restart restoration of the persisted continuous toggle.
- Queue client tests assert pull parses `result.messages`, decodes documented content types, rejects malformed payloads, and sends `{acks: [{lease_id}], retries: []}`.
- Queue ingestion tests assert source account/action/allowlist validation, duplicate and out-of-order idempotency, sanitized event persistence, SQLite-commit-before-ack ordering, and no acknowledgement after commit failure.
- SQLite tests assert WAL mode, unique `(source_bucket, object_key, fingerprint)` behavior, atomic object/route claims, queue-event deduplication, safe payload storage, origin allowlist, bounded retries/backoff, no secret fields, and restart claim recovery.
- Scanner tests assert model extraction is unique/sorted, each new archive fingerprint is read once, archive corruption is not retried, non-archives/unmatched objects are visible, and retryable reads attempt at most the configured bounded total.
- Routing tests assert saved mappings create deduplicated tasks, missing mappings stay unmapped, current source metadata is checked, target-existing results are conflicts/skips, source changes supersede stale tasks, and no write API other than the permitted server-side `CopyObject` is used.
- Backfill tests assert pagination/checkpoint persistence, start/pause/resume idempotency, running/draining/completed transitions, overlap with events, no claims while paused, bounded concurrency, and completion only after the durable drain.
- Status/UI contract tests assert all required monitoring fields are serializable and credential-free: continuous state, runtime readiness, backfill checkpoint/status, counts, queue timestamps, reconciliation timestamp, unmapped models, conflicts, and safe errors.
- Existing explicit scan/sync tests remain required, including read-only source testing, preflight behavior, explicit administrator action, multi-model reports, per-model deduplication, and unknown `/api/` JSON 404 behavior.
- Integration tests use local fake R2 and Queue clients only. They must not call real R2, Cloudflare Queue, Cloudflare APIs, perform a real 1–2 TB backfill, provision resources, deploy, or claim live provider validation.

## 9. Wrong vs Correct

### Wrong: stale connection contract and browser secret transport

```python
class ConnectionRequest(BaseModel):
    endpoint: str
    access_key_id: str
    secret_access_key: str
    source_buckets: list[str]
```

This accepts runtime credentials from the browser and makes the connection API contract stale. Runtime R2/Queue secrets are deployment-injected environment values; the request must contain only `endpoint` and `source_buckets`.

### Correct: non-secret connection settings

```python
class ConnectionRequest(BaseModel):
    endpoint: str = Field(min_length=1)
    source_buckets: list[str] = Field(min_length=1)
```

The handler reads R2 credentials only from process settings and persists only endpoint/source bucket configuration.

### Wrong: ack before durable acceptance or with a wrong provider envelope

```python
messages = queue.pull()
queue.ack([message.lease_id for message in messages])
store.ingest_event(message.body)
```

This can lose an event on a crash and does not implement the documented pull result/ack envelopes.

### Correct: durable SQLite acceptance, then documented ack

```python
messages = queue.pull(visibility_timeout_ms, batch_size)  # result.messages
leases = []
for message in messages:
    event = validate_and_allowlist(message.body)
    if event is None:
        continue
    store.ingest_event(event_key(message, event), sanitize(event), source_object(event))
    leases.append(message.lease_id)
queue.ack(leases)  # {"acks": [{"lease_id": ...}], "retries": []}
```

Archive classification and routing happen from the durable SQLite queues after this boundary; a commit failure does not produce a successful ack.

### Wrong: overwrite or implicit routing

```python
client.put_object(Bucket=target_bucket, Key=object_key, Body=body)
```

This violates the explicit sync boundary, streams object contents through the application, and can overwrite an existing target.

### Correct: mapped, current, no-overwrite server-side copy

```python
if not mapping_for(source_bucket, model_name):
    return "awaiting_mapping"
if client.object_exists(target_bucket, object_key):
    return "conflict:target_exists"
client.copy_object(source_bucket, object_key, target_bucket, object_key)
return "copied"
```

Only the explicit administrator sync worker or enabled incremental route worker may perform this existing server-side `CopyObject` path, after source freshness and mapping checks. The specification describes local mocks and durable contracts; it does not claim live provider validation.
