from backend.models import BucketModelMapping
from backend.sync import SyncAction, create_sync_actions, execute_sync_action


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
    def __init__(self, object_already_exists: bool) -> None:
        self.object_already_exists = object_already_exists
        self.copy_calls: list[tuple[str, str, str, str]] = []

    def object_exists(self, bucket_name: str, object_key: str) -> bool:
        return self.object_already_exists

    def copy_object(
        self,
        source_bucket: str,
        source_key: str,
        target_bucket: str,
        target_key: str,
    ) -> None:
        self.copy_calls.append((source_bucket, source_key, target_bucket, target_key))


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
