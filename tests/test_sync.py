import asyncio
import threading

import pytest

from backend.models import BucketModelMapping, SourceObject
from backend.sync import (
    SyncAction,
    create_sync_actions,
    execute_sync_action,
    execute_sync_action_async,
)
from backend.transfer import (
    TransferCancelledError,
    TransferOutcome,
    TransferVerificationError,
)


def create_scan_report() -> dict:
    return {
        "job_id": "scan-job",
        "generated_at": "timestamp",
        "source_buckets": [
            {
                "source_bucket": "招 2",
                "error": None,
                "object_count": 1,
                "total_bytes": 10,
                "models": {
                    "sol": {
                        "object_count": 1,
                        "total_bytes": 10,
                        "objects": [{"key": "shared.tar.gz", "size": 10}],
                    },
                    "Astra": {
                        "object_count": 1,
                        "total_bytes": 10,
                        "objects": [{"key": "shared.tar.gz", "size": 10}],
                    },
                },
                "unmatched_objects": [],
                "non_archive_objects": [],
                "failed_objects": [],
                "timed_out_objects": [],
            },
            {
                "source_bucket": "招 3",
                "error": None,
                "object_count": 1,
                "total_bytes": 10,
                "models": {
                    "sol": {
                        "object_count": 1,
                        "total_bytes": 10,
                        "objects": [{"key": "shared.tar.gz", "size": 10}],
                    }
                },
                "unmatched_objects": [],
                "non_archive_objects": [],
                "failed_objects": [],
                "timed_out_objects": [],
            },
        ],
        "object_count": 2,
        "total_bytes": 20,
    }


def test_sync_actions_scope_same_named_models_by_source_bucket() -> None:
    mappings = [
        BucketModelMapping(source_bucket="招 2", model_name="sol", target_bucket="招 2 sol"),
        BucketModelMapping(source_bucket="招 2", model_name="Astra", target_bucket="招 2 Astra"),
        BucketModelMapping(source_bucket="招 3", model_name="sol", target_bucket="招 3 sol"),
    ]

    actions = create_sync_actions(create_scan_report(), mappings)

    assert actions == [
        SyncAction(
            source_bucket="招 2",
            model_names=("Astra",),
            target_bucket="招 2 Astra",
            object_key="shared.tar.gz",
            size=10,
        ),
        SyncAction(
            source_bucket="招 2",
            model_names=("sol",),
            target_bucket="招 2 sol",
            object_key="shared.tar.gz",
            size=10,
        ),
        SyncAction(
            source_bucket="招 3",
            model_names=("sol",),
            target_bucket="招 3 sol",
            object_key="shared.tar.gz",
            size=10,
        ),
    ]


def test_sync_actions_deduplicate_same_object_to_same_target() -> None:
    mappings = [
        BucketModelMapping(source_bucket="招 2", model_name="sol", target_bucket="shared-target"),
        BucketModelMapping(source_bucket="招 2", model_name="Astra", target_bucket="shared-target"),
    ]

    actions = create_sync_actions(create_scan_report(), mappings)

    assert actions == [
        SyncAction(
            source_bucket="招 2",
            model_names=("Astra", "sol"),
            target_bucket="shared-target",
            object_key="shared.tar.gz",
            size=10,
        )
    ]


class FakeSyncClient:
    def __init__(
        self,
        object_already_exists: bool,
        *,
        current: SourceObject | None = None,
        outcome: TransferOutcome = TransferOutcome.COPIED,
    ) -> None:
        self.object_already_exists = object_already_exists
        self.current = current or SourceObject("path/file.tar.gz", 42, "etag")
        self.outcome = outcome
        self.copy_calls: list[tuple[str, str, str, str]] = []

    def object_exists(self, bucket_name: str, object_key: str) -> bool:
        return self.object_already_exists

    def head_object(self, bucket_name: str, object_key: str) -> SourceObject:
        return self.current

    def transfer_object(
        self,
        request,
        cancellation_event=None,
    ) -> TransferOutcome:
        self.copy_calls.append(
            (
                request.source_bucket,
                request.source_key,
                request.target_bucket,
                request.target_key,
            )
        )
        return self.outcome


def test_existing_target_object_is_skipped_without_copy() -> None:
    client = FakeSyncClient(object_already_exists=True)
    action = SyncAction("招 2", ("sol",), "招 2 sol", "path/file.tar.gz", 42)

    result = execute_sync_action(client, action)

    assert result["status"] == "skipped"
    assert client.copy_calls == []


def test_new_target_object_is_copied_with_original_key() -> None:
    client = FakeSyncClient(object_already_exists=False)
    action = SyncAction("招 2", ("sol",), "招 2 sol", "path/file.tar.gz", 42)

    result = execute_sync_action(client, action)

    assert result["status"] == "copied"
    assert client.copy_calls == [
        ("招 2", "path/file.tar.gz", "招 2 sol", "path/file.tar.gz")
    ]


def test_exit_zero_skip_is_reported_as_skipped_not_copied() -> None:
    client = FakeSyncClient(
        object_already_exists=False,
        outcome=TransferOutcome.SKIPPED,
    )
    action = SyncAction("招 2", ("sol",), "招 2 sol", "path/file.tar.gz", 42)

    result = execute_sync_action(client, action)

    assert result["status"] == "skipped"
    assert client.copy_calls == [
        ("招 2", "path/file.tar.gz", "招 2 sol", "path/file.tar.gz")
    ]


def test_source_version_change_fails_before_target_preflight_or_copy() -> None:
    client = FakeSyncClient(
        object_already_exists=False,
        current=SourceObject("path/file.tar.gz", 42, "new-etag"),
    )
    action = SyncAction(
        "招 2",
        ("sol",),
        "招 2 sol",
        "path/file.tar.gz",
        42,
        expected_etag="old-etag",
    )

    with pytest.raises(TransferVerificationError):
        execute_sync_action(client, action)

    assert client.copy_calls == []


@pytest.mark.asyncio
async def test_async_sync_cancellation_signals_blocking_transfer() -> None:
    started = threading.Event()
    cancellation_seen = threading.Event()

    class BlockingClient(FakeSyncClient):
        def transfer_object(self, request, cancellation_event=None) -> TransferOutcome:
            started.set()
            assert cancellation_event is not None
            cancellation_event.wait(timeout=2)
            if cancellation_event.is_set():
                cancellation_seen.set()
                raise TransferCancelledError("Object transfer was cancelled")
            return TransferOutcome.COPIED

    action = SyncAction(
        "source",
        ("model",),
        "target",
        "path/file.tar.gz",
        42,
        expected_etag="etag",
    )
    task = asyncio.create_task(
        execute_sync_action_async(BlockingClient(object_already_exists=False), action)
    )
    assert await asyncio.to_thread(started.wait, 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancellation_seen.is_set()
