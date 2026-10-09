"""Build and execute explicit source-object copy actions."""

import asyncio
from dataclasses import dataclass
import threading

from .models import BucketModelMapping, ScanReport, SyncResult
from .r2_client import R2Client
from .transfer import SourceChangedBeforeTransferError, TransferOutcome, TransferRequest


@dataclass(frozen=True)
class SyncAction:
    """One unique source-object to target-bucket copy action."""

    source_bucket: str
    model_names: tuple[str, ...]
    target_bucket: str
    object_key: str
    size: int
    expected_etag: str | None = None


def create_sync_actions(
    report: ScanReport,
    mappings: list[BucketModelMapping],
) -> list[SyncAction]:
    """Expand a scan report into deduplicated copy actions."""

    target_by_pair = {
        (mapping.source_bucket, mapping.model_name): mapping.target_bucket
        for mapping in mappings
    }
    grouped_actions: dict[tuple[str, str, str], dict[str, object]] = {}
    for source_bucket_report in report["source_buckets"]:
        source_bucket = source_bucket_report["source_bucket"]
        for model_name, model_report in source_bucket_report["models"].items():
            target_bucket = target_by_pair.get((source_bucket, model_name))
            if target_bucket is None:
                continue
            for source_object in model_report["objects"]:
                action_key = (source_bucket, source_object["key"], target_bucket)
                action = grouped_actions.setdefault(
                    action_key,
                    {
                        "source_bucket": source_bucket,
                        "model_names": set(),
                        "target_bucket": target_bucket,
                        "object_key": source_object["key"],
                        "size": source_object["size"],
                        "expected_etag": source_object.get("etag"),
                    },
                )
                model_names = action["model_names"]
                if isinstance(model_names, set):
                    model_names.add(model_name)

    actions: list[SyncAction] = []
    for action in grouped_actions.values():
        model_names = action["model_names"]
        if not isinstance(model_names, set):
            continue
        actions.append(
            SyncAction(
                source_bucket=str(action["source_bucket"]),
                model_names=tuple(sorted(str(model_name) for model_name in model_names)),
                target_bucket=str(action["target_bucket"]),
                object_key=str(action["object_key"]),
                size=int(action["size"]),
                expected_etag=(
                    str(action["expected_etag"])
                    if action.get("expected_etag") is not None
                    else None
                ),
            )
        )
    return sorted(
        actions,
        key=lambda action: (action.source_bucket, action.target_bucket, action.object_key),
    )


def execute_sync_action(
    client: R2Client,
    action: SyncAction,
    cancellation_event: threading.Event | None = None,
) -> SyncResult:
    """Copy one current action without overwriting an existing destination key."""

    source = client.head_object(action.source_bucket, action.object_key)
    source_changed = source.size != action.size or (
        action.expected_etag is not None and source.etag != action.expected_etag
    )
    if source_changed:
        raise SourceChangedBeforeTransferError("Source object changed before transfer")

    if client.object_exists(action.target_bucket, action.object_key):
        return {
            "source_bucket": action.source_bucket,
            "model_names": list(action.model_names),
            "target_bucket": action.target_bucket,
            "object_key": action.object_key,
            "size": action.size,
            "status": "skipped",
            "message": "Target object already exists",
        }

    request = TransferRequest(
        source_bucket=action.source_bucket,
        source_key=action.object_key,
        target_bucket=action.target_bucket,
        target_key=action.object_key,
        expected_size=action.size,
    )
    transfer_method = getattr(client, "transfer_object", None)
    if callable(transfer_method):
        outcome = transfer_method(request, cancellation_event)
    else:
        client.copy_object(
            action.source_bucket,
            action.object_key,
            action.target_bucket,
            action.object_key,
        )
        outcome = TransferOutcome.COPIED
    if outcome == TransferOutcome.SKIPPED:
        return {
            "source_bucket": action.source_bucket,
            "model_names": list(action.model_names),
            "target_bucket": action.target_bucket,
            "object_key": action.object_key,
            "size": action.size,
            "status": "skipped",
            "message": "Target object appeared before transfer completed",
        }
    return {
        "source_bucket": action.source_bucket,
        "model_names": list(action.model_names),
        "target_bucket": action.target_bucket,
        "object_key": action.object_key,
        "size": action.size,
        "status": "copied",
        "message": "Object copied successfully",
    }


async def execute_sync_action_async(client: R2Client, action: SyncAction) -> SyncResult:
    """Run a blocking adapter and signal cancellation to its child process."""

    cancellation_event = threading.Event()
    worker = asyncio.create_task(
        asyncio.to_thread(execute_sync_action, client, action, cancellation_event)
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancellation_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=5)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        raise


def create_sync_failure_result(action: SyncAction) -> SyncResult:
    """Create a safe failure result without exposing provider error details."""

    return {
        "source_bucket": action.source_bucket,
        "model_names": list(action.model_names),
        "target_bucket": action.target_bucket,
        "object_key": action.object_key,
        "size": action.size,
        "status": "failed",
        "message": "Object copy failed",
    }
