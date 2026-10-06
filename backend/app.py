"""FastAPI application for authenticated, read-only R2 model scanning."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import secrets
from pathlib import Path
from typing import cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import AppSettings
from .models import ConnectionRequest, ConnectionSettings, ConnectionTestResponse, ConnectionView, LoginRequest, ScanReport, SessionResponse, StartScanResponse
from .r2_client import create_r2_client
from .scanner import build_scan_report, scan_object_with_retries
from .storage import JobRecord, JobStorage, current_timestamp

SESSION_COOKIE_NAME = "r2_clean_session"


class SessionStore:
    """Keep opaque authenticated session tokens in process memory only."""

    def __init__(self, max_age_seconds: int) -> None:
        self.max_age = timedelta(seconds=max_age_seconds)
        self._sessions: dict[str, datetime] = {}

    def create_session(self) -> str:
        session_token = secrets.token_urlsafe(32)
        self._sessions[session_token] = datetime.now(timezone.utc) + self.max_age
        return session_token

    def is_authenticated(self, session_token: str | None) -> bool:
        if not session_token:
            return False
        expiration = self._sessions.get(session_token)
        if expiration is None:
            return False
        if expiration <= datetime.now(timezone.utc):
            self._sessions.pop(session_token, None)
            return False
        return True

    def remove_session(self, session_token: str | None) -> None:
        if session_token:
            self._sessions.pop(session_token, None)


def require_authenticated_user(request: Request) -> None:
    """Protect every data and action endpoint with the server-side session."""

    session_store = cast(SessionStore, request.app.state.session_store)
    if not session_store.is_authenticated(request.cookies.get(SESSION_COOKIE_NAME)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def ensure_same_origin(request: Request) -> None:
    """Reject cross-origin browser writes that could reuse the session cookie."""

    browser_origin = request.headers.get("origin") or request.headers.get("referer")
    if browser_origin is None:
        return
    parsed_browser_origin = urlparse(browser_origin)
    expected_origin = f"{request.url.scheme}://{request.url.netloc}"
    received_origin = f"{parsed_browser_origin.scheme}://{parsed_browser_origin.netloc}"
    if received_origin != expected_origin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-origin request rejected")


def get_connection_view(connection_settings: ConnectionSettings | None) -> ConnectionView:
    """Return connection metadata without exposing either credential."""

    if connection_settings is None:
        return ConnectionView(configured=False)
    return ConnectionView(configured=True, endpoint=connection_settings.endpoint, source_bucket=connection_settings.source_bucket)


async def run_scan_job(application: FastAPI, job_id: str) -> None:
    """Run one scan and persist status transitions without storing credentials."""

    settings = cast(AppSettings, application.state.settings)
    storage = cast(JobStorage, application.state.job_storage)
    connection_settings = cast(ConnectionSettings | None, application.state.connection_settings)
    existing_job = storage.get_job(job_id)
    if existing_job is None:
        return
    if connection_settings is None:
        existing_job["status"] = "failed"
        existing_job["error"] = "Connection settings are not configured"
        existing_job["updated_at"] = current_timestamp()
        storage.save_job(existing_job)
        return

    try:
        existing_job["status"] = "running"
        existing_job["updated_at"] = current_timestamp()
        storage.save_job(existing_job)
        r2_client = create_r2_client(connection_settings)
        source_objects = await asyncio.to_thread(lambda: list(r2_client.list_source_objects(connection_settings.source_bucket)))
        existing_job["progress"] = {"total": len(source_objects), "processed": 0, "failed": 0}
        existing_job["updated_at"] = current_timestamp()
        storage.save_job(existing_job)
        outcomes = {}
        for source_object in source_objects:
            outcome = await scan_object_with_retries(r2_client, connection_settings.source_bucket, source_object, settings.object_timeout_seconds, settings.object_retry_count)
            outcomes[source_object.key] = outcome
            existing_job["progress"]["processed"] += 1
            if outcome.classification in {"failed", "archive_corrupt", "timed_out"}:
                existing_job["progress"]["failed"] += 1
            existing_job["updated_at"] = current_timestamp()
            storage.save_job(existing_job)
        report: ScanReport = build_scan_report(job_id, connection_settings.source_bucket, current_timestamp(), source_objects, outcomes)
        storage.save_report(job_id, report)
        existing_job["status"] = "completed"
        existing_job["report_available"] = True
        existing_job["updated_at"] = current_timestamp()
        storage.save_job(existing_job)
    except (BotoCoreError, ClientError):
        existing_job["status"] = "failed"
        existing_job["error"] = "Unable to access the configured source bucket"
        existing_job["updated_at"] = current_timestamp()
        storage.save_job(existing_job)
    except asyncio.CancelledError:
        existing_job["status"] = "interrupted"
        existing_job["error"] = "The scan was interrupted by a service shutdown"
        existing_job["updated_at"] = current_timestamp()
        storage.save_job(existing_job)
        raise
    except Exception:
        existing_job["status"] = "failed"
        existing_job["error"] = "The scan failed before a report was generated"
        existing_job["updated_at"] = current_timestamp()
        storage.save_job(existing_job)


@asynccontextmanager
async def application_lifespan(application: FastAPI):
    """Prepare durable storage and mark unfinished jobs after a restart."""

    cast(JobStorage, application.state.job_storage).mark_active_jobs_interrupted()
    yield


def create_app(settings: AppSettings) -> FastAPI:
    """Create an application instance with isolated in-memory runtime state."""

    application = FastAPI(title="R2 Model Scanner", lifespan=application_lifespan)
    application.state.settings = settings
    application.state.job_storage = JobStorage(settings.data_directory)
    application.state.session_store = SessionStore(settings.session_max_age_seconds)
    application.state.connection_settings = None
    application.state.active_scan_task = None

    @application.post("/api/login", response_model=SessionResponse)
    async def login(login_request: LoginRequest, request: Request, response: Response) -> SessionResponse:
        ensure_same_origin(request)
        if not secrets.compare_digest(login_request.password, settings.admin_password):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid password")
        session_token = cast(SessionStore, application.state.session_store).create_session()
        response.set_cookie(key=SESSION_COOKIE_NAME, value=session_token, max_age=settings.session_max_age_seconds, httponly=True, secure=settings.cookie_secure, samesite="lax")
        return SessionResponse(authenticated=True)

    @application.post("/api/logout", response_model=SessionResponse)
    async def logout(request: Request, response: Response) -> SessionResponse:
        ensure_same_origin(request)
        session_store = cast(SessionStore, application.state.session_store)
        session_store.remove_session(request.cookies.get(SESSION_COOKIE_NAME))
        response.delete_cookie(key=SESSION_COOKIE_NAME, httponly=True, secure=settings.cookie_secure, samesite="lax")
        return SessionResponse(authenticated=False)

    @application.get("/api/session", response_model=SessionResponse)
    async def get_session(request: Request) -> SessionResponse:
        session_store = cast(SessionStore, application.state.session_store)
        return SessionResponse(authenticated=session_store.is_authenticated(request.cookies.get(SESSION_COOKIE_NAME)))

    @application.get("/api/connection", response_model=ConnectionView, dependencies=[Depends(require_authenticated_user)])
    async def get_connection() -> ConnectionView:
        return get_connection_view(cast(ConnectionSettings | None, application.state.connection_settings))

    @application.post("/api/connection", response_model=ConnectionView, dependencies=[Depends(require_authenticated_user)])
    async def set_connection(connection_request: ConnectionRequest, request: Request) -> ConnectionView:
        ensure_same_origin(request)
        application.state.connection_settings = ConnectionSettings(endpoint=connection_request.endpoint, access_key_id=connection_request.access_key_id, secret_access_key=connection_request.secret_access_key, source_bucket=connection_request.source_bucket)
        return get_connection_view(cast(ConnectionSettings, application.state.connection_settings))

    @application.post("/api/connection/test", response_model=ConnectionTestResponse, dependencies=[Depends(require_authenticated_user)])
    async def test_connection(request: Request) -> ConnectionTestResponse:
        ensure_same_origin(request)
        connection_settings = cast(ConnectionSettings | None, application.state.connection_settings)
        if connection_settings is None:
            return ConnectionTestResponse(success=False, reason="Configure a source connection first")
        try:
            await asyncio.to_thread(create_r2_client(connection_settings).test_source_bucket, connection_settings.source_bucket)
        except (BotoCoreError, ClientError):
            return ConnectionTestResponse(success=False, reason="Source bucket access failed; verify endpoint, credentials, and bucket name")
        except Exception:
            return ConnectionTestResponse(success=False, reason="Source connection test failed")
        return ConnectionTestResponse(success=True, reason="Source bucket is readable")

    @application.post("/api/scans", response_model=StartScanResponse, status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_authenticated_user)])
    async def start_scan(request: Request) -> StartScanResponse:
        ensure_same_origin(request)
        connection_settings = cast(ConnectionSettings | None, application.state.connection_settings)
        if connection_settings is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Configure a source connection first")
        active_scan_task = cast(asyncio.Task[None] | None, application.state.active_scan_task)
        if active_scan_task is not None and not active_scan_task.done():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A scan is already running")
        job_id = str(uuid4())
        initial_job: JobRecord = {"job_id": job_id, "status": "queued", "source_bucket": connection_settings.source_bucket, "progress": {"total": 0, "processed": 0, "failed": 0}, "report_available": False, "error": None, "updated_at": current_timestamp()}
        storage = cast(JobStorage, application.state.job_storage)
        storage.save_job(initial_job)
        application.state.active_scan_task = asyncio.create_task(run_scan_job(application, job_id))
        return StartScanResponse(job_id=job_id, status="queued")

    def normalize_job_id(job_id: str) -> str:
        try:
            return str(UUID(job_id))
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan job not found") from error

    @application.get("/api/scans/latest", response_model=None, dependencies=[Depends(require_authenticated_user)])
    async def get_latest_scan_status() -> JobRecord | None:
        return cast(JobStorage, application.state.job_storage).get_latest_job()

    @application.get("/api/scans/{job_id}", dependencies=[Depends(require_authenticated_user)])
    async def get_scan_status(job_id: str) -> JobRecord:
        stored_job = cast(JobStorage, application.state.job_storage).get_job(normalize_job_id(job_id))
        if stored_job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan job not found")
        return stored_job

    @application.get("/api/scans/{job_id}/report", dependencies=[Depends(require_authenticated_user)])
    async def get_scan_report(job_id: str) -> ScanReport:
        stored_report = cast(JobStorage, application.state.job_storage).get_report(normalize_job_id(job_id))
        if stored_report is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan report not found")
        return stored_report

    frontend_directory = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    assets_directory = frontend_directory / "assets"
    if assets_directory.is_dir():
        application.mount("/assets", StaticFiles(directory=assets_directory), name="assets")

    @application.get("/{path:path}", include_in_schema=False)
    async def serve_frontend(path: str) -> Response:
        if path == "api" or path.startswith("api/"):
            return JSONResponse({"detail": "API route not found"}, status_code=404)
        requested_file = frontend_directory / path
        if path and requested_file.is_file() and frontend_directory in requested_file.parents:
            return FileResponse(requested_file)
        index_file = frontend_directory / "index.html"
        if index_file.is_file():
            return FileResponse(index_file)
        return HTMLResponse("Frontend build is not present. Run the frontend build first.", status_code=503)

    return application
