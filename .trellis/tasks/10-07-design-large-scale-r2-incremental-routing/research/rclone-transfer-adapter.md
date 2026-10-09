# rclone as an R2 transfer adapter

**Task:** `10-07-design-large-scale-r2-incremental-routing`  
**Research date:** 2026-10-09  
**Locally inspected rclone:** `v1.74.3` (`darwin/arm64`, Go 1.26.4)  
**Scope:** Transfer-adapter research only. No remote, credential, deployment, or application change was made.

## Decision

Do **not** replace the current boto3 transfer path wholesale after the UI refactor. Keep the existing application-owned discovery, classification, SQLite state machine, source-version check, target-existence policy, retry scheduling, and reporting. If rclone is adopted, introduce it only as an optional implementation of the final `copy one object` operation behind the existing R2 client/route boundary.

For the current one-route-per-object workflow, `rclone copyto` is the closest fit. It preserves the exact destination key and does not require a bucket traversal. `rclone copy --files-from-raw` is useful only for a later batch adapter; it has weaker per-object result semantics and missing listed source files do not make the command fail.

The replacement is blocked until the following decisions are explicit:

1. Confirm that the requested tool is **rclone**, not a different product named `r2clone`.
2. Confirm whether every source bucket and mapped target bucket are in the same Cloudflare account and are accessible through the same endpoint and credential profile.
3. Measure the maximum archive size. Cloudflare R2 `CopyObject` is limited to 5 GiB and R2 does not support multipart copy, while rclone's S3 server-side path uses `CopyObject` rather than multipart copy.
4. Accept or redesign the current HEAD-then-copy race. rclone does not add an atomic destination create or bind the copy to the indexed source fingerprint.
5. Define a truthful subprocess result contract. Exit code 0 alone cannot distinguish `copied`, `already existed`, and `identical/skipped`.

Until those points are resolved and covered by adapter contract tests, boto3 remains the lower-risk default.

## Current application contract that must remain authoritative

The present design is more than a copy command:

- `backend/r2_client.py:13-20` defines an application-owned R2 protocol for listing, conditional reads, HEAD, target existence, and copy.
- `backend/r2_client.py:79-115` performs source and target HEAD operations and a server-side `CopyObject`. It never creates a bucket or deletes an object.
- `backend/sync.py:73-101` treats any existing destination key as `skipped` and only reports `copied` after the copy call returns.
- `backend/incremental.py:244-271` re-HEADs the source, compares the durable `(ETag, size)` fingerprint, marks changed source versions `superseded`, and only then invokes the copy boundary.
- `backend/models.py:31-50` defines the fingerprint as `ETag + size` and explicitly does not interpret ETag as a universal MD5 checksum.
- `backend/incremental.py:319-345` and `backend/incremental.py:544-561` use durable SQLite claims, process restart recovery, and application-level retry states. rclone must not become the durable queue.
- `backend/config.py:120-139` resolves per-source `credential_ref` values only from process environment. Saved source profiles contain endpoint and credential references, not secrets.
- `Dockerfile:9-20` currently installs Python dependencies only. The runtime image does not contain rclone, so deployment would need a pinned rclone version and checksum before an adapter could be enabled.

The existing routing behavior also remains unchanged: scan each new or changed `.tar.gz` once, recognize models from `roots/primary/<model>/`, and copy the complete archive to each mapped target bucket. rclone does not inspect tar members, consume Cloudflare Queue events, maintain the SQLite index, discover mappings, or reduce the unavoidable first read of historical archives.

## Command choice

| Candidate | Fit | Important behavior | Decision |
| --- | --- | --- | --- |
| `rclone copyto source:file dest:file` | One durable route task, exact key | Copies one file; a successful command can still mean no transfer was needed | Preferred primitive for a one-object adapter |
| `rclone copy ... --files-from-raw - --no-traverse` | Small batches sharing one source and target | Avoids full traversal, but addresses each listed path separately; a missing source path is not an error | Possible later batch optimization, not the first adapter |
| `rclone copy ... --files-from0 - --no-traverse` | Same as above, including keys containing newlines | NUL-delimited input is safer than line-delimited raw input | Prefer over `--files-from-raw` if arbitrary R2 keys must be supported |
| `rclone sync` | Directory mirroring | May delete destination objects to make trees identical | Prohibited |
| `rclone move` / `moveto` | Transfer followed by source removal | Can delete the source after copy | Prohibited |

### `--ignore-existing` versus `--immutable`

Neither flag implements the application's full policy on its own.

- `--ignore-existing` skips every destination path that already exists. In rclone v1.74.3 source, that skip returns a successful no-transfer result. This is the closest defensive flag for the current no-overwrite rule, but the application must still perform and record its own destination HEAD.
- `--immutable` also skips an identical destination successfully, but returns an error when an existing destination differs. It is useful for immutable backup verification, not as the sole implementation of the current rule that **any** existing destination is a visible conflict.
- `--no-check-dest` is the opposite of the required policy: it suppresses destination checks and should not be used.

Recommended defense in depth: retain the application target HEAD, run `copyto --ignore-existing`, then verify the destination and classify the result from structured rclone output plus the before/after observations. This preserves current behavior but still is not atomic.

### `--no-check-bucket` and `--no-traverse`

For the S3 backend, use `--s3-no-check-bucket` (the command-line form of `no_check_bucket`). Cloudflare recommends it when credentials do not have bucket-list/create permission. It also prevents rclone from trying to create a missing destination bucket, preserving the application's `do not create buckets` boundary; a missing bucket should surface as a route failure.

`--no-traverse` is valuable with `copy --files-from[-raw|0]` because each named object is addressed directly instead of listing a very large destination. It is unnecessary for `copyto`, whose file-to-file path already avoids a directory traversal. With a long file list, direct per-object API calls can cost more than a destination listing, so batch size should be measured rather than assumed.

## Server-side and streamed transfer behavior

### Same account and same remote

Rclone normally attempts server-side copy only when source and destination use the same rclone remote name. A single configless R2 remote can therefore address both buckets:

```text
r2:<source-bucket>/<object-key>
r2:<target-bucket>/<object-key>
```

With the same Cloudflare account endpoint and credential set, rclone's S3 backend calls R2 `CopyObject`; the archive does not pass through the FastAPI container.

### Different rclone configurations

`--server-side-across-configs` permits rclone to attempt server-side operations across different remote configurations. It does not override provider restrictions. Cloudflare requires the `CopySource` bucket to belong to the same account, and rclone documents this flag as something that can cause data loss if the configurations do not refer to the same underlying storage system. It should be disabled by default and considered only after an explicit same-account integration test.

The current data model stores a credential profile only on the source bucket. A mapping contains only `(source bucket, model, target bucket)`. If a target requires a different endpoint or credential profile, the application cannot yet describe that route safely; the model must be extended before rclone or boto3 can implement it correctly.

### Cross-account or incompatible configurations

When server-side copy is unavailable, rclone downloads from the source and uploads to the destination through the application host. This consumes container network bandwidth, increases transfer duration and failure exposure, and may incur source-side egress or provider operation charges. It must be displayed and monitored as a different transfer mode, not silently treated as equivalent to R2-internal copy.

For large streamed archives, rclone's S3 backend uses multipart upload. Its documented defaults are a 5 MiB chunk and upload concurrency of 4; memory is approximately `chunk_size * upload_concurrency * active_transfers`, excluding other buffers. Rclone automatically increases chunk size when a known object size would otherwise exceed the 10,000-part S3 limit. Application worker concurrency and rclone upload concurrency must be budgeted together.

### R2's 5 GiB copy boundary

Cloudflare documents a 5 GiB maximum object size for `CopyObject` and states that multipart copy is not implemented. Rclone v1.74.3's S3 server-side implementation sends a single `CopyObject` request and does not add a Cloudflare-specific multipart-copy path. Therefore:

- Same-account archives up to 5 GiB can use server-side copy.
- Archives above 5 GiB need a deliberately streamed download/multipart-upload path, such as disabling the `Copy` feature after integration testing, or must stay on another proven transfer implementation.
- Do not assume that rclone automatically turns an oversized R2 server-side copy into a multipart copy.

## Concurrency and retries

Rclone has its own high-level and low-level retries, while the application already persists `retry_wait` and retries route tasks after restart. Leaving both layers at broad defaults multiplies attempts and obscures operator-visible timing.

Recommended boundary for a first adapter:

- Keep application route retry scheduling authoritative.
- Use one `copyto` process per claimed route and bound total processes by the existing worker concurrency.
- Start with `--retries 1` and a small explicit `--low-level-retries` value; tune only from measured transient-failure behavior.
- Apply a subprocess timeout and terminate the entire process group on cancellation or service shutdown.
- For streamed transfers, explicitly cap `--s3-upload-concurrency` and `--s3-chunk-size` based on memory and bandwidth budgets.
- Never use rclone's transfer queue as a substitute for SQLite claims, route states, or restart recovery.

## Version binding, no-overwrite, and race conditions

Rclone does not close either race that already exists around the current copy call:

1. The source can be overwritten after the application's fingerprint HEAD and before rclone reads or copies it. Rclone's S3 copy request does not expose an adapter-level `CopySourceIfMatch` binding to the stored ETag.
2. The destination can be created after the application's existence HEAD and before rclone writes it. `--ignore-existing` is itself a check followed by copy, not an atomic `create if absent` operation. The inspected rclone S3 `CopyObjectInput` does not set destination `If-None-Match`.

A pre-copy HEAD plus rclone flags preserves the current best-effort behavior but is not a stronger guarantee. A production replacement must either accept and document this residual race or add a provider/API mechanism that has been validated to enforce the desired preconditions. Post-copy HEAD verification detects some outcomes but cannot prove which actor won a concurrent write.

## Truthful result reporting

The adapter must not map `process exit code == 0` directly to `status = copied`.

In rclone v1.74.3:

- `--ignore-existing` returns success after skipping an existing object.
- An identical destination can be skipped successfully.
- `copy --files-from[-raw|0]` does not fail merely because a listed source path is missing.
- High-level retries can repeat logger records, and rclone documents its per-path logger output as what should happen, not an infallible final audit record.

A one-object adapter should capture structured rclone logs/statistics in memory with a strict size limit, extract only safe status fields, and perform a destination HEAD after the command. Persist only normalized codes such as `copied`, `target_exists`, `source_missing_or_changed`, `verification_failed`, and `transfer_failed`. Do not persist raw provider responses, the subprocess environment, full command arguments, or unrestricted stderr. Object keys may be included only where the existing route/report contract already permits them.

For a batch adapter, every input key needs an independently verified outcome. One process exit code is insufficient to update multiple SQLite route rows.

## Secret and process integration

Do not create `rclone.conf` in the mounted data volume. Rclone supports configless named remotes through `RCLONE_CONFIG_<REMOTE>_*` environment variables. The adapter should build a private environment dictionary per subprocess from the already-resolved `ConnectionSettings`; it should not mutate global `os.environ`, especially with concurrent workers.

Use a fixed executable path from deployment configuration, an argument array without a shell, `--config /dev/null`, and environment-only secrets. The access key and secret must never appear in command arguments, logs, reports, SQLite business rows, or exception text. Endpoint, bucket, and key values must be passed as individual arguments rather than interpolated into a shell command.

Illustrative same-account command shape, with placeholders only:

```sh
RCLONE_CONFIG_R2_TYPE=s3 \
RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \
RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
RCLONE_CONFIG_R2_ENDPOINT="https://<ACCOUNT_ID>.r2.cloudflarestorage.com" \
rclone --config /dev/null copyto \
  'r2:<SOURCE_BUCKET>/<OBJECT_KEY>' \
  'r2:<TARGET_BUCKET>/<OBJECT_KEY>' \
  --ignore-existing \
  --s3-no-check-bucket \
  --retries 1 \
  --low-level-retries 3 \
  --use-json-log \
  --log-level NOTICE
```

This example does not provide atomic no-overwrite or source-version binding. It is an adapter invocation shape, not a complete correctness protocol.

## Recommended integration boundary

After the UI workflow refactor, introduce a narrow transfer interface whose input is one already-validated durable route:

```text
copy_whole_object(
  source ConnectionSettings,
  source bucket/key/fingerprint,
  target bucket/key,
) -> normalized copied/conflict/retryable/terminal result
```

Keep these steps outside the rclone adapter:

1. Resolve the per-source profile and deployment-injected credentials.
2. Claim the SQLite route and HEAD the current source.
3. Reject a fingerprint mismatch as `superseded`.
4. HEAD the destination and record an existing key as `conflict`.
5. Invoke either boto3 or rclone for exactly one whole-object transfer.
6. Verify destination metadata and persist a normalized result.
7. Let the application schedule retry/backoff and recover claims after restart.

Roll out rclone behind a disabled-by-default deployment setting. Contract tests should run both implementations against an S3-compatible test double or isolated test buckets and cover: source mutation before copy, pre-existing destination, destination created during copy, object over 5 GiB behavior, missing bucket, missing source, subprocess timeout/cancellation, skip with exit 0, cross-profile routes, and restart after process termination. No real 1-2 TB backfill should be part of adapter validation.

## Sources

Primary documentation, accessed 2026-10-09:

1. Cloudflare R2, **Use Rclone with R2**: <https://developers.cloudflare.com/r2/examples/rclone/>
2. Cloudflare R2, **S3 API compatibility**: <https://developers.cloudflare.com/r2/api/s3/api/>
3. Cloudflare R2, **Multipart uploads**: <https://developers.cloudflare.com/r2/objects/multipart-objects/>
4. Cloudflare R2, **Limits**: <https://developers.cloudflare.com/r2/platform/limits/>
5. Cloudflare R2, **Copy objects**: <https://developers.cloudflare.com/r2/objects/copy-objects/>
6. rclone, **S3 backend**: <https://rclone.org/s3/>
7. rclone, **copyto**: <https://rclone.org/commands/rclone_copyto/>
8. rclone, **copy**: <https://rclone.org/commands/rclone_copy/>
9. rclone, **filtering and files-from**: <https://rclone.org/filtering/>
10. rclone, **global documentation and server-side copy**: <https://rclone.org/docs/>

Behavioral source checks against the locally installed version tag `v1.74.3`, accessed 2026-10-09:

11. rclone `fs/operations/copy.go`: <https://github.com/rclone/rclone/blob/v1.74.3/fs/operations/copy.go>
12. rclone `fs/operations/operations.go`: <https://github.com/rclone/rclone/blob/v1.74.3/fs/operations/operations.go>
13. rclone S3 backend `backend/s3/s3.go`: <https://github.com/rclone/rclone/blob/v1.74.3/backend/s3/s3.go>
14. rclone S3 options `backend/s3/s3_options.go`: <https://github.com/rclone/rclone/blob/v1.74.3/backend/s3/s3_options.go>
