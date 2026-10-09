# R2 Model Sync

This project provides an authenticated Cloudflare R2 workflow for scanning multiple source buckets, discovering
model names from `.tar.gz` member paths under `roots/primary/<model-name>/`, assigning each `(source bucket,
model)` pair to a manually created target bucket, and synchronizing complete objects with server-side R2 copies.
Scan status, mappings, and credential-free reports are written to a configurable data directory.

Synchronization is explicit and never creates, deletes, or overwrites objects. Existing target keys are skipped and recorded as conflicts. Target buckets must be created in advance. Runtime R2 and Cloudflare Queue credentials are injected only through deployment environment/secrets and are never requested by or persisted in the browser/data volume.

Continuous routing is an explicit persisted administrator toggle. It consumes R2 `object-create` events through the Cloudflare Queue HTTP pull API, acknowledges only after SQLite acceptance, classifies each new/changed archive once, and copies the complete archive to every distinct saved mapping target. Missing mappings remain pending. Periodic metadata-only reconciliation is a backstop. Historical backfill is manual, paginated, checkpointed, bounded, and pausable/resumable; it never starts automatically.

## Local development

Requirements: Python 3.11+, Node.js 22+, and npm.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
export ADMIN_PASSWORD='choose-a-local-password'
export COOKIE_SECURE=false
export DATA_DIRECTORY=./data
export R2_ENDPOINT='https://<account-id>.r2.cloudflarestorage.com'
# default reference credentials stay deployment-injected:
# R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
# For credential_ref=team_a, inject R2_CREDENTIAL_TEAM_A_ACCESS_KEY_ID and
# R2_CREDENTIAL_TEAM_A_SECRET_ACCESS_KEY through your secret manager.
# Also inject CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_QUEUE_ID and CLOUDFLARE_API_TOKEN.
```

In another terminal, build the React frontend and start FastAPI:

```bash
cd frontend
npm install
npm run build
cd ..
uvicorn backend.main:app --reload
```

Open <http://localhost:8000>. The connection form uses one card per source bucket, with its endpoint and a
non-secret `credential_ref`. Add/remove cards, save, and test source access. The `default` reference uses the
legacy R2 variables; other references use `R2_CREDENTIAL_<NORMALIZED_REF>_ACCESS_KEY_ID` and
`R2_CREDENTIAL_<NORMALIZED_REF>_SECRET_ACCESS_KEY`. Runtime secrets are absent from UI, API models, SQLite,
reports, and logs. The data directory contains JSON reports plus `incremental.sqlite3` (WAL); do not put secrets in it.

Run focused checks:

```bash
pytest
cd frontend && npm run lint && npm run build
```

## Docker Compose deployment

Set the administrator password outside the repository, then build and start the single application container:

```bash
export ADMIN_PASSWORD='use-a-secret-manager-value'
export COOKIE_SECURE=true
docker compose up --build -d
```

The named `r2-model-scanner-data` volume stores credential-free jobs, mappings, and reports under
`/var/lib/r2-model-scanner`. Put HTTPS termination in a trusted reverse proxy before exposing this service
publicly. `COOKIE_SECURE=true` requires HTTPS; use `false` only for local HTTP development.

The production image includes checksum-verified rclone `v1.74.3` binaries for `amd64` and `arm64`, but transfer selection remains the backward-compatible `boto3` default. Verify the bundled binary without contacting R2:

```bash
ADMIN_PASSWORD=local-readiness-check docker compose run --rm --no-deps r2-model-scanner rclone version
```

To opt in, set `R2_TRANSFER_ADAPTER=rclone`; the image default `RCLONE_BINARY_PATH=/usr/local/bin/rclone` and bounded timeout/retry/output limits are shown in `.env.example`. Rclone readiness requires that path to be an executable file. Keep all R2 credentials in deployment Secret environment variables. The application passes them directly to each configless rclone subprocess and does not create or persist `rclone.conf`. Set `R2_TRANSFER_ADAPTER=boto3` and restart the service to roll back. See [Deployment Notes](docs/deployment.md#rclone-transfer-adapter) for the behavior and limitations.

The container intentionally runs one Uvicorn process because active scan and sync locking are process-local.
On restart, the deployment reinjects the R2 and Cloudflare Queue runtime secrets, while the SQLite-backed
continuous-routing toggle and work queues persist; if continuous mode was enabled, it resumes automatically.
Legacy queued or running scan/sync jobs are still marked interrupted, but credentials are not re-entered in the
UI or stored in the data volume.
