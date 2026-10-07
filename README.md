# R2 Model Sync

This project provides an authenticated Cloudflare R2 workflow for scanning multiple source buckets, discovering
model names from `.tar.gz` member paths under `roots/primary/<model-name>/`, assigning each `(source bucket,
model)` pair to a manually created target bucket, and synchronizing complete objects with server-side R2 copies.
Scan status, mappings, and credential-free reports are written to a configurable data directory.

Synchronization is explicit and never creates, deletes, or overwrites objects. Existing target keys are skipped.
Target buckets must be created in advance. R2 credentials remain only in server process memory.

## Local development

Requirements: Python 3.11+, Node.js 22+, and npm.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
export ADMIN_PASSWORD='choose-a-local-password'
export COOKIE_SECURE=false
export DATA_DIRECTORY=./data
```

In another terminal, build the React frontend and start FastAPI:

```bash
cd frontend
npm install
npm run build
cd ..
uvicorn backend.main:app --reload
```

Open <http://localhost:8000>. R2 credentials entered in the connection form remain in server process memory
and are cleared on restart. The data directory contains only job metadata and reports; do not put secrets in it.

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

The container intentionally runs one Uvicorn process because active scan and sync locking are process-local.
A restart marks queued or running jobs as interrupted, and R2 credentials must be entered again.
