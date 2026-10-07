from pathlib import Path
import asyncio
import io
import tarfile

import pytest

from backend.config import AppSettings
from backend.incremental import IncrementalService
from backend.models import BucketModelMapping, SourceObject
from backend.storage import JobStorage


@pytest.mark.asyncio
async def test_route_source_change_is_superseded_without_copy(tmp_path: Path) -> None:
    settings = make_ready_settings(tmp_path)
    storage = JobStorage(tmp_path)
    service = IncrementalService(settings, storage, lambda: None)
    object_id, _ = storage.incremental.upsert_object("source", SourceObject("item.tar.gz", 10, "old"))
    assert storage.incremental.enqueue_route(object_id, "source", "item.tar.gz", "target", ["model"])
    task = storage.incremental.claim_next_route()
    assert task is not None

    client = RoutingClient(SourceObject("item.tar.gz", 20, "new"))
    await service.process_route(task, client)

    with storage.incremental._connect() as connection:
        route = connection.execute("SELECT state,error_code FROM routing_tasks WHERE id=?", (task["id"],)).fetchone()
    assert route["state"] == "superseded"
    assert route["error_code"] == "source_changed"
    assert client.copies == []


@pytest.mark.asyncio
async def test_continuous_toggle_starts_and_stops_consumers_independently_of_backfill(
    tmp_path: Path,
) -> None:
    settings = AppSettings(
        admin_password="test-password",
        data_directory=tmp_path,
        cookie_secure=False,
        r2_endpoint="https://r2.example",
        r2_access_key_id="access",
        r2_secret_access_key="secret",
        cloudflare_account_id="account",
        cloudflare_queue_id="queue",
        cloudflare_api_token="token",
        worker_concurrency=1,
    )
    storage = JobStorage(tmp_path)
    storage.save_connection("https://r2.example", ("source",))
    service = IncrementalService(settings, storage, lambda: None)

    service.set_continuous(True)
    await service.start()
    names = [task.get_name() for task in service.tasks if not task.done()]
    assert "incremental-backfill" in names
    assert "incremental-queue" in names
    assert "incremental-reconcile" in names
    assert names.count("incremental-worker") == 1

    service.set_continuous(False)
    await service.apply_continuous_state()
    names = [task.get_name() for task in service.tasks if not task.done()]
    assert names == ["incremental-backfill"]

    await service.stop()


class RoutingClient:
    def __init__(self, current: SourceObject) -> None:
        self.current = current
        self.copies: list[tuple[str, str]] = []

    def head_object(self, bucket: str, key: str) -> SourceObject:
        return self.current

    def object_exists(self, bucket: str, key: str) -> bool:
        return False

    def copy_object(self, source_bucket: str, source_key: str, target_bucket: str, target_key: str) -> None:
        self.copies.append((source_bucket, source_key))


class BackfillClient:
    def __init__(self, pages: list[tuple[list[SourceObject], str | None]] | None = None) -> None:
        self.opened: list[str] = []
        self.pages = pages or []

    def head_object(self, bucket: str, key: str) -> SourceObject:
        return SourceObject(key, 10, "etag")

    def list_source_page(self, bucket: str, token: str | None, page_size: int) -> tuple[list[SourceObject], str | None]:
        if self.pages:
            return self.pages.pop(0)
        return [], None

    def open_object_conditional(self, bucket: str, key: str, etag: str | None = None) -> io.BytesIO:
        self.opened.append(key)
        body = io.BytesIO()
        with tarfile.open(fileobj=body, mode="w:gz") as archive:
            member = tarfile.TarInfo("roots/primary/model/config.json")
            member.size = 0
            archive.addfile(member)
        body.seek(0)
        return body


def make_ready_settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        admin_password="test-password",
        data_directory=tmp_path,
        cookie_secure=False,
        r2_endpoint="https://r2.example",
        r2_access_key_id="access",
        r2_secret_access_key="secret",
        cloudflare_account_id="account",
        cloudflare_queue_id="queue",
        cloudflare_api_token="token",
    )


@pytest.mark.asyncio
async def test_paused_backfill_does_not_classify_and_running_backfill_does_even_when_continuous_disabled(tmp_path: Path) -> None:
    settings = make_ready_settings(tmp_path)
    storage = JobStorage(tmp_path)
    client = BackfillClient()
    service = IncrementalService(settings, storage, lambda: client)
    object_id, _ = storage.incremental.upsert_object("source", SourceObject("item.tar.gz", 10, "etag"), "backfill")
    storage.save_connection("https://r2.example", ("source",))

    storage.incremental.update_backfill(status="paused", source_bucket="source")
    worker = asyncio.create_task(service.backfill_worker_loop())
    await asyncio.sleep(0.05)
    assert client.opened == []
    storage.incremental.update_backfill(status="running")
    service.wake_event.set()
    for _ in range(20):
        if storage.incremental.get_object(object_id)["state"] != "queued":
            break
        await asyncio.sleep(0.01)
    service.stop_event.set()
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    assert client.opened == ["item.tar.gz"]
    object_row = storage.incremental.get_object(object_id)
    assert object_row["classification"] == "awaiting_mapping"
    assert object_row["state"] == "unmapped"
    assert service.continuous_enabled is False


@pytest.mark.asyncio
async def test_backfill_drains_classification_before_completed(tmp_path: Path) -> None:
    settings = make_ready_settings(tmp_path)
    storage = JobStorage(tmp_path)
    storage.save_connection("https://r2.example", ("source",))
    client = BackfillClient(
        pages=[([SourceObject("history.tar.gz", 10, "etag")], None)]
    )
    service = IncrementalService(settings, storage, lambda: client)
    await service.start_backfill()
    listing = asyncio.create_task(service.backfill_loop())
    for _ in range(100):
        if storage.incremental.list_backfill().get("status") == "completed":
            break
        await asyncio.sleep(0.01)
    await service.stop()
    listing.cancel()
    await asyncio.gather(listing, return_exceptions=True)
    assert storage.incremental.list_backfill()["status"] == "completed"
    assert storage.incremental.list_backfill()["listing_complete"] == 1
    assert storage.incremental.counts().get("queued", 0) == 0
    object_row = storage.incremental.get_current_object("source", "history.tar.gz")
    assert object_row["classification"] == "awaiting_mapping"
    assert object_row["state"] == "unmapped"


@pytest.mark.asyncio
async def test_backfill_classification_waits_for_enable_before_enqueueing_existing_mapping(
    tmp_path: Path,
) -> None:
    settings = make_ready_settings(tmp_path)
    storage = JobStorage(tmp_path)
    storage.save_connection("https://r2.example", ("source",))
    storage.save_mappings([BucketModelMapping(source_bucket="source", model_name="model", target_bucket="target")])
    client = BackfillClient(pages=[([SourceObject("history.tar.gz", 10, "etag")], None)])
    service = IncrementalService(settings, storage, lambda: client)
    await service.start_backfill()
    listing = asyncio.create_task(service.backfill_loop())
    try:
        object_row = None
        for _ in range(100):
            object_row = storage.incremental.get_current_object("source", "history.tar.gz")
            if object_row and object_row["classification"] == "awaiting_mapping":
                break
            await asyncio.sleep(0.01)

        assert object_row is not None
        assert object_row["classification"] == "awaiting_mapping"
        assert object_row["state"] == "unmapped"
        assert storage.incremental.counts().get("route_pending", 0) == 0
        assert client.opened == ["history.tar.gz"]

        # Keep the task pending so this assertion observes the enqueue boundary,
        # rather than the worker consuming it immediately.
        service.get_client = lambda: None
        service.set_continuous(True)
        await service.apply_continuous_state()

        assert storage.incremental.counts().get("route_pending", 0) == 1
        assert client.opened == ["history.tar.gz"]

        # Re-applying the lifecycle transition is idempotent.
        await service.apply_continuous_state()
        assert storage.incremental.counts().get("route_pending", 0) == 1
    finally:
        await service.stop()
        listing.cancel()
        await asyncio.gather(listing, return_exceptions=True)


@pytest.mark.asyncio
async def test_mapping_save_stays_paused_until_continuous_mode_is_enabled(tmp_path: Path) -> None:
    settings = make_ready_settings(tmp_path)
    storage = JobStorage(tmp_path)
    storage.save_connection("https://r2.example", ("source",))
    service = IncrementalService(settings, storage, lambda: None)
    object_id, _ = storage.incremental.upsert_object(
        "source", SourceObject("item.tar.gz", 10, "etag")
    )
    claimed = storage.incremental.claim_next_object()
    assert claimed is not None
    assert storage.incremental.complete_classification(
        int(claimed["id"]), "awaiting_mapping", ("model",), state="unmapped"
    )

    mappings = [
        BucketModelMapping(source_bucket="source", model_name="model", target_bucket="target")
    ]
    storage.save_mappings(mappings)
    assert service.enqueue_mapped_routes_if_enabled(mappings) == 0
    assert storage.incremental.counts().get("route_pending", 0) == 0

    service.set_continuous(True)
    await service.apply_continuous_state()
    assert storage.incremental.counts().get("route_pending", 0) == 1
    await service.apply_continuous_state()
    assert storage.incremental.counts().get("route_pending", 0) == 1
    await service.stop()


@pytest.mark.asyncio
async def test_queue_backlog_is_persisted_and_returned_in_status(tmp_path: Path, monkeypatch) -> None:
    settings = make_ready_settings(tmp_path)
    storage = JobStorage(tmp_path)
    service = IncrementalService(settings, storage, lambda: None)

    class FakeQueue:
        def __init__(self, account_id: str, queue_id: str, api_token: str) -> None:
            self.last_backlog_count: int | None = None

        def pull(self, visibility_timeout_ms: int, batch_size: int) -> list[object]:
            self.last_backlog_count = 17
            service.stop_event.set()
            return []

        def ack(self, lease_ids: list[str]) -> None:
            raise AssertionError("empty pull should not acknowledge leases")

    async def no_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("backend.incremental.QueueClient", FakeQueue)
    monkeypatch.setattr("backend.incremental.asyncio.sleep", no_sleep)
    service.set_continuous(True)

    await service.queue_loop()

    assert service.status()["queue_backlog_count"] == 17
    restored = IncrementalService(settings, storage, lambda: None)
    assert restored.status()["queue_backlog_count"] == 17


@pytest.mark.asyncio
async def test_persisted_continuous_toggle_is_restored_on_start(tmp_path: Path) -> None:
    settings = AppSettings(
        admin_password="test-password",
        data_directory=tmp_path,
        cookie_secure=False,
        r2_endpoint="https://r2.example",
        r2_access_key_id="access",
        r2_secret_access_key="secret",
        cloudflare_account_id="account",
        cloudflare_queue_id="queue",
        cloudflare_api_token="token",
        worker_concurrency=1,
    )
    storage = JobStorage(tmp_path)
    storage.save_connection("https://r2.example", ("source",))
    storage.incremental.set_state("continuous_enabled", "true")
    service = IncrementalService(settings, storage, lambda: None)

    await service.start()
    names = [task.get_name() for task in service.tasks if not task.done()]
    assert "incremental-queue" in names
    assert "incremental-reconcile" in names

    await service.stop()
