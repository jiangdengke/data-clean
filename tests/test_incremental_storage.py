from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.models import SourceObject
from backend.storage import IncrementalStore


def test_duplicate_object_fingerprint_is_idempotent(tmp_path: Path) -> None:
    store = IncrementalStore(tmp_path / "incremental.sqlite3")
    first_id, first_created = store.upsert_object("source", SourceObject("a.tar.gz", 10, "etag"))
    second_id, second_created = store.upsert_object("source", SourceObject("a.tar.gz", 10, "etag"))

    assert first_created is True
    assert second_created is False
    assert second_id == first_id
    assert store.counts()["queued"] == 1


def test_object_claim_is_atomic_for_concurrent_workers(tmp_path: Path) -> None:
    store = IncrementalStore(tmp_path / "incremental.sqlite3")
    store.upsert_object("source", SourceObject("a.tar.gz", 10, "etag"))

    def claim() -> dict | None:
        return IncrementalStore(tmp_path / "incremental.sqlite3").claim_next_object()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(lambda _: claim(), range(2)))

    non_empty = [row for row in claimed if row is not None]
    assert len(non_empty) == 1
    assert non_empty[0]["state"] == "scanning"
    assert store.get_object(non_empty[0]["id"])["attempts"] == 1


def test_object_origin_is_persisted_and_allowlisted(tmp_path: Path) -> None:
    store = IncrementalStore(tmp_path / "incremental.sqlite3")
    object_id, _ = store.upsert_object("source", SourceObject("backfill.tar.gz", 10, "etag"), "backfill")
    assert store.get_object(object_id)["origin"] == "backfill"
    with pytest.raises(ValueError):
        store.upsert_object("source", SourceObject("bad.tar.gz", 10, "etag-2"), "unknown")


def test_backfill_claim_requires_running_status(tmp_path: Path) -> None:
    store = IncrementalStore(tmp_path / "incremental.sqlite3")
    object_id, _ = store.upsert_object("source", SourceObject("a.tar.gz", 10, "etag"), "backfill")
    assert store.claim_next_object(origin="backfill", require_backfill_running=True) is None
    store.update_backfill(status="running")
    claimed = store.claim_next_object(origin="backfill", require_backfill_running=True)
    assert claimed is not None and claimed["id"] == object_id


def test_retry_becomes_terminal_at_bounded_attempts(tmp_path: Path) -> None:
    store = IncrementalStore(tmp_path / "incremental.sqlite3", max_attempts=2)
    object_id, _ = store.upsert_object("source", SourceObject("a.tar.gz", 10, "etag"))
    first = store.claim_next_object()
    assert first is not None
    store.fail_object(object_id, "temporary", True)
    assert store.get_object(object_id)["state"] == "retry_wait"
    with store._connect() as connection:
        connection.execute("UPDATE objects SET next_retry_at=? WHERE id=?", ("1970-01-01T00:00:00+00:00", object_id))
    second = store.claim_next_object()
    assert second is not None
    store.fail_object(object_id, "temporary", True)
    terminal = store.get_object(object_id)
    assert terminal["state"] == "failed"
    assert terminal["next_retry_at"] is None


def test_recover_inflight_claims_requeues_objects_and_routes(tmp_path: Path) -> None:
    store = IncrementalStore(tmp_path / "incremental.sqlite3")
    object_id, _ = store.upsert_object("source", SourceObject("a.tar.gz", 10, "etag"))
    claimed_object = store.claim_next_object()
    assert claimed_object is not None and claimed_object["id"] == object_id
    assert store.enqueue_route(object_id, "source", "a.tar.gz", "target", ["model"])
    claimed_route = store.claim_next_route()
    assert claimed_route is not None

    recovered = store.recover_inflight_claims()

    assert recovered == {"objects": 1, "routing_tasks": 1}
    assert store.get_object(object_id)["state"] == "queued"
    assert store.claim_next_object()["id"] == object_id
    with store._connect() as connection:
        route = connection.execute("SELECT state FROM routing_tasks WHERE id=?", (claimed_route["id"],)).fetchone()
    assert route["state"] == "pending"


def test_queue_event_payload_is_sanitized(tmp_path: Path) -> None:
    store = IncrementalStore(tmp_path / "incremental.sqlite3")
    event = {
        "account": "account",
        "action": "PutObject",
        "bucket": "source",
        "eventTime": "2026-10-07T00:00:00Z",
        "object": {"key": "a.tar.gz", "size": 10, "eTag": "etag", "secret": "do-not-store"},
        "credentials": "do-not-store",
        "token": "do-not-store",
    }
    store.ingest_event("event-1", event, SourceObject("a.tar.gz", 10, "etag"))

    with store._connect() as connection:
        payload = connection.execute("SELECT payload_json FROM queue_events").fetchone()["payload_json"]
    assert "do-not-store" not in payload
    assert "credentials" not in payload
    assert '"eventTime"' in payload


def test_route_claim_is_atomic_for_concurrent_workers(tmp_path: Path) -> None:
    store = IncrementalStore(tmp_path / "incremental.sqlite3")
    object_id, _ = store.upsert_object("source", SourceObject("a.tar.gz", 10, "etag"))
    assert store.enqueue_route(object_id, "source", "a.tar.gz", "target", ["model"])

    def claim() -> dict | None:
        return IncrementalStore(tmp_path / "incremental.sqlite3").claim_next_route()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(lambda _: claim(), range(2)))

    non_empty = [row for row in claimed if row is not None]
    assert len(non_empty) == 1
    assert non_empty[0]["state"] == "running"
    assert non_empty[0]["attempts"] == 1
