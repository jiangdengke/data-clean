# R2 Model Scanner Contracts

## Scenario: Read-only R2 model scanning

### 1. Scope / Trigger

- Trigger: Any change to the FastAPI application, R2 client, scanner, background jobs, or report storage.
- Scope: The application reads a configured source bucket and discovers model names from `.tar.gz` archive member paths.
- Forbidden in the read-only slice: `PutObject`, `CopyObject`, multipart writes, deletes, bucket creation, and temporary test uploads.

### 2. Signatures

- `POST /api/login` accepts `{ "password": string }` and establishes an opaque server-side session cookie.
- `POST /api/connection` accepts `{ "endpoint": string, "access_key_id": string, "secret_access_key": string, "source_bucket": string }`.
- `POST /api/connection/test` performs read-only source bucket validation.
- `POST /api/scans` returns HTTP 202 with `{ "job_id": string, "status": "queued" }`.
- `GET /api/scans/{job_id}` returns credential-free job status and progress.
- `GET /api/scans/{job_id}/report` returns the credential-free scan report after completion.
- `extract_model_names(member_paths)` returns sorted unique names matching `roots/primary/<model-name>/...`.

### 3. Contracts

#### Request and response fields

- All protected endpoints require the server-side session cookie.
- Connection secrets are accepted only by the server and are never returned in a response, report, durable job record, or log.
- A scan report includes source object count and total bytes, model object lists and totals, unmatched objects, non-archive objects, failed objects, and timed-out objects.
- Object references contain only `key` and `size`.
- One source object may appear under multiple models, but at most once per individual model report.

#### Environment keys

- `ADMIN_PASSWORD` is required and is never written to the data directory.
- `DATA_DIRECTORY` selects durable non-secret job/report storage and defaults to `./data`.
- `COOKIE_SECURE` controls whether the session cookie has the `Secure` attribute; production HTTPS deployments must set it to `true`.

#### Runtime limits

- Each object scan has a 120-second timeout.
- Network/read errors may be retried up to two times after the first attempt.
- Archive corruption is classified without retry.
- One process-local scan task may run at a time.

### 4. Validation & Error Matrix

| Condition | Required result |
|---|---|
| Missing or invalid administrator password | HTTP 401; do not create a session |
| Protected request without a valid session | HTTP 401 |
| Cross-origin state-changing request | HTTP 403 |
| Missing connection settings when starting a scan | HTTP 400 |
| Unknown scan job ID | HTTP 404 |
| Existing active scan | HTTP 409 |
| Non-`.tar.gz` source object | Record as `non_archive`; continue |
| Archive contains no model path | Record as `unmatched`; continue |
| Corrupt archive | Record as `archive_corrupt`; do not retry |
| Timeout | Record as `timed_out`; continue |
| Exhausted retryable read failures | Record as `failed`; continue |
| Source bucket access failure | Return a credential-free error; do not expose provider response details |

### 5. Good / Base / Bad Cases

- Good: A valid archive containing `roots/primary/model-a/file.json` produces one `model-a` association and no extracted files on disk.
- Good: An archive containing both `model-a` and `model-b` is included once in each model report.
- Base: A valid non-archive object is listed in `non_archive_objects` and does not fail the complete scan.
- Bad: A response, report, or durable JSON record contains a secret access key, password, or raw provider error.
- Bad: A connection test uploads a temporary object or a read-only scan calls any object-write API.

### 6. Tests Required

- Authentication tests must assert protected endpoints return 401 before login and that session cookies are HttpOnly, SameSite, and conditionally Secure.
- Connection tests must assert the secret access key is absent from response bodies.
- Scanner tests must assert model extraction is unique and sorted, archive corruption is not retried, and retryable reads attempt at most three total reads.
- Report tests must assert multi-model objects are deduplicated within each model and totals use source object sizes.
- API smoke tests must assert unknown `/api/` paths return JSON 404 rather than the SPA HTML fallback.
- Source review must assert no read-only application module calls R2 write operations.

### 7. Wrong vs Correct

#### Wrong

```python
client.put_object(Bucket=target_bucket, Key=source_key, Body=source_body)
```

This violates the read-only scanner contract and exposes object contents to the application process.

#### Correct

```python
return await asyncio.to_thread(
    read_only_client.test_source_bucket,
    connection_settings.source_bucket,
)
```

Connection testing performs only a read-only bucket check. Actual synchronization requires a later, separately approved implementation phase.

