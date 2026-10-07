from pathlib import Path
import time

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import AppSettings
from backend.models import ScanReport, SourceObject


def create_scan_report() -> ScanReport:
    return {
        "job_id": "00000000-0000-0000-0000-000000000001",
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
                        "objects": [{"key": "path/file.tar.gz", "size": 10}],
                    }
                },
                "unmatched_objects": [],
                "non_archive_objects": [],
                "failed_objects": [],
                "timed_out_objects": [],
            }
        ],
        "object_count": 1,
        "total_bytes": 10,
    }


class FakeWorkflowClient:
    def __init__(self) -> None:
        self.checked_buckets: list[str] = []
        self.copied_objects: list[tuple[str, str, str, str]] = []

    def test_source_bucket(self, bucket_name: str) -> None:
        self.checked_buckets.append(bucket_name)

    def object_exists(self, bucket_name: str, object_key: str) -> bool:
        return False

    def copy_object(
        self,
        source_bucket: str,
        source_key: str,
        target_bucket: str,
        target_key: str,
    ) -> None:
        self.copied_objects.append((source_bucket, source_key, target_bucket, target_key))


def test_mapping_preflight_and_sync_use_saved_source_model_mapping(
    tmp_path: Path,
    monkeypatch,
) -> None:
    application = create_app(
        AppSettings(admin_password="test-password", data_directory=tmp_path, cookie_secure=False)
    )
    fake_client = FakeWorkflowClient()
    monkeypatch.setattr("backend.app.create_r2_client", lambda settings: fake_client)
    application.state.job_storage.save_report(
        "00000000-0000-0000-0000-000000000001",
        create_scan_report(),
    )

    with TestClient(application) as client:
        assert client.post("/api/login", json={"password": "test-password"}).status_code == 200
        connection_response = client.post(
            "/api/connection",
            json={
                "connections": [
                    {
                        "source_bucket": "招 2",
                        "endpoint": "https://example.invalid",
                        "credential_ref": "default",
                    }
                ]
            },
        )
        assert connection_response.status_code == 200

        incomplete_mapping_response = client.post(
            "/api/sync/preflight",
            json={"scan_job_id": "00000000-0000-0000-0000-000000000001"},
        )
        assert incomplete_mapping_response.json() == {
            "success": False,
            "reason": "Some discovered models do not have target buckets",
            "targets": [],
        }

        application.state.job_storage.incremental.upsert_object(
            "招 2", SourceObject("path/file.tar.gz", 10, "etag")
        )
        claimed = application.state.job_storage.incremental.claim_next_object()
        assert claimed is not None
        assert application.state.job_storage.incremental.complete_classification(
            int(claimed["id"]),
            "awaiting_mapping",
            ("sol",),
            state="unmapped",
        )
        mapping_response = client.post(
            "/api/mappings",
            json={
                "mappings": [
                    {
                        "source_bucket": "招 2",
                        "model_name": "sol",
                        "target_bucket": "招 2 sol",
                    }
                ]
            },
        )
        assert mapping_response.status_code == 200
        assert application.state.job_storage.incremental.counts().get("route_pending", 0) == 0

        preflight_response = client.post(
            "/api/sync/preflight",
            json={"scan_job_id": "00000000-0000-0000-0000-000000000001"},
        )
        assert preflight_response.json() == {
            "success": True,
            "reason": "All target buckets are accessible",
            "targets": [
                {
                    "target_bucket": "招 2 sol",
                    "accessible": True,
                    "reason": "Target bucket is accessible",
                }
            ],
        }

        start_sync_response = client.post(
            "/api/sync",
            json={"scan_job_id": "00000000-0000-0000-0000-000000000001"},
        )
        assert start_sync_response.status_code == 202
        sync_job_id = start_sync_response.json()["sync_job_id"]

        sync_status = None
        for _ in range(20):
            sync_status = client.get(f"/api/sync/{sync_job_id}").json()
            if sync_status["status"] == "completed":
                break
            time.sleep(0.01)

        assert sync_status is not None
        assert sync_status["status"] == "completed"
        assert fake_client.checked_buckets == ["招 2 sol", "招 2 sol"]
        assert fake_client.copied_objects == [
            ("招 2", "path/file.tar.gz", "招 2 sol", "path/file.tar.gz")
        ]
