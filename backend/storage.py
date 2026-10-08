"""Credential-free JSON reports plus transactional SQLite incremental state."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, TypedDict, cast

from .models import BucketModelMapping, ScanReport, SourceConnectionProfile, SyncReport, SourceObject
from .queue_client import sanitize_r2_event


OBJECT_ORIGINS = frozenset({"event", "backfill", "reconcile"})
DEFAULT_INCREMENTAL_MAX_ATTEMPTS = 5
MAX_RETRY_DELAY_SECONDS = 300


def _add_missing_object_metadata(objects: object) -> None:
    if not isinstance(objects, list):
        return
    for object_reference in objects:
        if not isinstance(object_reference, dict):
            continue
        object_reference.setdefault("etag", None)
        object_reference.setdefault("last_modified", None)


def _normalize_legacy_scan_report(report: dict[str, Any]) -> None:
    source_buckets = report.get("source_buckets")
    if not isinstance(source_buckets, list):
        return
    for source_bucket in source_buckets:
        if not isinstance(source_bucket, dict):
            continue
        models = source_bucket.get("models")
        if isinstance(models, dict):
            for model_report in models.values():
                if isinstance(model_report, dict):
                    _add_missing_object_metadata(model_report.get("objects"))
        _add_missing_object_metadata(source_bucket.get("unmatched_objects"))
        _add_missing_object_metadata(source_bucket.get("non_archive_objects"))


def current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobProgress(TypedDict):
    total: int
    processed: int
    failed: int


class JobRecord(TypedDict, total=False):
    job_id: str
    sync_job_id: str
    job_type: str
    source_scan_job_id: str
    status: str
    source_bucket: str
    source_buckets: list[str]
    current_source_bucket: str | None
    progress: JobProgress
    report_available: bool
    error: str | None
    updated_at: str


class IncrementalStore:
    """Single-process transactional object/version index and durable work queues."""

    def __init__(self, database_path: Path, max_attempts: int = DEFAULT_INCREMENTAL_MAX_ATTEMPTS) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.database_path = database_path
        self.max_attempts = max_attempts
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS objects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_bucket TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    etag TEXT,
                    last_modified TEXT,
                    classification TEXT,
                    models_json TEXT NOT NULL DEFAULT '[]',
                    state TEXT NOT NULL DEFAULT 'queued',
                    current INTEGER NOT NULL DEFAULT 1,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    error_code TEXT,
                    origin TEXT NOT NULL DEFAULT 'event',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_bucket, object_key, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS objects_work_idx
                    ON objects(state, next_retry_at, updated_at);
                CREATE INDEX IF NOT EXISTS objects_key_idx
                    ON objects(source_bucket, object_key, current);
                CREATE TABLE IF NOT EXISTS routing_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    source_bucket TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    target_bucket TEXT NOT NULL,
                    model_names_json TEXT NOT NULL DEFAULT '[]',
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(object_id, target_bucket)
                );
                CREATE INDEX IF NOT EXISTS routing_work_idx
                    ON routing_tasks(state, next_retry_at, updated_at);
                CREATE TABLE IF NOT EXISTS queue_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    account TEXT,
                    source_bucket TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    accepted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backfill_state (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    status TEXT NOT NULL DEFAULT 'idle',
                    source_bucket TEXT,
                    continuation_token TEXT,
                    total_seen INTEGER NOT NULL DEFAULT 0,
                    total_classified INTEGER NOT NULL DEFAULT 0,
                    listing_complete INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    updated_at TEXT,
                    error_code TEXT
                );
                INSERT OR IGNORE INTO backfill_state(id) VALUES (1);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(objects)")}
            if "origin" not in columns:
                connection.execute("ALTER TABLE objects ADD COLUMN origin TEXT NOT NULL DEFAULT 'event'")
            backfill_columns = {row["name"] for row in connection.execute("PRAGMA table_info(backfill_state)")}
            if "listing_complete" not in backfill_columns:
                connection.execute(
                    "ALTER TABLE backfill_state ADD COLUMN listing_complete INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _row_to_object(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def get_state(self, key: str, default: str | None = None) -> str | None:
        """Read one non-secret application state value transactionally."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_state WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return default
        value = row["value"]
        return str(value) if value is not None else default

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO app_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def delete_state(self, key: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM app_state WHERE key=?", (key,))


    def ingest_event(self, event_key: str, event: dict[str, Any], source_object: SourceObject) -> tuple[int, bool]:
        """Persist an event and coalesce duplicate/out-of-order versions transactionally."""

        safe_event = sanitize_r2_event(event)
        now = current_timestamp()
        with self._connect() as connection:
            existing_event = connection.execute(
                "SELECT id FROM queue_events WHERE event_key=?", (event_key,)
            ).fetchone()
            if existing_event:
                current = connection.execute(
                    "SELECT id FROM objects WHERE source_bucket=? AND object_key=? AND current=1 ORDER BY id DESC LIMIT 1",
                    (event.get("bucket", ""), source_object.key),
                ).fetchone()
                return (int(current["id"]) if current else 0, False)
            object_id = self._upsert_object(connection, str(event.get("bucket", "")), source_object, now, "event")
            connection.execute(
                "INSERT INTO queue_events(event_key,account,source_bucket,object_key,action,payload_json,received_at,accepted_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    event_key,
                    event.get("account"),
                    event.get("bucket", ""),
                    source_object.key,
                    event.get("action", ""),
                    json.dumps(safe_event, sort_keys=True),
                    now,
                    now,
                ),
            )
            return object_id, True

    def upsert_object(
        self, source_bucket: str, source_object: SourceObject, origin: str = "event"
    ) -> tuple[int, bool]:
        self._validate_origin(origin)
        now = current_timestamp()
        with self._connect() as connection:
            return self._upsert_object(connection, source_bucket, source_object, now, origin)

    @staticmethod
    def _validate_origin(origin: str) -> None:
        if origin not in OBJECT_ORIGINS:
            raise ValueError(f"Unsupported object origin: {origin}")

    def _upsert_object(
        self,
        connection: sqlite3.Connection,
        source_bucket: str,
        source_object: SourceObject,
        now: str,
        origin: str = "event",
    ) -> tuple[int, bool]:
        self._validate_origin(origin)
        fingerprint = source_object.fingerprint
        existing = connection.execute(
            "SELECT id, current FROM objects WHERE source_bucket=? AND object_key=? AND fingerprint=?",
            (source_bucket, source_object.key, fingerprint),
        ).fetchone()
        if existing:
            # A HEAD/reconciliation result can rediscover a version that was
            # temporarily marked non-current by an out-of-order event. Promote
            # that known version without resetting its classification or work.
            # Event-only duplicates have no provider LastModified and must not
            # reorder already-indexed versions.
            if source_object.last_modified and not existing["current"]:
                connection.execute(
                    "UPDATE objects SET current=0 WHERE source_bucket=? AND object_key=?",
                    (source_bucket, source_object.key),
                )
                connection.execute(
                    "UPDATE objects SET current=1,updated_at=? WHERE id=?",
                    (now, existing["id"]),
                )
            return int(existing["id"]), False
        current = connection.execute(
            "SELECT id, last_modified FROM objects WHERE source_bucket=? AND object_key=? AND current=1 ORDER BY id DESC LIMIT 1",
            (source_bucket, source_object.key),
        ).fetchone()
        # Metadata timestamps are the only safe ordering signal available from an event.
        # If absent, a newly observed version is considered current; a dated stale event
        # cannot displace a newer dated version.
        is_current = not current or not source_object.last_modified or not current["last_modified"] or source_object.last_modified >= current["last_modified"]
        if is_current:
            connection.execute(
                "UPDATE objects SET current=0 WHERE source_bucket=? AND object_key=?",
                (source_bucket, source_object.key),
            )
        cursor = connection.execute(
            """INSERT INTO objects(source_bucket,object_key,fingerprint,size,etag,last_modified,state,current,origin,created_at,updated_at)
               VALUES(?,?,?,?,?,?, 'queued', ?, ?, ?, ?)""",
            (
                source_bucket,
                source_object.key,
                fingerprint,
                source_object.size,
                source_object.etag,
                source_object.last_modified,
                1 if is_current else 0,
                origin,
                now,
                now,
            ),
        )
        return int(cursor.lastrowid), True

    def get_object(self, object_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
        return self._row_to_object(row) if row else None

    def get_current_object(self, source_bucket: str, object_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM objects WHERE source_bucket=? AND object_key=? AND current=1 ORDER BY id DESC LIMIT 1",
                (source_bucket, object_key),
            ).fetchone()
        return self._row_to_object(row) if row else None

    def claim_next_object(
        self,
        include_non_archive: bool = True,
        origin: str | None = None,
        require_backfill_running: bool = False,
        exclude_origin: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically claim one eligible object so workers cannot duplicate it."""
        if origin is not None:
            self._validate_origin(origin)
        if exclude_origin is not None:
            self._validate_origin(exclude_origin)
        if require_backfill_running and origin != "backfill":
            raise ValueError("Backfill status can only gate backfill objects")
        now = current_timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            origin_clause = " AND origin=?" if origin is not None else ""
            exclude_origin_clause = " AND origin<>?" if exclude_origin is not None else ""
            parameters: tuple[Any, ...] = (now,)
            if origin is not None:
                parameters += (origin,)
            if exclude_origin is not None:
                parameters += (exclude_origin,)
            backfill_clause = " AND EXISTS (SELECT 1 FROM backfill_state WHERE id=1 AND status IN ('running','draining'))" if require_backfill_running else ""
            row = connection.execute(
                f"""SELECT * FROM objects WHERE state IN ('queued','retry_wait')
                   AND current=1
                   AND (next_retry_at IS NULL OR next_retry_at<=?){origin_clause}{exclude_origin_clause}{backfill_clause}
                   ORDER BY id LIMIT 1""",
                parameters,
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE objects SET state='scanning', attempts=attempts+1, updated_at=? WHERE id=? AND current=1",
                (now, row["id"]),
            )
            claimed = connection.execute("SELECT * FROM objects WHERE id=?", (row["id"],)).fetchone()
            return self._row_to_object(claimed) if claimed else None

    def complete_classification(
        self,
        object_id: int,
        classification: str,
        model_names: tuple[str, ...] = (),
        state: str | None = None,
    ) -> bool:
        now = current_timestamp()
        resolved_state = state
        if resolved_state is None:
            resolved_state = "classified" if classification in {"matched", "routing"} else classification
            if classification in {"unmatched", "awaiting_mapping"}:
                resolved_state = "unmapped"
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE objects SET classification=?, models_json=?, state=?, error_code=NULL, next_retry_at=NULL, updated_at=? WHERE id=? AND state='scanning' AND current=1",
                (classification, json.dumps(sorted(set(model_names))), resolved_state, now, object_id),
            )
        return cursor.rowcount > 0

    def fail_object(self, object_id: int, error_code: str, retryable: bool) -> None:
        now = current_timestamp()
        state = "retry_wait" if retryable else "failed"
        with self._connect() as connection:
            row = connection.execute("SELECT attempts FROM objects WHERE id=?", (object_id,)).fetchone()
            attempts = int(row["attempts"]) if row else 1
            bounded_retry = retryable and attempts < self.max_attempts
            state = "retry_wait" if bounded_retry else "failed"
            delay_seconds = min(MAX_RETRY_DELAY_SECONDS, 2 ** min(max(attempts - 1, 0), 8))
            next_retry_at = (
                (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
                if bounded_retry
                else None
            )
            connection.execute(
                "UPDATE objects SET state=?, error_code=?, next_retry_at=?, updated_at=? WHERE id=? AND state='scanning'",
                (state, error_code, next_retry_at, now, object_id),
            )

    def enqueue_route(self, object_id: int, source_bucket: str, object_key: str, target_bucket: str, model_names: list[str]) -> bool:
        now = current_timestamp()
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO routing_tasks(object_id,source_bucket,object_key,target_bucket,model_names_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (object_id, source_bucket, object_key, target_bucket, json.dumps(sorted(set(model_names))), now, now),
            )
            return cursor.rowcount > 0

    def claim_next_route(self) -> dict[str, Any] | None:
        now = current_timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM routing_tasks WHERE state IN ('pending','retry_wait')
                   AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY id LIMIT 1""",
                (now,),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE routing_tasks SET state='running', attempts=attempts+1, updated_at=? WHERE id=?",
                (now, row["id"]),
            )
            claimed = connection.execute("SELECT * FROM routing_tasks WHERE id=?", (row["id"],)).fetchone()
            return self._row_to_object(claimed) if claimed else None

    def finish_route(self, task_id: int, state: str, error_code: str | None = None) -> None:
        now = current_timestamp()
        with self._connect() as connection:
            row = connection.execute("SELECT attempts FROM routing_tasks WHERE id=?", (task_id,)).fetchone()
            attempts = int(row["attempts"]) if row else 1
            next_retry_at = None
            if state == "retry_wait" and attempts < self.max_attempts:
                delay_seconds = min(MAX_RETRY_DELAY_SECONDS, 2 ** min(max(attempts - 1, 0), 8))
                next_retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
            elif state == "retry_wait":
                state = "failed"
            connection.execute(
                "UPDATE routing_tasks SET state=?, error_code=?, next_retry_at=?, updated_at=? WHERE id=? AND state='running'",
                (state, error_code, next_retry_at, now, task_id),
            )

    def enqueue_mapped_routes(self, mappings: list[BucketModelMapping]) -> int:
        added = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id,source_bucket,object_key,models_json,state FROM objects "
                "WHERE current=1 AND state IN ('classified','unmapped','routing') "
                "AND classification IN ('matched','awaiting_mapping')"
            ).fetchall()
            by_pair: dict[tuple[str, str], str] = {
                (item.source_bucket, item.model_name): item.target_bucket for item in mappings
            }
            now = current_timestamp()
            for row in rows:
                models = json.loads(row["models_json"])
                targets: dict[str, list[str]] = {}
                for model in models:
                    target = by_pair.get((row["source_bucket"], model))
                    if target:
                        targets.setdefault(target, []).append(model)
                for target, target_models in targets.items():
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO routing_tasks(object_id,source_bucket,object_key,target_bucket,model_names_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (row["id"], row["source_bucket"], row["object_key"], target, json.dumps(target_models), now, now),
                    )
                    added += cursor.rowcount
                if targets:
                    connection.execute("UPDATE objects SET state='routing', updated_at=? WHERE id=?", (now, row["id"]))
        return added

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            object_counts = connection.execute("SELECT state,COUNT(*) AS count FROM objects GROUP BY state").fetchall()
            route_counts = connection.execute("SELECT state,COUNT(*) AS count FROM routing_tasks GROUP BY state").fetchall()
        counts = {str(row["state"]): int(row["count"]) for row in object_counts}
        for row in route_counts:
            counts[f"route_{row['state']}"] = int(row["count"])
        return counts

    def list_backfill(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM backfill_state WHERE id=1").fetchone()
        return self._row_to_object(row) if row else {"status": "idle"}

    def update_backfill(self, **values: Any) -> None:
        self._update_backfill(values)

    def update_backfill_if_status(self, expected_status: str, **values: Any) -> bool:
        """Update backfill state only if the listing loop still owns its state."""
        return self._update_backfill(values, expected_status=expected_status)

    def _update_backfill(self, values: dict[str, Any], expected_status: str | None = None) -> bool:
        allowed = {
            "status",
            "source_bucket",
            "continuation_token",
            "total_seen",
            "total_classified",
            "listing_complete",
            "started_at",
            "error_code",
        }
        values = {key: value for key, value in values.items() if key in allowed}
        if not values:
            return False
        values["updated_at"] = current_timestamp()
        assignments = ",".join(f"{key}=?" for key in values)
        where = "WHERE id=1"
        parameters: list[Any] = list(values.values())
        if expected_status is not None:
            where += " AND status=?"
            parameters.append(expected_status)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE backfill_state SET {assignments} {where}", tuple(parameters)
            )
        return cursor.rowcount > 0

    def complete_backfill_if_drained(self) -> bool:
        """Atomically finish listing only after every current backfill object is classified."""
        now = current_timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT status,listing_complete FROM backfill_state WHERE id=1"
            ).fetchone()
            if not state or state["status"] != "draining" or not state["listing_complete"]:
                return False
            pending = connection.execute(
                """SELECT 1 FROM objects
                   WHERE origin='backfill' AND current=1
                     AND state IN ('queued','retry_wait','scanning')
                   LIMIT 1"""
            ).fetchone()
            if pending:
                return False
            cursor = connection.execute(
                "UPDATE backfill_state SET status='completed',updated_at=? WHERE id=1 AND status='draining'",
                (now,),
            )
            return cursor.rowcount > 0

    def release_claim(self, object_id: int) -> bool:
        """Return an interrupted claim to the durable queue without resetting attempts."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE objects SET state='queued', next_retry_at=NULL, updated_at=? WHERE id=? AND state='scanning'",
                (current_timestamp(), object_id),
            )
        return cursor.rowcount > 0

    def recover_inflight_claims(self) -> dict[str, int]:
        """Make claims abandoned by a process restart available again."""
        now = current_timestamp()
        retry_cutoff = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        with self._connect() as connection:
            object_cursor = connection.execute(
                """UPDATE objects
                   SET state=CASE WHEN attempts < ? THEN 'queued' ELSE 'retry_wait' END,
                       next_retry_at=CASE WHEN attempts < ? THEN NULL ELSE ? END,
                       updated_at=?
                   WHERE state='scanning'""",
                (self.max_attempts, self.max_attempts, retry_cutoff, now),
            )
            route_cursor = connection.execute(
                """UPDATE routing_tasks
                   SET state='pending', next_retry_at=NULL, updated_at=?
                   WHERE state='running'""",
                (now,),
            )
        return {"objects": object_cursor.rowcount, "routing_tasks": route_cursor.rowcount}

    def retry_failed(self, object_id: int | None = None) -> int:
        with self._connect() as connection:
            if object_id is None:
                cursor = connection.execute(
                    "UPDATE objects SET state='queued', attempts=0, next_retry_at=NULL, error_code=NULL, updated_at=? WHERE state='failed'",
                    (current_timestamp(),),
                )
            else:
                cursor = connection.execute(
                    "UPDATE objects SET state='queued', attempts=0, next_retry_at=NULL, error_code=NULL, updated_at=? WHERE id=? AND state='failed'",
                    (current_timestamp(), object_id),
                )
            return cursor.rowcount

    def metadata_reconciliation(self, source_bucket: str, objects: list[SourceObject]) -> int:
        """Merge one provider page in a single SQLite transaction."""
        self._validate_origin("reconcile")
        now = current_timestamp()
        created = 0
        with self._connect() as connection:
            for source_object in objects:
                _, was_created = self._upsert_object(
                    connection, source_bucket, source_object, now, "reconcile"
                )
                created += int(was_created)
        return created


class JobStorage:
    """Store user-facing JSON metadata and own the durable incremental SQLite store."""

    def __init__(self, data_directory: Path) -> None:
        self.jobs_directory = data_directory / "jobs"
        self.reports_directory = data_directory / "reports"
        self.jobs_directory.mkdir(parents=True, exist_ok=True)
        self.reports_directory.mkdir(parents=True, exist_ok=True)
        self.incremental = IncrementalStore(data_directory / "incremental.sqlite3")

    def _write_json_atomically(self, destination: Path, value: object) -> None:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, delete=False) as temporary_file:
            json.dump(value, temporary_file, sort_keys=True)
            temporary_file.write("\n")
            temporary_path = Path(temporary_file.name)
        temporary_path.replace(destination)

    def save_job(self, job: JobRecord) -> None:
        self._write_json_atomically(self.jobs_directory / f"{job['job_id']}.json", job)

    def get_job(self, job_id: str) -> JobRecord | None:
        job_path = self.jobs_directory / f"{job_id}.json"
        if not job_path.is_file():
            return None
        with job_path.open(encoding="utf-8") as job_file:
            loaded_job = json.load(job_file)
        if not isinstance(loaded_job, dict):
            raise ValueError("Stored job metadata is not an object")
        return cast(JobRecord, loaded_job)

    def get_latest_job(self, job_type: str | None = None) -> JobRecord | None:
        latest_job: JobRecord | None = None
        for job_path in self.jobs_directory.glob("*.json"):
            with job_path.open(encoding="utf-8") as job_file:
                loaded_job = json.load(job_file)
            if not isinstance(loaded_job, dict):
                continue
            candidate_job = cast(JobRecord, loaded_job)
            if job_type is not None and candidate_job.get("job_type", "scan") != job_type:
                continue
            if latest_job is None or candidate_job["updated_at"] > latest_job["updated_at"]:
                latest_job = candidate_job
        return latest_job

    def save_report(self, job_id: str, report: ScanReport) -> None:
        self._write_json_atomically(self.reports_directory / f"{job_id}.json", report)

    def get_report(self, job_id: str) -> ScanReport | None:
        report_path = self.reports_directory / f"{job_id}.json"
        if not report_path.is_file():
            return None
        with report_path.open(encoding="utf-8") as report_file:
            loaded_report = json.load(report_file)
        if not isinstance(loaded_report, dict):
            raise ValueError("Stored scan report is not an object")
        _normalize_legacy_scan_report(loaded_report)
        return cast(ScanReport, loaded_report)

    @property
    def mappings_path(self) -> Path:
        return self.jobs_directory.parent / "mappings.json"

    def save_mappings(self, mappings: list[BucketModelMapping]) -> None:
        self._write_json_atomically(self.mappings_path, [mapping.model_dump() for mapping in mappings])

    def get_mappings(self) -> list[BucketModelMapping]:
        if not self.mappings_path.is_file():
            return []
        with self.mappings_path.open(encoding="utf-8") as mappings_file:
            loaded_mappings = json.load(mappings_file)
        if not isinstance(loaded_mappings, list):
            raise ValueError("Stored mappings are not a list")
        return [BucketModelMapping.model_validate(item) for item in loaded_mappings]

    def save_sync_report(self, sync_job_id: str, report: SyncReport) -> None:
        self._write_json_atomically(self.reports_directory / f"sync-{sync_job_id}.json", report)

    def get_sync_report(self, sync_job_id: str) -> SyncReport | None:
        report_path = self.reports_directory / f"sync-{sync_job_id}.json"
        if not report_path.is_file():
            return None
        with report_path.open(encoding="utf-8") as report_file:
            loaded_report = json.load(report_file)
        if not isinstance(loaded_report, dict):
            raise ValueError("Stored sync report is not an object")
        return cast(SyncReport, loaded_report)

    def save_connection(self, endpoint: str, source_buckets: tuple[str, ...]) -> None:
        """Legacy compatibility shim; new state is stored as per-bucket profiles."""
        self.save_connection_profiles(tuple(SourceConnectionProfile(bucket, endpoint, "default") for bucket in source_buckets))

    def get_connection(self) -> tuple[str, tuple[str, ...]] | None:
        """Read the legacy aggregate connection shape during migration.

        New saves use ``connection_profiles``.  When all profiles share an
        endpoint, exposing the aggregate shape keeps older callers/fakes
        compatible; profiles with different endpoints cannot be represented by
        this legacy view and therefore return ``None``.
        """
        endpoint = self.incremental.get_state("endpoint")
        raw_buckets = self.incremental.get_state("source_buckets")
        if endpoint and raw_buckets:
            try:
                buckets = tuple(str(item) for item in json.loads(raw_buckets))
            except (TypeError, ValueError):
                return None
            return endpoint, buckets
        raw_profiles = self.incremental.get_state("connection_profiles")
        if not raw_profiles:
            return None
        try:
            loaded = json.loads(raw_profiles)
            profiles = [item for item in loaded if isinstance(item, dict)]
        except (TypeError, ValueError):
            return None
        endpoints = {str(item.get("endpoint", "")) for item in profiles}
        buckets = tuple(str(item.get("source_bucket", "")) for item in profiles)
        if len(endpoints) != 1 or not profiles or not all(endpoints) or not all(buckets):
            return None
        return next(iter(endpoints)), buckets
    def save_connection_profiles(self, profiles: tuple[SourceConnectionProfile, ...]) -> None:
        payload = [
            {
                "source_bucket": profile.source_bucket,
                "endpoint": profile.endpoint,
                "credential_ref": profile.credential_ref,
            }
            for profile in profiles
        ]
        self.incremental.set_state("connection_profiles", json.dumps(payload, sort_keys=True))
        # Remove the legacy aggregate keys after a successful migration/save.
        self.incremental.delete_state("endpoint")
        self.incremental.delete_state("source_buckets")
    def get_connection_profiles(self) -> tuple[SourceConnectionProfile, ...]:
        raw_profiles = self.incremental.get_state("connection_profiles")
        if raw_profiles:
            try:
                loaded = json.loads(raw_profiles)
                profiles = tuple(
                    SourceConnectionProfile(
                        source_bucket=str(item["source_bucket"]),
                        endpoint=str(item["endpoint"]),
                        credential_ref=str(item.get("credential_ref", "default")),
                    )
                    for item in loaded
                    if isinstance(item, dict)
                )
                if profiles:
                    return profiles
            except (TypeError, ValueError, KeyError):
                pass
        legacy_connection = self.get_connection()
        if legacy_connection is None:
            return ()
        endpoint, buckets = legacy_connection
        profiles = tuple(SourceConnectionProfile(bucket, endpoint, "default") for bucket in buckets)
        self.save_connection_profiles(profiles)
        return profiles

    def delete_state(self, key: str) -> None:
        self.incremental.delete_state(key)



    def mark_active_jobs_interrupted(self) -> None:
        for job_path in self.jobs_directory.glob("*.json"):
            with job_path.open(encoding="utf-8") as job_file:
                loaded_job = json.load(job_file)
            if not isinstance(loaded_job, dict) or loaded_job.get("status") not in {"queued", "running"}:
                continue
            loaded_job["status"] = "interrupted"
            loaded_job["error"] = "The sync was interrupted by a service restart" if loaded_job.get("job_type", "scan") == "sync" else "The scan was interrupted by a service restart"
            loaded_job["updated_at"] = current_timestamp()
            self._write_json_atomically(job_path, loaded_job)
