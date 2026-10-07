# Deployment Notes

The application is deployed as one container: a Node build stage produces the React `dist` directory, and
the Python runtime serves that output through FastAPI. The container should run one Uvicorn process because
the scan and sync locks are process-local in this MVP.

Set `ADMIN_PASSWORD` through the deployment environment or a secret manager. Set `COOKIE_SECURE=true` when
the service is reached through HTTPS. R2 endpoint and credentials are entered after login and remain only in
the server process memory.

Mount the named Compose volume at `/var/lib/r2-model-scanner`. It contains only job metadata, saved mappings,
and credential-free scan/sync reports.
A restart marks queued or running jobs as interrupted; it does not claim that an interrupted scan completed.
After a page reload, the dashboard restores the latest persisted scan status and report without restoring R2
credentials to the browser.

The scan phase and target-bucket preflight are read-only. Synchronization is a separate explicit action: after
the administrator saves a mapping for every discovered `(source bucket, model)` pair and target-bucket
preflight passes, the background sync worker uses server-side `CopyObject`. It never creates buckets, deletes
source objects, or overwrites an existing target key; existing target keys are recorded as skipped. A
successful `HeadBucket` preflight proves bucket access, not write permission, so individual copy failures
remain isolated in the sync report.

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
