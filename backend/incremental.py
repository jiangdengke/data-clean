"""Durable event, classification, routing, reconciliation and backfill orchestration."""

import asyncio
import base64
import json
from datetime import datetime, timezone
from typing import Any, Callable

from .config import AppSettings
from .models import BucketModelMapping, ConnectionSettings, SourceObject
from .queue_client import QueueClient, sanitize_r2_event, validate_r2_event
from .r2_client import R2Client, create_r2_client
from .scanner import scan_object_with_retries
from .storage import IncrementalStore, JobStorage, current_timestamp
from .sync import SyncAction, execute_sync_action_async
from .transfer import (
    SourceChangedBeforeTransferError,
    TargetVerificationError,
    TransferBinaryMissingError,
    TransferConfigurationError,
    UnsupportedTransferTopologyError,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IncrementalService:
    """Single-container durable worker; no task is acknowledged before SQLite commit."""

    def __init__(self, settings: AppSettings, storage: JobStorage, get_client: Callable[..., R2Client | None]) -> None:
        self.settings = settings
        self.storage = storage
        self.store: IncrementalStore = storage.incremental
        self.get_client = get_client
        self.stop_event = asyncio.Event()
        self.wake_event = asyncio.Event()
        self.tasks: list[asyncio.Task[None]] = []
        self.queue_last_pull_at: str | None = self.store.get_state("queue_last_pull_at")
        self.queue_last_ack_at: str | None = self.store.get_state("queue_last_ack_at")
        persisted_backlog = self.store.get_state("queue_backlog_count")
        self.queue_backlog_count: int | None = int(persisted_backlog) if persisted_backlog is not None and persisted_backlog.isdigit() else None
        self.reconcile_last_run_at: str | None = self.store.get_state("reconcile_last_run_at")
        self.last_error: str | None = None

    def _get_client(self, source_bucket: str | None = None) -> R2Client | None:
        """Resolve a bucket client while retaining zero-argument test factories."""
        try:
            return self.get_client(source_bucket)
        except TypeError:
            return self.get_client()

    @property
    def continuous_enabled(self) -> bool:
        return self.store.get_state("continuous_enabled", "false") == "true"

    @property
    def backfill_paused(self) -> bool:
        return self.store.list_backfill().get("status") == "paused"

    def set_continuous(self, enabled: bool) -> None:
        """Persist the administrator toggle without changing task state yet.

        The caller must await :meth:`apply_continuous_state` so disabling can
        cancel consumers and enabling can create them even when the backfill
        task is already running.
        """
        self.store.set_state("continuous_enabled", "true" if enabled else "false")
        self.wake_event.set()

    def _task_named(self, name: str) -> asyncio.Task[None] | None:
        for task in self.tasks:
            if task.get_name() == name and not task.done():
                return task
        return None

    async def _stop_continuous_tasks(self) -> None:
        consumer_names = {"incremental-queue", "incremental-reconcile", "incremental-worker"}
        consumers = [task for task in self.tasks if task.get_name() in consumer_names]
        for task in consumers:
            task.cancel()
        if consumers:
            await asyncio.gather(*consumers, return_exceptions=True)
        self.tasks = [task for task in self.tasks if task.get_name() not in consumer_names and not task.done()]

    async def _start_continuous_tasks(self) -> None:
        if (
            not self.continuous_enabled
            or self.stop_event.is_set()
            or not self.runtime_ready()
        ):
            return
        # A mapping may have been saved while continuous processing was off.
        # Reconcile already-classified objects when the administrator enables it;
        # classification that happens later is routed by the worker itself.
        self.store.enqueue_mapped_routes(self.storage.get_mappings())
        if self._task_named("incremental-queue") is None and self.settings.queue_credentials_ready:
            self.tasks.append(asyncio.create_task(self.queue_loop(), name="incremental-queue"))
        if self._task_named("incremental-reconcile") is None:
            self.tasks.append(asyncio.create_task(self.reconcile_loop(), name="incremental-reconcile"))
        existing_workers = sum(1 for task in self.tasks if task.get_name() == "incremental-worker" and not task.done())
        self.tasks.extend(
            asyncio.create_task(self.worker_loop(), name="incremental-worker")
            for _ in range(max(0, self.settings.worker_concurrency - existing_workers))
        )

    async def apply_continuous_state(self) -> None:
        """Make running consumer tasks match the persisted toggle."""
        if self.continuous_enabled:
            await self._start_continuous_tasks()
        else:
            await self._stop_continuous_tasks()
        self.wake_event.set()

    def runtime_ready(self) -> bool:
        """Return readiness for the saved per-bucket profiles and Queue runtime."""
        return self._runtime_ready() and self.settings.queue_credentials_ready

    def readiness_message(self) -> str:
        profiles = self.storage.get_connection_profiles()
        if not profiles:
            return "请先配置至少一个源桶连接"
        return self.settings.readiness_message_for(
            tuple(profile.credential_ref for profile in profiles),
            tuple(profile.endpoint for profile in profiles),
        )

    def status(self) -> dict[str, Any]:
        return {
            "continuous_enabled": self.continuous_enabled,
            "paused": not self.continuous_enabled,
            "runtime_ready": self.runtime_ready(),
            "readiness_message": self.readiness_message(),
            "backfill": self.store.list_backfill(),
            "counts": self.store.counts(),
            "queue_last_pull_at": self.queue_last_pull_at,
            "queue_last_ack_at": self.queue_last_ack_at,
            "queue_backlog_count": self.queue_backlog_count,
            "reconcile_last_run_at": self.reconcile_last_run_at,
            "last_error": self.last_error,
        }

    def accept_event(self, event: dict[str, Any], event_key: str | None = None) -> int | None:
        validated = validate_r2_event(
            event,
            self.settings.cloudflare_account_id or None,
            self._source_buckets(),
        )
        if validated is None:
            return None
        account, bucket, key, action, size, etag = validated
        event = sanitize_r2_event(event)
        last_modified = event.get("eventTime") if isinstance(event.get("eventTime"), str) else None
        source_object = SourceObject(key=key, size=size, etag=etag, last_modified=last_modified)
        fingerprint = source_object.fingerprint
        identity = event_key or f"{bucket}:{key}:{fingerprint}"
        object_id, _ = self.store.ingest_event(identity, event, source_object)
        self.wake_event.set()
        return object_id

    async def process_object(
        self, object_row: dict[str, Any], client: R2Client, allow_routing: bool = True
    ) -> None:
        object_id = int(object_row["id"])
        source_bucket = str(object_row["source_bucket"])
        object_key = str(object_row["object_key"])
        head_method = getattr(client, "head_object", None)
        if callable(head_method):
            current = await asyncio.to_thread(head_method, source_bucket, object_key)
        else:
            # Keep compatibility with the small scanner fakes used by older
            # callers; real clients implement HEAD and take the branch above.
            current = SourceObject(
                object_key,
                int(object_row["size"]),
                object_row.get("etag"),
                object_row.get("last_modified"),
            )
        # A missing event ETag is completed by HEAD. Compare the full stored
        # fingerprint so the durable index is upgraded to the provider version
        # before classification rather than being reread by reconciliation.
        metadata_changed = current.fingerprint != str(object_row.get("fingerprint", ""))
        if metadata_changed:
            self.store.upsert_object(source_bucket, current, origin="event")
            self.store.fail_object(object_id, "superseded", False)
            return
        event_etag = object_row.get("etag")
        if event_etag and current.etag and event_etag != current.etag:
            self.store.upsert_object(source_bucket, current, origin="event")
            self.store.fail_object(object_id, "superseded", False)
            return
        outcome = await scan_object_with_retries(
            client,
            str(object_row["source_bucket"]),
            current,
            self.settings.object_timeout_seconds,
            self.settings.object_retry_count,
        )
        if outcome.classification in {"failed", "timed_out"}:
            self.store.fail_object(object_id, outcome.classification, True)
            return
        if outcome.classification == "matched":
            targets = self._mapped_targets(str(object_row["source_bucket"]), outcome.model_names) if allow_routing else {}
            for target_bucket, target_models in targets.items():
                self.store.enqueue_route(
                    object_id,
                    str(object_row["source_bucket"]),
                    str(object_row["object_key"]),
                    target_bucket,
                    target_models,
                )
            classification = "matched" if targets else "awaiting_mapping"
            state = "routing" if targets else "unmapped"
            self.store.complete_classification(object_id, classification, outcome.model_names, state=state)
        else:
            self.store.complete_classification(object_id, outcome.classification, outcome.model_names)

    def enqueue_mapped_routes_if_enabled(self, mappings: list[BucketModelMapping]) -> int:
        """Queue routes for classified objects only while continuous mode is enabled."""
        if not self.continuous_enabled:
            return 0
        return self.store.enqueue_mapped_routes(mappings)

    def _mapped_targets(self, source_bucket: str, model_names: tuple[str, ...]) -> dict[str, list[str]]:
        mappings = {
            (item.source_bucket, item.model_name): item.target_bucket
            for item in self.storage.get_mappings()
        }
        targets: dict[str, list[str]] = {}
        for model_name in model_names:
            target_bucket = mappings.get((source_bucket, model_name))
            if target_bucket:
                targets.setdefault(target_bucket, []).append(model_name)
        return targets

    def _enqueue_routes_for_classification(
        self, object_row: dict[str, Any], object_id: int, model_names: tuple[str, ...]
    ) -> int:
        targets = self._mapped_targets(str(object_row["source_bucket"]), model_names)
        for target_bucket, target_models in targets.items():
            self.store.enqueue_route(
                object_id,
                str(object_row["source_bucket"]),
                str(object_row["object_key"]),
                target_bucket,
                target_models,
            )
        return len(targets)

    async def process_route(self, task: dict[str, Any], client: R2Client) -> None:
        try:
            object_row = self.store.get_object(int(task["object_id"]))
            if object_row is None:
                self.store.finish_route(int(task["id"]), "superseded", "source_missing")
                return
            current = await asyncio.to_thread(
                client.head_object, str(task["source_bucket"]), str(task["object_key"])
            )
            stored_size = int(object_row.get("size", 0))
            if current.fingerprint != str(object_row.get("fingerprint", "")):
                self.store.finish_route(int(task["id"]), "superseded", "source_changed")
                self.store.upsert_object(str(task["source_bucket"]), current, origin="event")
                return
            action = SyncAction(
                source_bucket=str(task["source_bucket"]),
                model_names=tuple(json.loads(task["model_names_json"])),
                target_bucket=str(task["target_bucket"]),
                object_key=str(task["object_key"]),
                size=stored_size,
                expected_etag=(
                    str(object_row["etag"])
                    if object_row.get("etag") is not None
                    else None
                ),
            )
            result = await execute_sync_action_async(client, action)
            if result["status"] == "copied":
                self.store.finish_route(int(task["id"]), "copied")
            else:
                self.store.finish_route(int(task["id"]), "conflict", "target_exists")
        except SourceChangedBeforeTransferError:
            self.store.finish_route(int(task["id"]), "superseded", "source_changed")
        except TargetVerificationError:
            self.store.finish_route(
                int(task["id"]), "conflict", "target_verification_failed"
            )
        except UnsupportedTransferTopologyError:
            self.store.finish_route(
                int(task["id"]), "failed", "unsupported_transfer_topology"
            )
        except TransferBinaryMissingError:
            self.store.finish_route(
                int(task["id"]), "failed", "transfer_binary_unavailable"
            )
        except TransferConfigurationError:
            self.store.finish_route(
                int(task["id"]), "failed", "transfer_configuration_invalid"
            )
        except Exception:
            self.store.finish_route(int(task["id"]), "retry_wait", "copy_failed")

    async def backfill_worker_loop(self) -> None:
        """Classify only backfill objects while the durable job is running.

        The status predicate is enforced inside ``claim_next_object`` as well as
        in this loop.  The second check avoids starting a new classification if
        pause wins between the claim transaction and this worker's next turn.
        """
        while not self.stop_event.is_set():
            backfill_status = self.store.list_backfill().get("status")
            if backfill_status not in {"running", "draining"}:
                await self._wait_for_wake(2)
                continue
            object_row = self.store.claim_next_object(
                origin="backfill", require_backfill_running=True
            )
            if object_row is None:
                if self.store.list_backfill().get("status") == "draining":
                    self.store.complete_backfill_if_drained()
                await self._wait_for_wake(2)
                continue
            client = self._get_client(str(object_row["source_bucket"]))
            if client is None:
                self.store.release_claim(int(object_row["id"]))
                await self._wait_for_wake(5)
                continue
            object_id = int(object_row["id"])
            if self.store.list_backfill().get("status") not in {"running", "draining"}:
                self.store.release_claim(object_id)
                continue
            try:
                await self.process_object(
                    object_row,
                    client,
                    allow_routing=self.continuous_enabled,
                )
                current = self.store.get_object(object_id)
                if current and current.get("state") not in {"scanning", "queued", "retry_wait"}:
                    self.store.update_backfill(
                        total_classified=int(self.store.list_backfill().get("total_classified") or 0) + 1
                    )
            except asyncio.CancelledError:
                self.store.release_claim(object_id)
                raise
            except Exception:
                self.store.fail_object(object_id, "processing_failed", True)

    async def worker_loop(self) -> None:
        while not self.stop_event.is_set():
            if not self.continuous_enabled:
                await self._wait_for_wake(2)
                continue
            route = self.store.claim_next_route()
            if route:
                client = self._get_client(str(route["source_bucket"]))
                if client is None:
                    self.store.finish_route(int(route["id"]), "retry_wait", "r2_not_ready")
                    await self._wait_for_wake(5)
                    continue
                await self.process_route(route, client)
                continue
            object_row = self.store.claim_next_object(exclude_origin="backfill")
            if object_row:
                client = self._get_client(str(object_row["source_bucket"]))
                if client is None:
                    self.store.release_claim(int(object_row["id"]))
                    await self._wait_for_wake(5)
                    continue
                try:
                    await self.process_object(object_row, client)
                except Exception:
                    self.store.fail_object(int(object_row["id"]), "processing_failed", True)
                continue
            await self._wait_for_wake(2)

    async def queue_loop(self) -> None:
        if not self.settings.queue_credentials_ready:
            return
        queue = QueueClient(self.settings.cloudflare_account_id, self.settings.cloudflare_queue_id, self.settings.cloudflare_api_token)
        while not self.stop_event.is_set():
            if not self.continuous_enabled:
                await self._wait_for_wake(5)
                continue
            try:
                messages = await asyncio.to_thread(queue.pull, self.settings.queue_visibility_timeout_ms, self.settings.queue_batch_size)
                self.queue_last_pull_at = _now()
                self.store.set_state("queue_last_pull_at", self.queue_last_pull_at)
                if queue.last_backlog_count is not None:
                    self.queue_backlog_count = queue.last_backlog_count
                    self.store.set_state("queue_backlog_count", str(self.queue_backlog_count))
                leases: list[str] = []
                for message in messages:
                    if self.accept_event(message.body, f"lease:{message.lease_id}") is not None:
                        leases.append(message.lease_id)
                if leases:
                    await asyncio.to_thread(queue.ack, leases)
                    self.queue_last_ack_at = _now()
                    self.store.set_state("queue_last_ack_at", self.queue_last_ack_at)
                await asyncio.sleep(self.settings.queue_poll_interval_seconds if messages else self.settings.queue_idle_poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error = "Queue pull failed"
                await asyncio.sleep(self.settings.queue_idle_poll_interval_seconds)

    async def reconcile_once(self) -> int:
        if not self.continuous_enabled:
            return 0
        created = 0
        for bucket in self._source_buckets():
            try:
                client = self._get_client(bucket)
                if client is None:
                    self.last_error = "R2 credentials are not ready"
                    continue
                page_method = getattr(client, "list_source_page", None)
                if callable(page_method):
                    token: str | None = None
                    while True:
                        objects, token = await asyncio.to_thread(
                            page_method, bucket, token, self.settings.backfill_page_size
                        )
                        created += self.store.metadata_reconciliation(bucket, objects)
                        if not token:
                            break
                else:
                    source_objects = await asyncio.to_thread(
                        lambda: list(client.list_source_objects(bucket))
                    )
                    created += self.store.metadata_reconciliation(bucket, source_objects)
            except Exception:
                self.last_error = "Metadata reconciliation failed"
        self.reconcile_last_run_at = _now()
        self.store.set_state("reconcile_last_run_at", self.reconcile_last_run_at)
        return created

    async def reconcile_loop(self) -> None:
        while not self.stop_event.is_set():
            await self._wait_for_wake(self.settings.reconcile_interval_seconds)
            if self.stop_event.is_set():
                return
            await self.reconcile_once()

    def _runtime_ready(self) -> bool:
        profiles = self.storage.get_connection_profiles()
        return (
            self.settings.transfer_adapter_ready
            and self.settings.transfer_topology_ready(
                tuple(profile.endpoint for profile in profiles)
            )
            and bool(profiles)
            and all(
                profile.endpoint.strip()
                and self.settings.credentials_ready_for(profile.credential_ref)
                for profile in profiles
            )
        )

    def _source_buckets(self) -> tuple[str, ...]:
        return tuple(profile.source_bucket for profile in self.storage.get_connection_profiles())

    async def _ensure_backfill_worker(self) -> None:
        """Create at most one classifier, and only for a running backfill."""
        if (
            self.stop_event.is_set()
            or not self._runtime_ready()
            or self.store.list_backfill().get("status") not in {"running", "draining"}
        ):
            return
        if self._task_named("incremental-backfill-worker") is None:
            self.tasks.append(
                asyncio.create_task(self.backfill_worker_loop(), name="incremental-backfill-worker")
            )

    async def start_backfill(self) -> None:
        if not self._runtime_ready():
            self.store.update_backfill(status="failed", error_code="r2_not_ready")
            return
        state = self.store.list_backfill()
        current_status = state.get("status")
        if current_status in {"running", "draining"}:
            # Idempotent start: do not reset the checkpoint or create a second
            # worker when an admin double-clicks the action or after a restart.
            await self._ensure_backfill_worker()
            self.wake_event.set()
            return
        if current_status == "paused":
            self.store.update_backfill(status="running", error_code=None)
            await self._ensure_backfill_worker()
            self.wake_event.set()
            return
        buckets = self._source_buckets()
        if not buckets:
            self.store.update_backfill(status="failed", error_code="no_source_buckets")
            return
        self.store.update_backfill(
            status="running",
            source_bucket=buckets[0],
            continuation_token=None,
            total_seen=0,
            total_classified=0,
            listing_complete=0,
            started_at=_now(),
            error_code=None,
        )
        await self._ensure_backfill_worker()
        self.wake_event.set()

    def pause_backfill(self) -> None:
        if self.store.list_backfill().get("status") in {"running", "draining"}:
            self.store.update_backfill(status="paused")
            # Keep the worker alive so resume is a state transition, but its
            # claim transaction explicitly refuses work while paused.
            self.wake_event.set()

    async def backfill_loop(self) -> None:
        while not self.stop_event.is_set():
            state = self.store.list_backfill()
            if state.get("status") != "running":
                await self._wait_for_wake(2)
                continue
            if state.get("listing_complete"):
                self.store.update_backfill_if_status("running", status="draining")
                self.store.complete_backfill_if_drained()
                await self._wait_for_wake(2)
                continue
            bucket = str(state.get("source_bucket") or "")
            if not bucket:
                self.store.update_backfill(status="completed")
                continue
            client = self._get_client(bucket)
            if client is None:
                await self._wait_for_wake(5)
                continue
            try:
                page_method = getattr(client, "list_source_page", None)
                if callable(page_method):
                    objects, token = await asyncio.to_thread(page_method, bucket, state.get("continuation_token"), self.settings.backfill_page_size)
                else:
                    objects = await asyncio.to_thread(lambda: list(client.list_source_objects(bucket)))
                    token = None
                for source_object in objects:
                    self.store.upsert_object(bucket, source_object, origin="backfill")
                # Wake the classifier immediately after durable page insertion;
                # otherwise a worker sleeping on its polling timeout can delay
                # draining and make completion appear stuck.
                self.wake_event.set()
                total_seen = int(state.get("total_seen") or 0) + len(objects)
                if token:
                    self.store.update_backfill(continuation_token=token, total_seen=total_seen)
                else:
                    buckets = self._source_buckets()
                    index = buckets.index(bucket)
                    if index + 1 < len(buckets):
                        self.store.update_backfill(
                            source_bucket=buckets[index + 1],
                            continuation_token=None,
                            total_seen=total_seen,
                        )
                    else:
                        # Listing completion is durable but deliberately not the
                        # terminal job state: the drain worker must classify every
                        # queued backfill object before completion is recorded.
                        self.store.update_backfill(
                            listing_complete=1,
                            continuation_token=None,
                            total_seen=total_seen,
                        )
                        self.store.update_backfill_if_status("running", status="draining")
                        self.store.complete_backfill_if_drained()
            except Exception:
                self.store.update_backfill(status="failed", error_code="listing_failed")

    async def _wait_for_wake(self, timeout: int) -> None:
        try:
            await asyncio.wait_for(self.wake_event.wait(), timeout=timeout)
            self.wake_event.clear()
        except asyncio.TimeoutError:
            return

    async def start(self) -> None:
        self.stop_event.clear()
        self.store.recover_inflight_claims()
        if self._task_named("incremental-backfill") is None:
            self.tasks.append(asyncio.create_task(self.backfill_loop(), name="incremental-backfill"))
        if self.store.list_backfill().get("status") in {"running", "draining"} and self._runtime_ready():
            if self._task_named("incremental-backfill-worker") is None:
                self.tasks.append(asyncio.create_task(self.backfill_worker_loop(), name="incremental-backfill-worker"))
        await self.apply_continuous_state()

    async def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
