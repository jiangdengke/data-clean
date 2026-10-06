# Research: FastAPI + React SPA single-container deployment

- **Query**: Minimal production pattern for this project: a FastAPI backend serving a built React SPA from one Docker container, process-local background jobs, HttpOnly session cookies, and Docker Compose volumes.
- **Scope**: Mixed (project requirements plus official FastAPI, Starlette, Docker, and Python documentation)
- **Date**: 2026-10-06

## Findings

### Recommended deployment shape

Use one production image with two build stages:

1. A Node stage installs the React dependencies and runs the production build.
2. A Python runtime stage installs only backend runtime dependencies, copies the built frontend `dist/`, and starts one Uvicorn process serving FastAPI.

Keep application source and frontend build output inside the image in production. Mount only a named volume, for example `/var/lib/r2-cleaner`, for reports, task status/results, and the model-to-target-bucket mapping. This follows Docker's multi-stage-build guidance (copy only production artifacts into the final stage) and Compose's named-volume model (the volume is created/reused independently of the container lifecycle).

FastAPI's frontend support is a good fit for the single-origin SPA arrangement:

```python
app.include_router(api_router, prefix="/api")
app.frontend("/", directory="dist")
```

The API routes must be registered before the frontend route. `app.frontend()` checks normal path operations first and can fall back to `index.html` for browser navigation, so React client-side routes work without a second web server. If the selected FastAPI version does not provide `app.frontend()`, use `StaticFiles` with an explicit HTML fallback while preserving the same `/api` versus `/` separation.

### Background job convention for the MVP

The scan and sync jobs are long-running and must continue after the browser closes. Return `202 Accepted` with a generated job ID, keep a single process-local job registry/lock, and expose authenticated status and result endpoints that read persisted JSON under the data volume.

For the first version, run exactly one Uvicorn worker/container process. A process-local `asyncio.Lock` (or equivalent guarded state) is sufficient to enforce the requirement that only one scan and one sync run at a time; multiple workers would each have a separate lock and registry. Prefer a retained `asyncio.Task` or a dedicated controlled worker thread for these long operations rather than relying on an untracked fire-and-forget coroutine. Record `queued`, `running`, `completed`, and `failed` state and write progress/result files atomically enough that a restart cannot expose a partially written JSON document.

FastAPI `BackgroundTasks` is appropriate for small work that runs after the response, but the official documentation cautions that heavy computation or work needing independent process/server execution belongs in a queue system such as Celery. For this MVP, a process-local task is acceptable because the PRD explicitly keeps one service and only requires continuation after page closure, not survival across process/container restart. A restart loses in-memory credentials and active execution; persisted status should mark an interrupted job as failed or interrupted on the next startup rather than claiming it completed.

Do not pass R2 secrets in job IDs, task payloads written to disk, reports, or ordinary logs. The job should resolve the current in-memory connection settings when it starts and should fail clearly if the settings were cleared or the process restarted.

### Session cookie convention

Use Starlette's `SessionMiddleware` or an equivalent server-side session implementation at the same origin as the SPA. The default signed-cookie middleware is small and avoids a database for this single-admin MVP:

- Put only a minimal authenticated marker and expiry-related state in the session; signed cookie contents are readable by the client and are not encryption.
- Inject a random session signing key from the deployment environment or a Compose secret; never use a repository default.
- Keep `HttpOnly` enabled (Starlette sets it for the session cookie).
- Use `SameSite=Lax` for the same-origin UI and `Secure`/`https_only=True` in production HTTPS deployments. Allow insecure cookies only for explicitly local HTTP development.
- Use a finite `max_age`, clear the cookie on logout, and require the authenticated dependency on every report, task, settings, and sync endpoint.

Because POST endpoints trigger scans, connection tests, and syncs, add a same-origin CSRF defense (at minimum validate `Origin`/`Referer`, or use a CSRF token) if the session cookie is used as the only write authorization. Same-origin deployment removes CORS complexity, but it does not by itself eliminate browser cross-site request risks.

HTTPS termination should normally happen at a reverse proxy/load balancer in front of the container. Configure trusted forwarded headers only for the known proxy path; do not blindly trust client-supplied `X-Forwarded-*` headers. The proxy must preserve the external HTTPS scheme so the backend emits secure cookies and generates correct URLs.

### Compose and volume conventions

Keep the production Compose service minimal:

- publish only the application port (and preferably expose it through a reverse proxy rather than directly to the public internet);
- set a restart policy such as `unless-stopped` or `always`;
- inject the admin password and session secret from deployment configuration, preferably Compose secrets for sensitive values, while honoring this project's requirement that R2 connection values are entered at runtime and remain process-memory-only;
- mount one named volume at the controlled data directory;
- do not mount the application source tree over the image in production.

The named volume should contain only non-secret durable state: JSON scan reports, sync results, job metadata, and model-to-bucket mappings. Set restrictive container/file permissions and ensure download APIs authorize before opening any report. Do not put R2 endpoint, access key ID, secret, or the admin password into that volume.

The volume is durable across `docker compose up` recreation, but it is not a backup. Define an explicit backup/restore procedure for the host or external volume. Rebuilding the image must not remove the volume; removing the volume intentionally destroys the reports and mappings.

### Minimal request/data flow

1. Browser requests `/`; FastAPI serves the built SPA and static assets.
2. Browser submits login; backend sets the signed, HttpOnly session cookie.
3. Authenticated browser submits connection settings over HTTPS; backend keeps R2 credentials only in process memory and returns no secret values to the SPA.
4. Scan/sync start endpoints validate state, acquire the single-job guard, persist a job record, and return a job ID without waiting for the full operation.
5. The in-process job updates persisted status/progress and writes a credential-free report/result.
6. The SPA polls authenticated status endpoints and can fetch the final report after a page reload or browser close.

## External References

- [FastAPI Frontend](https://fastapi.tiangolo.com/tutorial/frontend/) — `app.frontend()` serves prebuilt React/Vite output, gives API path operations precedence, and supports SPA navigation fallback.
- [FastAPI Static Files](https://fastapi.tiangolo.com/tutorial/static-files/) — fallback option using `StaticFiles` when the deployed FastAPI version lacks `app.frontend()`.
- [FastAPI Background Tasks](https://fastapi.tiangolo.com/tutorial/background-tasks/) — documents response-after tasks and cautions that heavy work may need a separate queue; relevant to choosing a controlled MVP task runner.
- [Starlette Middleware: SessionMiddleware](https://starlette.dev/middleware/) — documents signed cookie sessions, mandatory HttpOnly behavior, `same_site`, `max_age`, and `https_only`.
- [FastAPI Behind a Proxy](https://fastapi.tiangolo.com/advanced/behind-a-proxy/) — proxy forwarding and HTTPS deployment considerations.
- [Docker Multi-stage Builds](https://docs.docker.com/build/building/multi-stage/) — build frontend artifacts separately and copy only production artifacts into the runtime image.
- [Docker Compose Volumes](https://docs.docker.com/reference/compose-file/volumes/) — named-volume declaration, reuse, and explicit service access.
- [Docker Compose Production](https://docs.docker.com/compose/how-tos/production/) — production overrides, removing application-code mounts, restart policy, and rebuild/recreate workflow.
- [Docker Compose Secrets](https://docs.docker.com/compose/how-tos/use-secrets/) — safer file-mounted handling for passwords/API keys than exposing them to all processes as environment variables; a deployment option for the admin/session secrets.
- [Python asyncio Tasks](https://docs.python.org/3/library/asyncio-task.html) — retain strong references to fire-and-forget tasks and avoid untracked tasks disappearing or hiding failures.

## Related Project Requirements

- `.trellis/tasks/10-06-r2-model-cleanup-sync/prd.md` — requires FastAPI + React, one production container serving the SPA, authenticated HttpOnly/Secure/SameSite cookies, process-memory-only R2 settings, background scan/sync progress, one active task at a time, and a Docker volume for reports/results/mappings.
- No application source tree or package-specific `.trellis/spec/` guidelines exist yet; deployment choices above are therefore based on the task PRD and official framework/container documentation.

## Caveats / Not Found

- Process-local jobs and locks are intentionally an MVP trade-off. They do not provide execution continuity across container restarts and are unsafe if the deployment later runs multiple worker processes or replicas without moving job state/locking to a shared durable system.
- `SessionMiddleware` signs but does not encrypt cookie contents. Never place R2 credentials or other sensitive settings in the session.
- `Secure` cookies require HTTPS. Local HTTP development needs a deliberate development setting; production must not expose the login or connection form over plain HTTP.
- The repository has no business code, dependency pins, Dockerfile, Compose file, or reverse-proxy configuration yet, so exact package versions and health-check commands remain implementation decisions.
- Compose named volumes persist data but do not replace backups, access control, or report-retention policy.
