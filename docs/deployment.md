# Deployment Notes

The application is deployed as one container: a Node build stage produces the React `dist` directory, and
the Python runtime serves that output through FastAPI. The container should run one Uvicorn process because
the scan lock is process-local in this MVP.

Set `ADMIN_PASSWORD` through the deployment environment or a secret manager. Set `COOKIE_SECURE=true` when
the service is reached through HTTPS. R2 endpoint and credentials are entered after login and remain only in
the server process memory.

Mount the named Compose volume at `/var/lib/r2-model-scanner`. It contains only job metadata and scan reports.
A restart marks queued or running jobs as interrupted; it does not claim that an interrupted scan completed.
After a page reload, the dashboard restores the latest persisted scan status and report without restoring R2
credentials to the browser.

This slice has no R2 write path. It does not upload, copy, overwrite, delete, or create buckets.

## Oracle VM deployment

The current Oracle deployment uses the existing `oracle` SSH alias and runs the application on the
ARM64 Ubuntu VM at `/home/ubuntu/r2-model-scanner`. Docker Compose binds the application only to
`127.0.0.1:8000` on the VM, so it is not directly exposed as a new public port and does not change the
existing Nginx sites.

The deployment environment file is stored on the VM with mode `0600` and contains the administrator
password and `COOKIE_SECURE=false`. This HTTP setting is temporary and is appropriate only when accessing
the application through an SSH tunnel:

```bash
ssh -N -L 8000:127.0.0.1:8000 oracle
```

With that tunnel open, visit <http://127.0.0.1:8000>. The named Docker volume
`r2-model-scanner_r2-model-scanner-data` stores the credential-free job and report data.

For public access, configure a dedicated HTTPS Nginx virtual host that proxies to `127.0.0.1:8000`,
change `COOKIE_SECURE` to `true`, and reload Nginx. Do not expose the application through plain public
HTTP. The Oracle deployment has not enabled a public hostname or changed the existing reverse-proxy
configuration.
