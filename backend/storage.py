"""Credential-free durable storage for scan jobs and reports."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from typing import TypedDict, cast

from .models import BucketModelMapping, ScanReport, SyncReport


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


def current_timestamp() -> str:
    """Return an unambiguous UTC timestamp for durable metadata."""

    return datetime.now(timezone.utc).isoformat()


class JobStorage:
    """Store only non-secret scan metadata under the configured data directory."""

    def __init__(self, data_directory: Path) -> None:
        self.jobs_directory = data_directory / "jobs"
        self.reports_directory = data_directory / "reports"
        self.jobs_directory.mkdir(parents=True, exist_ok=True)
        self.reports_directory.mkdir(parents=True, exist_ok=True)

    def _write_json_atomically(self, destination: Path, value: object) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent, delete=False
        ) as temporary_file:
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
        """Return the most recently updated credential-free job record."""

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
        return cast(ScanReport, loaded_report)

    @property
    def mappings_path(self) -> Path:
        return self.jobs_directory.parent / "mappings.json"

    def save_mappings(self, mappings: list[BucketModelMapping]) -> None:
        self._write_json_atomically(
            self.mappings_path,
            [mapping.model_dump() for mapping in mappings],
        )

    def get_mappings(self) -> list[BucketModelMapping]:
        if not self.mappings_path.is_file():
            return []
        with self.mappings_path.open(encoding="utf-8") as mappings_file:
            loaded_mappings = json.load(mappings_file)
        if not isinstance(loaded_mappings, list):
            raise ValueError("Stored mappings are not a list")
        return [BucketModelMapping.model_validate(item) for item in loaded_mappings]

    def save_sync_report(self, sync_job_id: str, report: SyncReport) -> None:
        self._write_json_atomically(
            self.reports_directory / f"sync-{sync_job_id}.json",
            report,
        )

    def get_sync_report(self, sync_job_id: str) -> SyncReport | None:
        report_path = self.reports_directory / f"sync-{sync_job_id}.json"
        if not report_path.is_file():
            return None
        with report_path.open(encoding="utf-8") as report_file:
            loaded_report = json.load(report_file)
        if not isinstance(loaded_report, dict):
            raise ValueError("Stored sync report is not an object")
        return cast(SyncReport, loaded_report)

    def mark_active_jobs_interrupted(self) -> None:
        for job_path in self.jobs_directory.glob("*.json"):
            with job_path.open(encoding="utf-8") as job_file:
                loaded_job = json.load(job_file)
            if not isinstance(loaded_job, dict):
                continue
            if loaded_job.get("status") not in {"queued", "running"}:
                continue
            loaded_job["status"] = "interrupted"
            loaded_job["error"] = (
                "The sync was interrupted by a service restart"
                if loaded_job.get("job_type", "scan") == "sync"
                else "The scan was interrupted by a service restart"
            )
            loaded_job["updated_at"] = current_timestamp()
            self._write_json_atomically(job_path, loaded_job)
