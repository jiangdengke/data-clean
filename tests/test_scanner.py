import io
import tarfile

import pytest

from backend.models import SourceObject
from backend.r2_client import R2Client
from backend.scanner import (
    ArchiveCorruptionError,
    ObjectScanOutcome,
    build_scan_report,
    build_source_bucket_report,
    discover_models_from_archive,
    extract_model_names,
    scan_object_with_retries,
)


def create_archive_body(member_paths: list[str]) -> io.BytesIO:
    archive_body = io.BytesIO()
    with tarfile.open(fileobj=archive_body, mode="w:gz") as archive:
        for member_path in member_paths:
            archive_info = tarfile.TarInfo(member_path)
            archive_info.size = 0
            archive.addfile(archive_info)
    archive_body.seek(0)
    return archive_body


def test_extract_model_names_is_unique_and_sorted() -> None:
    assert extract_model_names(["roots/primary/zeta/file", "roots/primary/alpha/file", "roots/primary/zeta/other"]) == ["alpha", "zeta"]


def test_discover_models_from_archive_does_not_extract() -> None:
    archive_body = create_archive_body(["roots/primary/model-a/config.json"])
    assert discover_models_from_archive(archive_body) == ["model-a"]


def test_corrupt_archive_is_classified_without_retry_signal() -> None:
    try:
        discover_models_from_archive(io.BytesIO(b"not a gzip archive"))
    except ArchiveCorruptionError:
        pass
    else:
        raise AssertionError("Expected corrupt archive error")


@pytest.mark.asyncio
async def test_corrupt_archive_is_not_retried() -> None:
    class CorruptObjectClient:
        open_count = 0

        def test_source_bucket(self, bucket_name: str) -> None:
            return None

        def list_source_objects(self, bucket_name: str):
            return iter(())

        def open_object(self, bucket_name: str, object_key: str) -> io.BytesIO:
            self.open_count += 1
            return io.BytesIO(b"not a gzip archive")

    client: R2Client = CorruptObjectClient()
    outcome = await scan_object_with_retries(client, "source", SourceObject("broken.tar.gz", 19), 120, 2)

    assert outcome.classification == "archive_corrupt"
    assert client.open_count == 1


@pytest.mark.asyncio
async def test_network_read_error_retries_twice_then_fails() -> None:
    class FailingObjectClient:
        open_count = 0

        def test_source_bucket(self, bucket_name: str) -> None:
            return None

        def list_source_objects(self, bucket_name: str):
            return iter(())

        def open_object(self, bucket_name: str, object_key: str) -> io.BytesIO:
            self.open_count += 1
            raise ConnectionError("temporary source read failure")

    client: R2Client = FailingObjectClient()
    outcome = await scan_object_with_retries(client, "source", SourceObject("unavailable.tar.gz", 23), 120, 2)

    assert outcome.classification == "failed"
    assert client.open_count == 3


def test_report_preserves_multi_model_associations_without_duplicates() -> None:
    source_objects = [SourceObject(key="b.tar.gz", size=20), SourceObject(key="a.tar.gz", size=10), SourceObject(key="notes.txt", size=5)]
    outcomes = {"a.tar.gz": ObjectScanOutcome("matched", ("model-b", "model-a", "model-a")), "b.tar.gz": ObjectScanOutcome("unmatched"), "notes.txt": ObjectScanOutcome("non_archive")}
    bucket_report = build_source_bucket_report("source", source_objects, outcomes)
    report = build_scan_report("job-id", "timestamp", [bucket_report])
    assert report["source_buckets"][0]["models"]["model-a"]["object_count"] == 1
    assert report["source_buckets"][0]["models"]["model-a"]["total_bytes"] == 10
    assert report["source_buckets"][0]["models"]["model-b"]["objects"] == [{"key": "a.tar.gz", "size": 10}]
    assert report["source_buckets"][0]["unmatched_objects"] == [{"key": "b.tar.gz", "size": 20}]
    assert report["source_buckets"][0]["non_archive_objects"] == [{"key": "notes.txt", "size": 5}]


def test_multi_source_report_keeps_same_model_in_separate_buckets() -> None:
    first_bucket_report = build_source_bucket_report(
        "招 2",
        [SourceObject(key="first.tar.gz", size=10)],
        {"first.tar.gz": ObjectScanOutcome("matched", ("sol",))},
    )
    second_bucket_report = build_source_bucket_report(
        "招 3",
        [SourceObject(key="second.tar.gz", size=20)],
        {"second.tar.gz": ObjectScanOutcome("matched", ("sol",))},
    )

    report = build_scan_report(
        "job-id",
        "timestamp",
        [first_bucket_report, second_bucket_report],
    )

    assert [bucket["source_bucket"] for bucket in report["source_buckets"]] == ["招 2", "招 3"]
    assert report["source_buckets"][0]["models"]["sol"]["objects"] == [
        {"key": "first.tar.gz", "size": 10}
    ]
    assert report["source_buckets"][1]["models"]["sol"]["objects"] == [
        {"key": "second.tar.gz", "size": 20}
    ]
