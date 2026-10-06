"""Internal and API models for the scanner application."""

from dataclasses import dataclass
from typing import TypedDict

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class ConnectionSettings:
    """R2 settings kept only in the running process."""

    endpoint: str
    access_key_id: str
    secret_access_key: str
    source_bucket: str


@dataclass(frozen=True)
class SourceObject:
    """Credential-free source object metadata returned by object listing."""

    key: str
    size: int


class ObjectReference(TypedDict):
    key: str
    size: int


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
    source_bucket: str
    generated_at: str
    object_count: int
    total_bytes: int
    models: dict[str, ModelReport]
    unmatched_objects: list[ObjectReference]
    non_archive_objects: list[ObjectReference]
    failed_objects: list[FailedObject]
    timed_out_objects: list[FailedObject]


class LoginRequest(BaseModel):
    password: str = Field(min_length=1)


class ConnectionRequest(BaseModel):
    endpoint: str = Field(min_length=1)
    access_key_id: str = Field(min_length=1)
    secret_access_key: str = Field(min_length=1)
    source_bucket: str = Field(min_length=1)


class ConnectionView(BaseModel):
    configured: bool
    endpoint: str | None = None
    source_bucket: str | None = None


class ConnectionTestResponse(BaseModel):
    success: bool
    reason: str


class StartScanResponse(BaseModel):
    job_id: str
    status: str


class SessionResponse(BaseModel):
    authenticated: bool
