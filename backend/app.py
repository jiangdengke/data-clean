"""FastAPI application for authenticated R2 scanning and explicit sync."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import secrets
from pathlib import Path
from typing import cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import AppSettings
from .incremental import IncrementalService
from .models import (
    BucketModelMapping,
    ConnectionRequest,
    ConnectionProfileRequest,
    ConnectionProfileView,
    ConnectionSettings,
    ConnectionTestResponse,
    ConnectionView,
    LoginRequest,
    MappingsRequest,
    MappingsResponse,
    ScanReferenceRequest,
    ScanReport,
    SessionResponse,
    SourceBucketReport,
    SourceConnectionProfile,
    SourceObject,
    StartScanResponse,
    StartSyncResponse,
    SyncResult,
    SyncPreflightResponse,
    TargetBucketCheck,
    ContinuousModeRequest,
    BackfillActionRequest,
    RetryRequest,
    IncrementalStatusResponse,
)
from .r2_client import create_r2_client
from .scanner import (
    ObjectScanOutcome,
    build_scan_report,
    build_source_bucket_report,
    scan_object_with_retries,
)
from .storage import JobRecord, JobStorage, current_timestamp
from .sync import create_sync_actions, create_sync_failure_result, execute_sync_action

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


def resolve_connection_settings(settings: AppSettings, profile: SourceConnectionProfile) -> ConnectionSettings:
    access_key_id, secret_access_key = settings.resolve_r2_credentials(profile.credential_ref)
    return ConnectionSettings(
        endpoint=profile.endpoint,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        source_bucket=profile.source_bucket,
        credential_ref=profile.credential_ref,
    )


def get_connection_view(
    connection_profiles: tuple[SourceConnectionProfile, ...] | None, settings: AppSettings
) -> ConnectionView:
    """Return non-secret profiles and deployment-secret readiness."""

    profiles = connection_profiles or ()
    credential_refs = tuple(profile.credential_ref for profile in profiles)
    return ConnectionView(
        configured=bool(profiles),
        endpoint=profiles[0].endpoint if profiles else None,
        source_buckets=[profile.source_bucket for profile in profiles],
        connections=[
            ConnectionProfileView(
                source_bucket=profile.source_bucket,
                endpoint=profile.endpoint,
                credential_ref=profile.credential_ref,
            )
            for profile in profiles
        ],
        runtime_ready=bool(profiles)
        and all(
            profile.endpoint.strip() and settings.credentials_ready_for(profile.credential_ref)
            for profile in profiles
        )
        and settings.queue_credentials_ready,
        readiness_message=(
            settings.readiness_message_for(
                credential_refs,
                tuple(profile.endpoint for profile in profiles),
            )
            if profiles
            else "请先配置至少一个源桶连接"
        ),
    )


def get_connection_profiles(application: FastAPI) -> tuple[SourceConnectionProfile, ...]:
    return cast(JobStorage, application.state.job_storage).get_connection_profiles()


def get_bucket_client(application: FastAPI, source_bucket: str) -> object:
    settings = cast(AppSettings, application.state.settings)
    profile = next(
        (item for item in get_connection_profiles(application) if item.source_bucket == source_bucket),
        None,
    )
    if profile is None:
        raise RuntimeError("Source bucket is not configured")
    return create_r2_client(resolve_connection_settings(settings, profile))


def get_required_mapping_pairs(report: ScanReport) -> set[tuple[str, str]]:
    """Return every source-bucket/model pair discovered by a scan."""

    return {
        (bucket_report["source_bucket"], model_name)
        for bucket_report in report["source_buckets"]
        for model_name in bucket_report["models"]
    }


def find_missing_mappings(
    report: ScanReport,
    mappings: list[BucketModelMapping],
) -> list[tuple[str, str]]:
    """Find discovered model pairs that do not have a target bucket."""

    configured_pairs = {
        (mapping.source_bucket, mapping.model_name)
        for mapping in mappings
        if mapping.target_bucket.strip()
    }
    return sorted(get_required_mapping_pairs(report) - configured_pairs)


def find_duplicate_mapping_pairs(mappings: list[BucketModelMapping]) -> list[tuple[str, str]]:
    """Find duplicate source-bucket/model mapping keys."""

    seen_pairs: set[tuple[str, str]] = set()
    duplicate_pairs: set[tuple[str, str]] = set()
    for mapping in mappings:
        pair = (mapping.source_bucket, mapping.model_name)
        if pair in seen_pairs:
            duplicate_pairs.add(pair)
        seen_pairs.add(pair)
    return sorted(duplicate_pairs)


async def run_scan_job(application: FastAPI, job_id: str) -> None:
    """Scan all configured source buckets and persist a credential-free report."""

    settings = cast(AppSettings, application.state.settings)
    storage = cast(JobStorage, application.state.job_storage)
    connection_profiles = get_connection_profiles(application)
    existing_job = storage.get_job(job_id)
    if existing_job is None:
        return
    if not connection_profiles:
        existing_job["status"] = "failed"
        existing_job["error"] = "Connection settings are not configured"
        existing_job["updated_at"] = current_timestamp()
        storage.save_job(existing_job)
        return

    try:
        existing_job["status"] = "running"
        existing_job["updated_at"] = current_timestamp()
        storage.save_job(existing_job)
        bucket_objects: list[tuple[str, list[SourceObject]]] = []
        bucket_clients: dict[str, object] = {}
        bucket_errors: dict[str, str] = {}
        total_object_count = 0

        for source_bucket in (profile.source_bucket for profile in connection_profiles):
            try:
                r2_client = get_bucket_client(application, source_bucket)
                bucket_clients[source_bucket] = r2_client
                source_objects = await asyncio.to_thread(
                    lambda bucket=source_bucket, client=r2_client: list(client.list_source_objects(bucket))
                )
            except Exception:
                source_objects = []
                bucket_errors[source_bucket] = "Unable to access source bucket"
            bucket_objects.append((source_bucket, source_objects))
            total_object_count += len(source_objects)

        existing_job["progress"] = {
            "total": total_object_count,
            "processed": 0,
            "failed": 0,
        }
        existing_job["updated_at"] = current_timestamp()
        storage.save_job(existing_job)

        source_bucket_reports: list[SourceBucketReport] = []
        for source_bucket, source_objects in bucket_objects:
            existing_job["current_source_bucket"] = source_bucket
            existing_job["updated_at"] = current_timestamp()
            storage.save_job(existing_job)
            outcomes: dict[str, ObjectScanOutcome] = {}
            for source_object in source_objects:
                bucket_client = bucket_clients.get(source_bucket)
                if bucket_client is None:
                    outcome = ObjectScanOutcome(classification="failed")
                else:
                    outcome = await scan_object_with_retries(
                        bucket_client,
                        source_bucket,
                        source_object,
                        settings.object_timeout_seconds,
                        settings.object_retry_count,
                    )
                outcomes[source_object.key] = outcome
                existing_job["progress"]["processed"] += 1
                if outcome.classification in {"failed", "archive_corrupt", "timed_out"}:
                    existing_job["progress"]["failed"] += 1
                existing_job["updated_at"] = current_timestamp()
                storage.save_job(existing_job)

            source_bucket_reports.append(
                build_source_bucket_report(
                    source_bucket,
                    source_objects,
                    outcomes,
                    bucket_errors.get(source_bucket),
                )
            )

        report = build_scan_report(job_id, current_timestamp(), source_bucket_reports)
        storage.save_report(job_id, report)
        existing_job["status"] = "completed"
        existing_job["report_available"] = True
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


async def run_sync_job(application: FastAPI, sync_job_id: str, scan_job_id: str) -> None:
    """Copy every mapped source object and persist a safe sync report."""

    storage = cast(JobStorage, application.state.job_storage)
    sync_job = storage.get_job(sync_job_id)
    scan_report = storage.get_report(scan_job_id)
    if sync_job is None or scan_report is None or not get_connection_profiles(application):
        return

    try:
        mappings = storage.get_mappings()
        actions = create_sync_actions(scan_report, mappings)
        sync_job["status"] = "running"
        sync_job["progress"] = {"total": len(actions), "processed": 0, "failed": 0}
        sync_job["updated_at"] = current_timestamp()
        storage.save_job(sync_job)
        results: list[SyncResult] = []
        for action in actions:
            try:
                client = get_bucket_client(application, action.source_bucket)
                result = await asyncio.to_thread(execute_sync_action, client, action)
            except Exception:
                result = create_sync_failure_result(action)
            results.append(result)
            sync_job["progress"]["processed"] += 1
            if result["status"] == "failed":
                sync_job["progress"]["failed"] += 1
            sync_job["updated_at"] = current_timestamp()
            storage.save_job(sync_job)

        storage.save_sync_report(
            sync_job_id,
            {
                "sync_job_id": sync_job_id,
                "source_scan_job_id": scan_job_id,
                "generated_at": current_timestamp(),
                "total": len(results),
                "copied": sum(1 for result in results if result["status"] == "copied"),
                "skipped": sum(1 for result in results if result["status"] == "skipped"),
                "failed": sum(1 for result in results if result["status"] == "failed"),
                "results": results,
            },
        )
        sync_job["status"] = "completed"
        sync_job["report_available"] = True
        sync_job["updated_at"] = current_timestamp()
        storage.save_job(sync_job)
    except asyncio.CancelledError:
        sync_job["status"] = "interrupted"
        sync_job["error"] = "The sync was interrupted by a service shutdown"
        sync_job["updated_at"] = current_timestamp()
        storage.save_job(sync_job)
        raise
    except Exception:
        sync_job["status"] = "failed"
        sync_job["error"] = "The sync failed before a report was generated"
        sync_job["updated_at"] = current_timestamp()
        storage.save_job(sync_job)


@asynccontextmanager
async def application_lifespan(application: FastAPI):
    """Recover durable state and run only explicitly enabled incremental services."""

    storage = cast(JobStorage, application.state.job_storage)
    storage.mark_active_jobs_interrupted()
    incremental = cast(IncrementalService, application.state.incremental_service)
    await incremental.start()
    try:
        yield
    finally:
        await incremental.stop()


def create_app(settings: AppSettings) -> FastAPI:
    """Create an application instance with isolated in-memory runtime state."""

    application = FastAPI(title="R2 Model Scanner", lifespan=application_lifespan)

    @application.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Pydantic's default 422 payload includes the rejected ``input`` field.
        # That would echo a browser-supplied credential even though the request
        # contract rejects it. Keep locations/messages/types, never values.
        detail = [
            {
                key: error[key]
                for key in ("loc", "msg", "type")
                if key in error
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})
    application.state.settings = settings
    application.state.job_storage = JobStorage(settings.data_directory)
    application.state.session_store = SessionStore(settings.session_max_age_seconds)
    saved_profiles = cast(JobStorage, application.state.job_storage).get_connection_profiles()
    application.state.connection_profiles = saved_profiles
    application.state.connection_settings = None
    application.state.incremental_service = IncrementalService(
        settings,
        cast(JobStorage, application.state.job_storage),
        lambda source_bucket=None: get_bucket_client(application, source_bucket) if source_bucket else None,
    )
    application.state.active_scan_task = None
    application.state.active_sync_task = None
    application.state.sync_start_lock = asyncio.Lock()

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
        return get_connection_view(get_connection_profiles(application), settings)

    @application.post("/api/connection", response_model=None, dependencies=[Depends(require_authenticated_user)])
    async def set_connection(connection_request: ConnectionRequest, request: Request) -> ConnectionView:
        ensure_same_origin(request)
        if connection_request.connections is not None:
            raw_profiles = connection_request.connections
        else:
            raw_profiles = [
                ConnectionProfileRequest(
                    source_bucket=bucket,
                    endpoint=connection_request.endpoint or "",
                    credential_ref="default",
                )
                for bucket in (connection_request.source_buckets or [])
            ]
        profiles: list[SourceConnectionProfile] = []
        seen_buckets: set[str] = set()
        for profile in raw_profiles:
            source_bucket = profile.source_bucket.strip()
            endpoint = profile.endpoint.strip()
            credential_ref = profile.credential_ref.strip()
            if not source_bucket or not endpoint or not credential_ref or source_bucket in seen_buckets:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Each source bucket must have a unique endpoint and credential reference")
            seen_buckets.add(source_bucket)
            profiles.append(SourceConnectionProfile(source_bucket, endpoint, credential_ref))
        if not profiles:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="At least one source bucket is required")
        storage = cast(JobStorage, application.state.job_storage)
        storage.save_connection_profiles(tuple(profiles))
        application.state.connection_profiles = tuple(profiles)
        return get_connection_view(tuple(profiles), settings)

    @application.post("/api/connection/test", response_model=ConnectionTestResponse, dependencies=[Depends(require_authenticated_user)])
    async def test_connection(request: Request) -> ConnectionTestResponse:
        ensure_same_origin(request)
        profiles = get_connection_profiles(application)
        if not profiles:
            return ConnectionTestResponse(success=False, reason="Configure a source connection first")
        failed_buckets: list[str] = []
        for profile in profiles:
            try:
                client = get_bucket_client(application, profile.source_bucket)
                await asyncio.to_thread(client.test_source_bucket, profile.source_bucket)
            except Exception:
                failed_buckets.append(profile.source_bucket)
        if failed_buckets:
            return ConnectionTestResponse(
                success=False,
                reason=f"Source bucket access failed: {', '.join(failed_buckets)}",
            )
        return ConnectionTestResponse(success=True, reason="All source buckets are readable")

    @application.get("/api/mappings", response_model=MappingsResponse, dependencies=[Depends(require_authenticated_user)])
    async def get_mappings() -> MappingsResponse:
        return MappingsResponse(mappings=cast(JobStorage, application.state.job_storage).get_mappings())

    @application.post("/api/mappings", response_model=MappingsResponse, dependencies=[Depends(require_authenticated_user)])
    async def save_mappings(mapping_request: MappingsRequest, request: Request) -> MappingsResponse:
        ensure_same_origin(request)
        storage = cast(JobStorage, application.state.job_storage)
        normalized_mappings: list[BucketModelMapping] = []
        for mapping in mapping_request.mappings:
            source_bucket = mapping.source_bucket.strip()
            model_name = mapping.model_name.strip()
            target_bucket = mapping.target_bucket.strip()
            if not source_bucket or not model_name or not target_bucket:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Source bucket, model name, and target bucket are required",
                )
            normalized_mappings.append(
                BucketModelMapping(
                    source_bucket=source_bucket,
                    model_name=model_name,
                    target_bucket=target_bucket,
                )
            )
        duplicate_pairs = find_duplicate_mapping_pairs(normalized_mappings)
        if duplicate_pairs:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Duplicate source bucket and model mapping")
        storage.save_mappings(normalized_mappings)
        cast(IncrementalService, application.state.incremental_service).enqueue_mapped_routes_if_enabled(normalized_mappings)
        return MappingsResponse(mappings=storage.get_mappings())

    @application.get("/api/incremental/status", response_model=IncrementalStatusResponse, dependencies=[Depends(require_authenticated_user)])
    async def incremental_status() -> IncrementalStatusResponse:
        return IncrementalStatusResponse(**cast(IncrementalService, application.state.incremental_service).status())

    @application.post("/api/incremental/continuous", response_model=IncrementalStatusResponse, dependencies=[Depends(require_authenticated_user)])
    async def set_continuous(request_body: ContinuousModeRequest, request: Request) -> IncrementalStatusResponse:
        ensure_same_origin(request)
        service = cast(IncrementalService, application.state.incremental_service)
        if request_body.enabled and not get_connection_profiles(application):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Configure at least one source bucket")
        if request_body.enabled and not service.runtime_ready():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=service.readiness_message(),
            )
        service.set_continuous(request_body.enabled)
        await service.apply_continuous_state()
        return IncrementalStatusResponse(**service.status())

    @application.post("/api/incremental/backfill", response_model=IncrementalStatusResponse, dependencies=[Depends(require_authenticated_user)])
    async def backfill_action(request_body: BackfillActionRequest, request: Request) -> IncrementalStatusResponse:
        ensure_same_origin(request)
        service = cast(IncrementalService, application.state.incremental_service)
        if request_body.action == "start":
            await service.start_backfill()
        elif request_body.action == "pause":
            service.pause_backfill()
        else:
            await service.start_backfill()
        return IncrementalStatusResponse(**service.status())

    @application.post("/api/incremental/retry", response_model=IncrementalStatusResponse, dependencies=[Depends(require_authenticated_user)])
    async def retry_incremental(request_body: RetryRequest, request: Request) -> IncrementalStatusResponse:
        ensure_same_origin(request)
        cast(IncrementalService, application.state.incremental_service).store.retry_failed(request_body.object_id)
        return IncrementalStatusResponse(**cast(IncrementalService, application.state.incremental_service).status())

    @application.post("/api/scans", response_model=StartScanResponse, status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_authenticated_user)])
    async def start_scan(request: Request) -> StartScanResponse:
        ensure_same_origin(request)
        connection_profiles = get_connection_profiles(application)
        if not connection_profiles:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Configure a source connection first")
        active_scan_task = cast(asyncio.Task[None] | None, application.state.active_scan_task)
        if active_scan_task is not None and not active_scan_task.done():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A scan is already running")
        job_id = str(uuid4())
        initial_job: JobRecord = {
            "job_id": job_id,
            "job_type": "scan",
            "status": "queued",
            "source_buckets": [profile.source_bucket for profile in connection_profiles],
            "current_source_bucket": None,
            "progress": {"total": 0, "processed": 0, "failed": 0},
            "report_available": False,
            "error": None,
            "updated_at": current_timestamp(),
        }
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
        return cast(JobStorage, application.state.job_storage).get_latest_job("scan")

    @application.get("/api/scans/{job_id}", dependencies=[Depends(require_authenticated_user)])
    async def get_scan_status(job_id: str) -> JobRecord:
        stored_job = cast(JobStorage, application.state.job_storage).get_job(normalize_job_id(job_id))
        if stored_job is None or stored_job.get("job_type", "scan") != "scan":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan job not found")
        return stored_job

    @application.get("/api/scans/{job_id}/report", dependencies=[Depends(require_authenticated_user)])
    async def get_scan_report(job_id: str) -> ScanReport:
        stored_report = cast(JobStorage, application.state.job_storage).get_report(normalize_job_id(job_id))
        if stored_report is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan report not found")
        return stored_report

    @application.post("/api/sync/preflight", response_model=SyncPreflightResponse, dependencies=[Depends(require_authenticated_user)])
    async def sync_preflight(scan_reference: ScanReferenceRequest, request: Request) -> SyncPreflightResponse:
        ensure_same_origin(request)
        connection_profiles = get_connection_profiles(application)
        if not connection_profiles:
            return SyncPreflightResponse(success=False, reason="Connection settings are not configured")
        scan_report = cast(JobStorage, application.state.job_storage).get_report(normalize_job_id(scan_reference.scan_job_id))
        if scan_report is None:
            return SyncPreflightResponse(success=False, reason="Scan report not found")
        report_source_buckets = {
            bucket_report["source_bucket"]
            for bucket_report in scan_report["source_buckets"]
        }
        configured_source_buckets = {profile.source_bucket for profile in connection_profiles}
        if report_source_buckets != configured_source_buckets:
            return SyncPreflightResponse(
                success=False,
                reason="The scan report does not match the current source buckets",
            )
        if any(bucket_report["error"] for bucket_report in scan_report["source_buckets"]):
            return SyncPreflightResponse(success=False, reason="The scan did not access every source bucket")
        mappings = cast(JobStorage, application.state.job_storage).get_mappings()
        missing_mappings = find_missing_mappings(scan_report, mappings)
        if missing_mappings:
            return SyncPreflightResponse(success=False, reason="Some discovered models do not have target buckets")
        required_pairs = get_required_mapping_pairs(scan_report)
        target_sources: dict[str, set[str]] = {}
        for mapping in mappings:
            if (mapping.source_bucket, mapping.model_name) in required_pairs:
                target_sources.setdefault(mapping.target_bucket, set()).add(mapping.source_bucket)
        target_checks: list[TargetBucketCheck] = []
        for target_bucket in sorted(target_sources):
            failed_sources: list[str] = []
            for source_bucket in sorted(target_sources[target_bucket]):
                try:
                    client = get_bucket_client(application, source_bucket)
                    await asyncio.to_thread(client.test_source_bucket, target_bucket)
                except Exception:
                    failed_sources.append(source_bucket)
            if failed_sources:
                target_checks.append(
                    TargetBucketCheck(
                        target_bucket=target_bucket,
                        accessible=False,
                        reason="Target bucket is not accessible",
                    )
                )
            else:
                target_checks.append(
                    TargetBucketCheck(
                        target_bucket=target_bucket,
                        accessible=True,
                        reason="Target bucket is accessible",
                    )
                )
        success = all(check.accessible for check in target_checks)
        return SyncPreflightResponse(
            success=success,
            reason="All target buckets are accessible" if success else "Some target buckets are not accessible",
            targets=target_checks,
        )

    @application.post("/api/sync", response_model=StartSyncResponse, status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_authenticated_user)])
    async def start_sync(scan_reference: ScanReferenceRequest, request: Request) -> StartSyncResponse:
        ensure_same_origin(request)
        sync_start_lock = cast(asyncio.Lock, application.state.sync_start_lock)
        async with sync_start_lock:
            active_sync_task = cast(asyncio.Task[None] | None, application.state.active_sync_task)
            if active_sync_task is not None and not active_sync_task.done():
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A sync is already running")
            preflight_result = await sync_preflight(scan_reference, request)
            if not preflight_result.success:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=preflight_result.reason)
            sync_job_id = str(uuid4())
            sync_job: JobRecord = {
                "job_id": sync_job_id,
                "sync_job_id": sync_job_id,
                "source_scan_job_id": normalize_job_id(scan_reference.scan_job_id),
                "job_type": "sync",
                "status": "queued",
                "source_buckets": [],
                "progress": {"total": 0, "processed": 0, "failed": 0},
                "report_available": False,
                "error": None,
                "updated_at": current_timestamp(),
            }
            cast(JobStorage, application.state.job_storage).save_job(sync_job)
            application.state.active_sync_task = asyncio.create_task(
                run_sync_job(application, sync_job_id, sync_job["source_scan_job_id"])
            )
            return StartSyncResponse(sync_job_id=sync_job_id, status="queued")

    @application.get("/api/sync/latest", response_model=None, dependencies=[Depends(require_authenticated_user)])
    async def get_latest_sync_status() -> JobRecord | None:
        return cast(JobStorage, application.state.job_storage).get_latest_job("sync")

    @application.get("/api/sync/{sync_job_id}", response_model=None, dependencies=[Depends(require_authenticated_user)])
    async def get_sync_status(sync_job_id: str) -> JobRecord:
        stored_job = cast(JobStorage, application.state.job_storage).get_job(normalize_job_id(sync_job_id))
        if stored_job is None or stored_job.get("job_type") != "sync":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sync job not found")
        return stored_job

    @application.get("/api/sync/{sync_job_id}/report", response_model=None, dependencies=[Depends(require_authenticated_user)])
    async def get_sync_report(sync_job_id: str) -> object:
        stored_report = cast(JobStorage, application.state.job_storage).get_sync_report(normalize_job_id(sync_job_id))
        if stored_report is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sync report not found")
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
