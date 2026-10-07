# Multi-source scan, mapping, and sync

## Goal

Complete the workflow from source-bucket discovery through explicit model-to-target-bucket mapping and background object synchronization. A single R2 connection may contain multiple source buckets; each mapping is scoped by `(source bucket, model name)` so same-named models from different source buckets cannot collide.

## Requirements

- Accept one endpoint and credential set plus a non-empty list of source buckets.
- Scan every configured source bucket and report each bucket independently, including discovered models, object totals, unmatched objects, non-archives, failures, timeouts, and bucket-level access errors.
- Keep the existing archive model rule `roots/primary/<model>/` and preserve full source object keys.
- After a completed scan, allow the administrator to enter and save a target bucket for every discovered `(source bucket, model)` pair.
- Store only non-secret mapping configuration durably; credentials remain process-memory-only.
- Require all discovered model pairs to have a target bucket before sync starts.
- Before starting sync, verify every distinct target bucket is accessible with a read-only `HeadBucket` check. The system must not create target buckets.
- Run synchronization as a background task using server-side R2 `CopyObject`; copy each matched source object to every mapped target bucket while preserving the source object key.
- Do not overwrite an existing target object key: check for an existing target object and report it as skipped.
- Persist sync job status and a credential-free sync report with copied, skipped, and failed counts and failure details that do not contain provider responses or secrets.
- Keep scan and sync tasks process-local and prevent duplicate active tasks of the same kind.
- Localize new UI text and errors in Simplified Chinese.

## Safety boundary

- This task introduces the first R2 write operation (`CopyObject`) only after explicit administrator action from the sync UI.
- No bucket creation, deletion, object deletion, or overwrite is allowed.
- A target `HeadBucket` check proves bucket access/existence, not write permission; copy failures remain isolated in the sync report.
- No real R2 synchronization is executed during development verification.

## Acceptance Criteria

- [ ] Multiple source buckets can be configured and scanned in one scan job.
- [ ] Report UI distinguishes every source bucket and its models.
- [ ] Mapping UI exposes every discovered source-bucket/model pair and prevents starting with missing mappings.
- [ ] Sync preflight checks target buckets and rejects inaccessible targets before creating a sync task.
- [ ] Sync copies matched objects without overwriting existing keys and reports each failure/skipped result.
- [ ] Authentication, secret redaction, scanner behavior, and existing read-only scan tests remain passing.
- [ ] Frontend lint/build and backend tests pass.
- [ ] Documentation and progress log describe the new write boundary and rollback.

## Out of Scope

- Automatic target-bucket creation, deletion, overwrite, scheduling, incremental scanning, multi-user access, cancellation, or resumable sync.
- Real R2 scans or syncs during automated tests.

## Technical Notes

- Relevant layers: `backend/models.py`, `backend/r2_client.py`, `backend/scanner.py`, `backend/storage.py`, `backend/app.py`, React/Vite frontend, tests, README, deployment docs, and progress log.
- The repository currently has unrelated but intentional uncommitted UI/HTTPS changes. Preserve them while extending the current frontend.
{"file":".trellis/spec/backend/index.md","reason":"Backend package conventions and applicable guideline map."}
{"file":".trellis/spec/backend/type-safety.md","reason":"Typed API request/response and validation conventions."}
{"file":".trellis/spec/backend/quality.md","reason":"Backend verification and pre-commit checklist."}
{"file":".trellis/spec/backend/performance.md","reason":"External API concurrency and background progress patterns."}
{"file":".trellis/spec/frontend/index.md","reason":"Frontend package conventions."}
{"file":".trellis/spec/frontend/components.md","reason":"Accessible React markup and form conventions."}
{"file":".trellis/spec/frontend/css-layout.md","reason":"Responsive layout and touch behavior requirements."}
{"file":".trellis/spec/frontend/quality.md","reason":"Frontend build and quality checklist."}
{"file":".trellis/spec/guides/index.md","reason":"Cross-layer and pre-implementation thinking guidance."}
{"file":".trellis/spec/backend/quality.md","reason":"Verify backend tests, safety boundaries, and type quality."}
{"file":".trellis/spec/backend/performance.md","reason":"Review external API concurrency and background task behavior."}
{"file":".trellis/spec/frontend/quality.md","reason":"Run frontend lint/build and UI quality checks."}
{"file":".trellis/spec/frontend/css-layout.md","reason":"Review responsive behavior and touch targets."}
