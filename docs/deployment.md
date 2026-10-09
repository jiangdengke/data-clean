# Deployment Notes

The application is deployed as one container: a Node build stage produces the React `dist` directory, and
the Python runtime serves that output through FastAPI. The container should run one Uvicorn process because
the scan and sync locks are process-local in this MVP.

Set `ADMIN_PASSWORD` through the deployment environment or a secret manager. Set `COOKIE_SECURE=true` when
the service is reached through HTTPS. Each source bucket is configured in its own UI card with an endpoint and
non-secret `credential_ref`. `default` uses `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`; a reference such as
`team_a` uses `R2_CREDENTIAL_TEAM_A_ACCESS_KEY_ID` and `R2_CREDENTIAL_TEAM_A_SECRET_ACCESS_KEY`. Set
`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_QUEUE_ID`, and `CLOUDFLARE_API_TOKEN` through deployment secrets.
The form stores only profiles; no runtime secret is accepted from or returned to the browser. The Queue token
needs Cloudflare Queues read and write permissions.

Mount the named Compose volume at `/var/lib/r2-model-scanner`. It contains job metadata, saved mappings,
credential-free reports, and the WAL SQLite incremental index; it must never contain credentials.
The persisted continuous toggle resumes after restart when runtime secrets are ready. Queue events are accepted
into SQLite before acknowledgment. Historical backfill is started explicitly by an administrator, and its
continuation token/checkpoint and paused state survive restart.
After a page reload, the dashboard restores the latest persisted scan status and report without restoring R2
credentials to the browser.

The scan phase and target-bucket preflight are read-only. Synchronization is a separate explicit action: after
the administrator saves a mapping for every discovered `(source bucket, model)` pair and target-bucket
preflight passes, the background sync worker uses the selected one-object transfer adapter. It never creates
buckets, deletes source objects, or overwrites an existing target key; existing target keys are recorded as
skipped. A successful `HeadBucket` preflight proves bucket access, not write permission, so individual copy
failures remain isolated in the sync report.

## rclone transfer adapter

The production image installs checksum-verified rclone `v1.74.3` for Linux `amd64` and `arm64`. The adapter
is disabled by default: `R2_TRANSFER_ADAPTER` defaults to `boto3`, preserving the existing boto3
`CopyObject` behavior. The non-secret deployment controls are:

| Variable | Default | Constraint |
| --- | --- | --- |
| `R2_TRANSFER_ADAPTER` | `boto3` | Set to `rclone` only for explicit opt-in. |
| `RCLONE_BINARY_PATH` | `/usr/local/bin/rclone` | Must be an absolute path in rclone mode. |
| `RCLONE_TRANSFER_TIMEOUT_SECONDS` | `3600` | Per-object subprocess timeout; integer from 1 through 86400. |
| `RCLONE_OUTPUT_LIMIT_BYTES` | `65536` | Maximum captured subprocess output; integer from 1024 through 1048576. |
| `RCLONE_LOW_LEVEL_RETRIES` | `3` | rclone low-level retries; integer from 0 through 100. Application retries remain authoritative. |

R2 credentials remain in deployment Secret environment variables. The application resolves the selected
source profile and gives each child process a private `RCLONE_CONFIG_R2TRANSFER_*` environment. It invokes
rclone with `--config /dev/null`; do not create or mount a persistent `rclone.conf`, and do not put credentials
in `.env.example`, the data volume, arguments, reports, or logs. The child receives only its private configless
rclone environment, not the service user's existing rclone environment. Captured JSON output is drained with a
strict memory cap, is never persisted, and provider output is never copied into exceptions or reports.

The adapter executes exactly one configless `rclone copyto` for one whole object, with `--ignore-existing` and
`--s3-no-check-bucket`. It does not use `sync`, `move`, or delete operations, and it cannot create buckets.
Source and target buckets must be in the same Cloudflare account and accessible through the source profile's
single endpoint and credential set. Unsupported endpoints, a mismatched source profile, invalid bucket names,
and objects larger than 5 GiB fail closed before transfer. Cross-account or streamed fallback is not enabled.

Before transfer, application code HEADs the source and checks its expected size/fingerprint, then HEADs the
target and skips any existing key. After a successful rclone process, it HEADs the target and verifies its size.
Rclone exit code 0 does not by itself mean copied: structured output can classify the result as skipped, and no
positive copied record is also treated as skipped. These checks are not atomic. The source can change or the
target can appear between HEAD and copy, so the existing HEAD/copy race remains; post-copy size verification
cannot prove which writer won a concurrent race.

Cloudflare R2 limits `CopyObject` to 5 GiB and does not support multipart copy. This adapter's current
same-account mode uses that server-side operation and rejects larger objects; it does not download and
multipart-upload them. Rclone replaces only the final object-transfer call. It does not replace Cloudflare
Queue consumption, the SQLite durable index, archive classification, mapping decisions, or application retry
state, and it does not avoid the first archive read needed to classify each new or changed archive.
Configuration and topology failures are terminal route states; subprocess/provider failures and timeouts remain
subject to the application's bounded retry schedule.

Build the local image and verify binary readiness without contacting R2:

```bash
ADMIN_PASSWORD=local-readiness-check docker compose build r2-model-scanner
ADMIN_PASSWORD=local-readiness-check docker compose run --rm --no-deps r2-model-scanner rclone version
```

To enable the adapter, set `R2_TRANSFER_ADAPTER=rclone` in deployment configuration and recreate the service.
To roll back, set `R2_TRANSFER_ADAPTER=boto3` and recreate it; no index or mapping migration is needed. A
binary version check proves packaging only, not credentials, topology, bucket permissions, or provider copy
behavior.

## Cloudflare Queue and R2 notification setup

Create the main Queue and a separate dead-letter queue (DLQ) manually before enabling continuous routing.
In the Cloudflare dashboard, open **Queues**, create `<QUEUE_NAME>`, create `<DLQ_NAME>` as a second queue,
and configure the main Queue to send exhausted messages to that DLQ. Wrangler examples for the same manual
provisioning are shown below; they are examples only and are not executed by this application or by this
documentation:

```bash
npx wrangler queues create <QUEUE_NAME>
npx wrangler queues create <DLQ_NAME>
```

On the main Queue, add an HTTP pull consumer. The account API token used for this operation and for runtime
pull/ack requests must have Cloudflare account-level **Queues read and write** permissions. Inject that token
only as `CLOUDFLARE_API_TOKEN`:

```bash
npx wrangler queues consumer http add <QUEUE_NAME>
```

For every source R2 bucket, add an `object-create` event notification whose destination is `<QUEUE_NAME>`.
Use the dashboard's R2 bucket **Settings → Event notifications** flow (or the corresponding Wrangler notification
command for the installed Wrangler version), filter the rule to the `.tar.gz` suffix, and optionally add the
source prefix used by the bucket. `object-create` covers `PutObject`, `CopyObject`, and
`CompleteMultipartUpload`, including overwrites; it does not identify the model inside the archive.

Configure the runtime through deployment environment variables or a secret manager only:
`R2_ENDPOINT` is selected per source card, while `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` back the
legacy `default` reference. For other references, inject
`R2_CREDENTIAL_<NORMALIZED_REF>_ACCESS_KEY_ID` and
`R2_CREDENTIAL_<NORMALIZED_REF>_SECRET_ACCESS_KEY`. Keep these values out of the browser, data volume,
reports, and logs. After deployment, authenticate to the application and check `GET /api/incremental/status`;
`runtime_ready` must be `true` before enabling continuous routing. In rclone mode this also verifies that
`RCLONE_BINARY_PATH` is an executable file. The first historical backfill and the
continuous-routing toggle are manual UI actions. Enable notifications before starting the first backfill so
new objects are not missed, then use the status page to monitor readiness and progress.

This application does not create or modify Cloudflare Queues, DLQs, consumers, or R2 notification rules. No
Cloudflare operation, provider call, historical backfill, or real object copy is performed by executing the
documentation examples; all resource provisioning and the first UI actions remain operator-controlled.

## Oracle VM deployment

The current Oracle deployment uses the existing `oracle` SSH alias and runs the application on the
ARM64 Ubuntu VM at `/home/ubuntu/r2-model-scanner`. Docker Compose binds the application only to
`127.0.0.1:8000` on the VM, so it is not directly exposed as a new public port and does not change the
existing Nginx sites.

The deployment environment file is stored on the VM with mode `0600` and contains the administrator
password and `COOKIE_SECURE=true`. Nginx terminates HTTPS for the public hostname
<https://dataclean.lsynb.me> and proxies requests to the loopback-only application port.

The application can still be reached through an SSH tunnel for origin-only maintenance checks:

```bash
ssh -N -L 8000:127.0.0.1:8000 oracle
```

The tunnel reaches the application directly and bypasses Nginx. Because production cookies are marked
`Secure`, use the public HTTPS hostname for normal browser login. The named Docker volume
`r2-model-scanner_r2-model-scanner-data` stores the credential-free job and report data.

The dedicated Nginx virtual host redirects public HTTP to HTTPS, serves the ACME challenge path from
`/var/www/certbot`, and proxies the application to `127.0.0.1:8000` with forwarded HTTPS headers. The
origin certificate is managed by Certbot for `dataclean.lsynb.me`; Cloudflare remains the public DNS and
proxy layer. Do not expose the application through plain public HTTP.
