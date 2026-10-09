"""Internal and API models for the scanner application."""

from dataclasses import dataclass
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


@dataclass(frozen=True)
class ConnectionSettings:
    """Resolved bucket-specific R2 client settings; secrets stay process-local."""

    endpoint: str
    access_key_id: str = ""
    secret_access_key: str = ""
    source_bucket: str = ""
    credential_ref: str = "default"
    source_buckets: tuple[str, ...] = ()
    transfer_adapter: str = "boto3"
    rclone_binary_path: str = "/usr/local/bin/rclone"
    rclone_transfer_timeout_seconds: int = 3600
    rclone_output_limit_bytes: int = 64 * 1024
    rclone_low_level_retries: int = 3


@dataclass(frozen=True)
class SourceConnectionProfile:
    """Persistable per-source-bucket configuration with no secret material."""

    source_bucket: str
    endpoint: str
    credential_ref: str = "default"


@dataclass(frozen=True)
class SourceObject:
    """Credential-free source object metadata returned by listing or HEAD."""

    key: str
    size: int
    etag: str | None = None
    last_modified: str | None = None

    @property
    def fingerprint(self) -> str:
        """Return the stable metadata version key used for idempotency.

        ``LastModified`` is useful for reconciliation diagnostics, but event
        delivery timestamps are not object version identifiers.  Including it
        here would turn duplicate notifications for the same object into new
        work items.  ETag + size are the provider's version clues; ETag is not
        interpreted as an MD5 digest.
        """

        return f"{self.etag or ''}:{self.size}"


class ObjectReference(TypedDict):
    key: str
    size: int
    etag: str | None
    last_modified: str | None


class FailedObject(TypedDict):
    key: str
    size: int
    classification: str
    message: str


class ModelReport(TypedDict):
    object_count: int
    total_bytes: int
    objects: list[ObjectReference]


class ScanReport(TypedDict):
    job_id: str
    generated_at: str
    source_buckets: list["SourceBucketReport"]
    object_count: int
    total_bytes: int


class SourceBucketReport(TypedDict):
    source_bucket: str
    error: str | None
    object_count: int
    total_bytes: int
    models: dict[str, ModelReport]
    unmatched_objects: list[ObjectReference]
    non_archive_objects: list[ObjectReference]
    failed_objects: list[FailedObject]
    timed_out_objects: list[FailedObject]


class SyncResult(TypedDict):
    source_bucket: str
    model_names: list[str]
    target_bucket: str
    object_key: str
    size: int
    status: str
    message: str


class SyncReport(TypedDict):
    sync_job_id: str
    source_scan_job_id: str
    generated_at: str
    total: int
    copied: int
    skipped: int
    failed: int
    results: list[SyncResult]


class LoginRequest(BaseModel):
    password: str = Field(min_length=1)


class ConnectionRequest(BaseModel):
    """Non-secret profiles, with the old aggregate shape accepted for migration."""

    model_config = ConfigDict(extra="forbid")

    connections: list["ConnectionProfileRequest"] | None = None
    endpoint: str | None = None
    source_buckets: list[str] | None = None

    @model_validator(mode="after")
    def validate_connection_shape(self) -> "ConnectionRequest":
        if self.connections is None and (not self.endpoint or not self.source_buckets):
            raise ValueError("Provide per-bucket connections or legacy endpoint and source_buckets")
        if self.connections is not None and (self.endpoint is not None or self.source_buckets is not None):
            raise ValueError("Do not mix per-bucket connections with legacy fields")
        return self


class ConnectionProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_bucket: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    credential_ref: str = Field(min_length=1, pattern="^[A-Za-z0-9_-]+$")


class ConnectionProfileView(ConnectionProfileRequest):
    pass


class ConnectionView(BaseModel):
    configured: bool
    endpoint: str | None = None
    source_buckets: list[str] = Field(default_factory=list)
    connections: list[ConnectionProfileView] = Field(default_factory=list)
    runtime_ready: bool = False
    readiness_message: str = ""


class SavedConnectionView(BaseModel):
    """Compact compatibility response for the save form; never contains secrets."""

    configured: bool
    endpoint: str | None = None
    source_buckets: list[str] = Field(default_factory=list)


class BucketModelMapping(BaseModel):
    source_bucket: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    target_bucket: str = Field(min_length=1)


class MappingsRequest(BaseModel):
    mappings: list[BucketModelMapping]


class MappingsResponse(BaseModel):
    mappings: list[BucketModelMapping]


class ScanReferenceRequest(BaseModel):
    scan_job_id: str = Field(min_length=1)


class ConnectionTestResponse(BaseModel):
    success: bool
    reason: str


class StartScanResponse(BaseModel):
    job_id: str
    status: str


class TargetBucketCheck(BaseModel):
    target_bucket: str
    accessible: bool
    reason: str


class SyncPreflightResponse(BaseModel):
    success: bool
    reason: str
    targets: list[TargetBucketCheck] = Field(default_factory=list)


class StartSyncResponse(BaseModel):
    sync_job_id: str
    status: str


class SessionResponse(BaseModel):
    authenticated: bool


class ContinuousModeRequest(BaseModel):
    enabled: bool


class BackfillActionRequest(BaseModel):
    action: str = Field(pattern="^(start|pause|resume)$")


class RetryRequest(BaseModel):
    object_id: int | None = Field(default=None, ge=1)


class IncrementalStatusResponse(BaseModel):
    continuous_enabled: bool
    paused: bool
    runtime_ready: bool
    readiness_message: str
    backfill: dict[str, object]
    counts: dict[str, int]
    queue_last_pull_at: str | None = None
    queue_last_ack_at: str | None = None
    queue_backlog_count: int | None = Field(default=None, ge=0)
    reconcile_last_run_at: str | None = None
    last_error: str | None = None
