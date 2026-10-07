"""Deterministic, failure-isolated model discovery for source objects."""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
import gzip
import re
import tarfile
import zlib

from botocore.exceptions import BotoCoreError

from .models import (
    FailedObject,
    ModelReport,
    ObjectReference,
    ScanReport,
    SourceBucketReport,
    SourceObject,
)
from .r2_client import R2Client

MODEL_MEMBER_PATTERN = re.compile(r"^roots/primary/([^/]+)/")


class ArchiveCorruptionError(Exception):
    """Raised when a tar.gz stream cannot be read as a valid archive."""


@dataclass(frozen=True)
class ObjectScanOutcome:
    """Credential-free result of scanning one listed source object."""

    classification: str
    model_names: tuple[str, ...] = ()


def extract_model_names(member_paths: Iterable[str]) -> list[str]:
    """Extract unique model names from matching archive member paths."""

    model_names = {
        match.group(1)
        for member_path in member_paths
        if (match := MODEL_MEMBER_PATTERN.match(member_path)) is not None
    }
    return sorted(model_names)


def discover_models_from_archive(object_body: object) -> list[str]:
    """Read member names from a tar.gz stream without extracting files."""

    try:
        with tarfile.open(fileobj=object_body, mode="r|gz") as archive:
            return extract_model_names(member.name for member in archive)
    except (tarfile.ReadError, EOFError, gzip.BadGzipFile, zlib.error) as error:
        raise ArchiveCorruptionError from error


def is_retryable_read_error(error: Exception) -> bool:
    """Identify transport/read failures that are safe to retry."""

    return isinstance(error, (BotoCoreError, ConnectionError, TimeoutError, OSError))


def scan_object_once(
    client: R2Client,
    source_bucket: str,
    source_object: SourceObject,
) -> ObjectScanOutcome:
    """Scan one object once, preserving the source archive as an opaque object."""

    if not source_object.key.lower().endswith(".tar.gz"):
        return ObjectScanOutcome(classification="non_archive")

    object_body = client.open_object(source_bucket, source_object.key)
    try:
        model_names = discover_models_from_archive(object_body)
    finally:
        object_body.close()

    if not model_names:
        return ObjectScanOutcome(classification="unmatched")
    return ObjectScanOutcome(classification="matched", model_names=tuple(model_names))


async def scan_object_with_retries(
    client: R2Client,
    source_bucket: str,
    source_object: SourceObject,
    timeout_seconds: int,
    retry_count: int,
) -> ObjectScanOutcome:
    """Scan one object with a timeout and bounded transport retries."""

    for attempt_number in range(retry_count + 1):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(scan_object_once, client, source_bucket, source_object),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            return ObjectScanOutcome(classification="timed_out")
        except ArchiveCorruptionError:
            return ObjectScanOutcome(classification="archive_corrupt")
        except Exception as error:
            if not is_retryable_read_error(error) or attempt_number >= retry_count:
                return ObjectScanOutcome(classification="failed")
    return ObjectScanOutcome(classification="failed")


def create_object_reference(source_object: SourceObject) -> ObjectReference:
    """Convert internal object metadata to the report's safe shape."""

    return {"key": source_object.key, "size": source_object.size}


def create_failed_object(source_object: SourceObject, classification: str) -> FailedObject:
    """Create an error record without persisting exception details or credentials."""

    messages = {
        "archive_corrupt": "Archive could not be read",
        "failed": "Object read failed after retries",
        "timed_out": "Object scan exceeded the configured timeout",
    }
    return {
        "key": source_object.key,
        "size": source_object.size,
        "classification": classification,
        "message": messages.get(classification, "Object scan failed"),
    }


def build_source_bucket_report(
    source_bucket: str,
    source_objects: list[SourceObject],
    outcomes: dict[str, ObjectScanOutcome],
    error: str | None = None,
) -> SourceBucketReport:
    """Aggregate one source bucket into a credential-free report section."""

    model_objects: dict[str, dict[str, ObjectReference]] = {}
    unmatched_objects: list[ObjectReference] = []
    non_archive_objects: list[ObjectReference] = []
    failed_objects: list[FailedObject] = []
    timed_out_objects: list[FailedObject] = []

    for source_object in source_objects:
        outcome = outcomes.get(source_object.key, ObjectScanOutcome("failed"))
        object_reference = create_object_reference(source_object)
        if outcome.classification == "matched":
            for model_name in outcome.model_names:
                model_objects.setdefault(model_name, {})[source_object.key] = object_reference
        elif outcome.classification == "unmatched":
            unmatched_objects.append(object_reference)
        elif outcome.classification == "non_archive":
            non_archive_objects.append(object_reference)
        elif outcome.classification == "timed_out":
            timed_out_objects.append(create_failed_object(source_object, "timed_out"))
        else:
            failed_objects.append(create_failed_object(source_object, outcome.classification))

    models: dict[str, ModelReport] = {}
    for model_name in sorted(model_objects):
        model_object_list = [model_objects[model_name][key] for key in sorted(model_objects[model_name])]
        models[model_name] = {
            "object_count": len(model_object_list),
            "total_bytes": sum(item["size"] for item in model_object_list),
            "objects": model_object_list,
        }

    return {
        "source_bucket": source_bucket,
        "error": error,
        "object_count": len(source_objects),
        "total_bytes": sum(source_object.size for source_object in source_objects),
        "models": models,
        "unmatched_objects": unmatched_objects,
        "non_archive_objects": non_archive_objects,
        "failed_objects": failed_objects,
        "timed_out_objects": timed_out_objects,
    }


def build_scan_report(
    job_id: str,
    generated_at: str,
    source_bucket_reports: list[SourceBucketReport],
) -> ScanReport:
    """Aggregate all source bucket reports into one credential-free report."""

    return {
        "job_id": job_id,
        "generated_at": generated_at,
        "source_buckets": source_bucket_reports,
        "object_count": sum(report["object_count"] for report in source_bucket_reports),
        "total_bytes": sum(report["total_bytes"] for report in source_bucket_reports),
    }
