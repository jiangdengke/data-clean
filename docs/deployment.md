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
preflight passes, the background sync worker uses server-side `CopyObject`. It never creates buckets, deletes
source objects, or overwrites an existing target key; existing target keys are recorded as skipped. A
successful `HeadBucket` preflight proves bucket access, not write permission, so individual copy failures
remain isolated in the sync report.

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
`runtime_ready` must be `true` before enabling continuous routing. The first historical backfill and the
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
