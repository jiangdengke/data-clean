# Research: Cloudflare R2 object-copy strategy

- **Task**: `10-06-r2-model-cleanup-sync`
- **Question**: Compare S3-compatible server-side `CopyObject` with streaming `GetObject`/`PutObject` for copying complete `.tar.gz` objects into model target buckets while preserving object keys.
- **Research date**: 2026-10-06
- **Primary provider**: Cloudflare R2 S3-compatible API

## Executive recommendation

Use server-side `CopyObject` as the first implementation for ordinary source and target buckets accessible by the same R2 connection. It copies the complete object without downloading the archive through the FastAPI process, preserves the source key when the destination `Key` is set to the same value, and supports source and destination conditional checks needed for the required idempotent/conflict behavior.

Keep streaming as a narrowly scoped fallback, not the default path. It is useful when the source and destination cannot be addressed by one R2 S3 client/credential set, when a particular SDK cannot issue R2 `CopyObject`, or when multipart handling must be controlled explicitly. Streaming increases application bandwidth, latency, timeout exposure, and metadata-handling responsibility.

The copy operation must treat the `.tar.gz` as opaque bytes. Model detection is a scan-time concern; synchronization must not extract, rewrite, recompress, or rename the archive.

## Server-side `CopyObject`

### Operation shape

R2 implements the S3 `CopyObject` operation. The request writes to the destination bucket/key and identifies the source with `x-amz-copy-source`; Cloudflare's example for a same-bucket copy uses:

```text
destination bucket: bucket-name
destination key:    path/to/object.txt
x-amz-copy-source:  /bucket-name/path/to/object.txt
```

For this project, the destination key should be exactly the source object key for every model association:

```text
destination bucket: <configured-model-target-bucket>
destination key:    <source-object-key>
x-amz-copy-source:  /<source-bucket>/<source-object-key>
```

The key is the complete string, including every prefix and the `.tar.gz` suffix. R2 has flat object storage; slash-delimited prefixes are naming/grouping conventions, not directories.

### Why it fits this task

- The R2 service performs the object transfer, so the application does not buffer or relay the archive body.
- The source object remains untouched; `CopyObject` creates the destination object.
- A single request can copy a whole object, including a multipart-uploaded source object. Cloudflare release notes state that copying multipart objects with `CopyObject` and `UploadPartCopy` was re-enabled.
- The operation supports source conditional headers (`x-amz-copy-source-if-*`) and R2 destination conditional headers (`cf-copy-destination-if-*`). The latter can prevent overwriting a destination object that has changed since the preflight check.
- `CopyObject` returns a copy result containing the destination ETag and last-modified time. The implementation should still perform/record a destination `HeadObject` after a successful copy if the result is needed for an audit report.

### Metadata behavior

R2 supports `x-amz-metadata-directive` for `CopyObject`:

- `COPY` preserves source metadata.
- `REPLACE` uses metadata supplied by the copy request.
- R2 additionally supports `MERGE`, which copies source metadata and replaces only keys supplied in the request. `MERGE` cannot remove source metadata; use `REPLACE` when removal is intentional.

The S3-compatible operation lists system metadata such as `Content-Type`, `Cache-Control`, `Content-Disposition`, `Content-Encoding`, `Content-Language`, and `Expires` as supported copy metadata. R2 custom metadata is represented through `x-amz-meta-*` headers. For a byte-preserving archive mirror, use `COPY` and do not supply replacement metadata unless the product explicitly wants destination metadata changed.

R2 does not implement object tagging operations and the S3 compatibility table marks `x-amz-tagging` and `x-amz-tagging-directive` as unsupported for `CopyObject`. Do not describe tags as preserved by this design; the current product requirements do not rely on tags.

### ETag and integrity behavior

`CopyObject` returns an ETag for the destination object, but the implementation must not blindly equate an ETag with a universal content hash:

- R2 documents MD5 as the default checksum for non-multipart objects in the Workers API.
- For multipart objects, R2 documents the multipart ETag form as the hash of concatenated part MD5 values followed by `-<part-count>`, rather than the MD5 of the complete byte stream.
- A successful server-side copy is expected to preserve object bytes, but destination ETag equality is the practical verification signal only when the source and destination use comparable R2 ETag semantics. For robust conflict handling, compare source and destination size and ETag, and use an explicit checksum when the source metadata/report contains one. Do not calculate a new MD5 over a large archive just to decide whether to copy unless the implementation intentionally pays that read cost.

The existing requirement "same content skips; different content reports conflict" should therefore be implemented as:

1. `HeadObject` the source and destination without writing.
2. If destination is absent, issue `CopyObject`.
3. If destination size and comparable ETag/checksum match, record `skipped_identical`.
4. If destination exists but differs, record `conflict` and skip by default.
5. If the preflight result becomes stale, use `cf-copy-destination-if-none-match: *` for the absent-destination case where supported by the selected SDK, or treat a destination-precondition failure as a conflict and do not retry it as a network failure.

R2 documents that source and destination conditional checks are not atomic relative to each other: source conditions are checked when the source is selected, while destination conditions are checked when the destination is committed. The result is still useful for preventing ordinary overwrites, but it is not a full compare-and-swap transaction across both objects.

## Streaming `GetObject` then `PutObject`

### Operation shape

The application reads the source object body with `GetObject` and sends those bytes to `PutObject` or a multipart upload at the same destination key. To preserve the archive exactly, the stream must not be decompressed or reconstructed.

The minimum operation permissions are:

- Source: list permission for the initial scan, plus object read (`HeadObject` and/or `GetObject`) for each source object.
- Destination: bucket existence/readability check and object write (`PutObject`, or multipart create/upload/complete/abort for a multipart fallback), plus `HeadObject` if checking for an existing object before the write.

The R2 API token documentation groups these as `Object Read only` (read/list) and `Object Read & Write` (read/write/list) for selected buckets. The R2 temporary-credential documentation also lists `HeadObject`, `GetObject`, and listing as read actions and `PutObject`, `CopyObject`, and multipart actions as write actions.

### Advantages

- Works when the source and destination need different S3 clients or credential sets, provided the server is allowed to hold both connection configurations. This is not part of the current MVP, which deliberately uses one connection configuration.
- Gives the application direct control over multipart upload thresholds, part size, retrying individual parts, and checksum calculation.
- Can be used as a compatibility fallback if a client library exposes `GetObject`/`PutObject` but not a usable R2 `CopyObject` request.

### Costs and risks

- The server relays every byte. For approximately 14.5 GB and about 1,407 objects, this adds application egress/ingress, CPU scheduling, open-connection time, and pressure on the 120-second per-object timeout.
- A naive `read()` or in-memory buffer is unsafe for large archives. Use a bounded stream or disk-backed multipart staging; disk staging conflicts with the requirement to keep R2 connection data in memory only but not necessarily with object data, so the deployment volume and cleanup policy would need to be explicit.
- A failed upload can leave an incomplete multipart upload. The job must abort it and record the failure; otherwise unfinished uploads may remain until lifecycle cleanup.
- `PutObject` does not automatically copy source metadata. The code must read source metadata and explicitly pass the allowed metadata to the upload, or intentionally document that only bytes/key are preserved.
- A streamed multipart upload can produce a destination ETag that differs from the source even when bytes are identical because the part boundaries or upload mode differ. Use a strong checksum or a byte comparison policy if exact integrity proof is required.
- Retrying a stream requires a replayable source. An already-consumed network body cannot necessarily be retried; reopen the source object or use multipart part retries.

## Permissions and bucket/account constraints

### Same-account, cross-bucket

For the planned MVP, source and all model target buckets should be in the same Cloudflare account and covered by one R2 API token scoped to the required buckets. One client configured with the account-level R2 endpoint (`https://<ACCOUNT_ID>.r2.cloudflarestorage.com`) can address the source and destination bucket names in S3 requests. The token must have read/list access to the source bucket and write access to each target bucket; a read-only token cannot perform `CopyObject`.

Cloudflare's public S3 compatibility documentation confirms that `CopyObject` is implemented and that `x-amz-copy-source` is supported. It does not, on the pages reviewed, state a separate cross-bucket or cross-account policy model for R2 `CopyObject`. Therefore, do not infer that a token scoped only to the source bucket can write to an arbitrary destination bucket; validate each target bucket with a non-writing `HeadBucket`/`HeadObject` test and require the target bucket to already exist.

### Cross-account

Do not make cross-account server-side copy a requirement or assumption for this task. R2 API token resources are identified by Cloudflare account ID and bucket, and Cloudflare's token model is account-scoped. The reviewed R2 docs do not document an S3-style cross-account bucket policy flow that would allow one R2 `CopyObject` request to read a source in account A and write a destination in account B.

If cross-account copying is later required, the safe documented design is application-mediated streaming with independently authenticated clients: source client/credentials read account A, destination client/credentials write account B. That requires a product/security decision to support two credential sets and is explicitly outside the current PRD. Do not attempt to solve it by putting a second secret in the current single-connection configuration or by logging either credential.

### Temporary credentials caveat

R2 temporary credentials are bound to exactly one bucket and do not support cross-bucket access within a single credential. They are therefore not suitable for a single credential that performs the current source-plus-many-target-bucket workflow. Use an appropriately scoped long-lived R2 API token held in process memory for the MVP, or mint separate temporary credentials per bucket only in a future design that explicitly supports that complexity.

### Bucket existence

The product requires no automatic bucket creation. R2 has an optional `cf-create-bucket-if-missing` upload extension, but it must not be sent by this application. Test the target bucket first, and classify `NoSuchBucket` or an authorization failure as a reported skip rather than retrying it as a transient network error.

## Whole `.tar.gz` object and key-preservation implications

- Copy the object key exactly; do not use the model name as a replacement prefix. If one source archive contains multiple models, issue one copy per model target bucket, each with the same source key.
- The archive body is opaque. The copy path must not open tar members, change compression, add a manifest, or normalize path separators.
- Preserve source metadata with `x-amz-metadata-directive: COPY` unless the application has a specific reason to set destination metadata. In particular, do not accidentally remove `Content-Encoding` or change `Content-Type` while mirroring.
- R2's upload documentation lists single-request uploads up to 5 GiB and multipart objects up to 5 TiB. The current source set is described as `.tar.gz` archives, so object size must be checked before selecting a fallback. Do not assume a single streamed `PutObject` can handle every archive.
- For objects beyond the selected single-operation limit, prefer R2 `CopyObject` if the endpoint accepts the complete object; otherwise use `UploadPartCopy` (server-side multipart copy) or a bounded streaming multipart upload. `UploadPartCopy` is implemented in R2, but its conditional-copy feature set is more limited than `CopyObject` according to the compatibility table.
- Preserve the source key in the report and in every copy result. Report one synchronization attempt per `(model, source_key, target_bucket)` association, while deduplicating the physical source object size in model totals according to the PRD's multi-model reporting rule.

## Comparison for this MVP

### Preferred path: server-side copy

1. Verify source object metadata and target bucket/object state with read-only requests.
2. If destination matches, record `skipped_identical`.
3. If destination differs, record `conflict` and skip by default.
4. If destination is absent, call `CopyObject` with the exact source key and `COPY` metadata behavior.
5. Verify/record the destination response and classify failures; retry only transient network/service failures, not permission, missing-bucket, or precondition errors.

### Fallback path: streaming

Use only when server-side copy is unavailable for the selected endpoint/client or when a future cross-account design supplies separate authorized clients. Stream in bounded chunks, preserve metadata explicitly, use multipart upload for large objects, abort incomplete uploads, and verify the resulting size/checksum. Keep the same destination conflict checks and exact key mapping as the preferred path.

## Sources

### Cloudflare

- [S3 API compatibility](https://developers.cloudflare.com/r2/api/s3/api/) — current R2 implementation table; `CopyObject`, `x-amz-copy-source`, metadata directives, conditional operations, and supported/unsupported headers.
- [S3 API compatibility (Markdown)](https://developers.cloudflare.com/r2/api/s3/api/index.md) — fetched 2026-10-06; same operation matrix in Markdown form.
- [S3 API extensions](https://developers.cloudflare.com/r2/api/s3/extensions/) — R2 `MERGE` metadata directive, destination conditional-copy headers, and non-atomicity caveat.
- [Authentication / API tokens](https://developers.cloudflare.com/r2/api/s3/tokens/) — R2 API token permission groups and bucket scoping.
- [Temporary credentials](https://developers.cloudflare.com/r2/api/s3/temporary-credentials/) — one-bucket credential scope and explicit S3 action list.
- [Upload objects](https://developers.cloudflare.com/r2/objects/multipart-objects/) — single versus multipart size limits, multipart ETag behavior, and cleanup considerations.
- [Storage classes](https://developers.cloudflare.com/r2/buckets/storage-classes/) — official `aws s3api copy-object` example using `x-amz-copy-source`.
- [R2 extensions (Markdown)](https://developers.cloudflare.com/r2/api/s3/extensions/index.md) — fetched 2026-10-06; confirms optional auto-create behavior and destination conditional-copy details.

## Critical caveats

1. Cloudflare's reviewed R2 docs confirm `CopyObject` support but do not clearly document a cross-account R2 copy policy. Treat cross-account server-side copy as unsupported/unverified; use separate-client streaming only after an explicit security/product decision.
2. Do not use R2's `cf-create-bucket-if-missing`; the application must never auto-create target buckets.
3. ETag equality is not a universal content-equality proof for multipart objects. Record size and ETag/checksum semantics, and do not claim byte identity solely from an ETag when upload modes differ.
4. Destination conditional headers are documented as beta and are not atomic with source conditional headers. A copy can still race with external writers; classify precondition failures as conflicts and never overwrite by default.
5. For large objects, check limits and multipart behavior before execution. R2 documents 5 GiB single uploads and 5 TiB multipart objects; use server-side multipart copy or controlled streaming multipart upload when needed.
6. Temporary R2 credentials are one-bucket only, so they cannot implement the MVP's one-connection, source-plus-many-target-buckets flow.
