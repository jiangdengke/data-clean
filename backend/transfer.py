"""Single-object transfer adapters for R2 copy operations."""

from dataclasses import dataclass
from enum import Enum
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from typing import Any, BinaryIO, Protocol
from urllib.parse import urlsplit


R2_COPY_MAX_BYTES = 5 * 1024**3
MAX_RCLONE_TRANSFER_TIMEOUT_SECONDS = 24 * 60 * 60
MAX_RCLONE_OUTPUT_LIMIT_BYTES = 1024 * 1024
MAX_RCLONE_LOW_LEVEL_RETRIES = 100
MIN_RCLONE_OUTPUT_LIMIT_BYTES = 1024
_RCLONE_REMOTE_NAME = "r2transfer"
_RCLONE_READ_CHUNK_BYTES = 8192
_RCLONE_STOP_GRACE_SECONDS = 2
_R2_ENDPOINT_HOST = re.compile(
    r"^[A-Za-z0-9-]+(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com$"
)
_R2_BUCKET_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")


def is_supported_r2_endpoint(endpoint_value: str) -> bool:
    endpoint = urlsplit(endpoint_value)
    return (
        endpoint.scheme == "https"
        and endpoint.hostname is not None
        and _R2_ENDPOINT_HOST.fullmatch(endpoint.hostname) is not None
        and endpoint.username is None
        and endpoint.password is None
        and endpoint.path in {"", "/"}
        and not endpoint.query
        and not endpoint.fragment
        and endpoint.port in {None, 443}
    )


@dataclass(frozen=True)
class TransferRequest:
    """One validated whole-object transfer within one R2 account."""

    source_bucket: str
    source_key: str
    target_bucket: str
    target_key: str
    expected_size: int


class TransferOutcome(str, Enum):
    COPIED = "copied"
    SKIPPED = "skipped"


class TransferError(RuntimeError):
    """A normalized transfer failure that never contains provider output."""


class TransferConfigurationError(TransferError):
    pass


class UnsupportedTransferTopologyError(TransferError):
    pass


class TransferBinaryMissingError(TransferError):
    pass


class TransferTimeoutError(TransferError):
    pass


class TransferCancelledError(TransferError):
    pass


class TransferVerificationError(TransferError):
    pass


class SourceChangedBeforeTransferError(TransferVerificationError):
    pass


class TargetVerificationError(TransferVerificationError):
    pass


class ObjectTransferAdapter(Protocol):
    requires_target_verification: bool

    def transfer_object(
        self,
        request: TransferRequest,
        cancellation_event: threading.Event | None = None,
    ) -> TransferOutcome: ...


class Boto3TransferAdapter:
    """Preserve the existing provider-side CopyObject implementation."""

    requires_target_verification = False

    def __init__(self, client: Any) -> None:
        self._client = client

    def transfer_object(
        self,
        request: TransferRequest,
        cancellation_event: threading.Event | None = None,
    ) -> TransferOutcome:
        if cancellation_event is not None and cancellation_event.is_set():
            raise TransferCancelledError("Object transfer was cancelled")
        self._client.copy_object(
            Bucket=request.target_bucket,
            Key=request.target_key,
            CopySource={"Bucket": request.source_bucket, "Key": request.source_key},
        )
        return TransferOutcome.COPIED


class _CappedOutput:
    """Drain a pipe fully while retaining only a bounded in-memory prefix."""

    def __init__(self, limit_bytes: int) -> None:
        self._limit_bytes = limit_bytes
        self._buffer = bytearray()

    def drain(self, stream: BinaryIO) -> None:
        while True:
            chunk = stream.read(_RCLONE_READ_CHUNK_BYTES)
            if not chunk:
                return
            remaining = self._limit_bytes - len(self._buffer)
            if remaining > 0:
                self._buffer.extend(chunk[:remaining])

    @property
    def value(self) -> bytes:
        return bytes(self._buffer)


class RcloneTransferAdapter:
    """Run one configless, same-account R2 server-side copy via rclone."""

    requires_target_verification = True

    def __init__(
        self,
        *,
        binary_path: str,
        endpoint: str,
        access_key_id: str,
        secret_access_key: str,
        source_bucket: str,
        timeout_seconds: int,
        output_limit_bytes: int,
        low_level_retries: int,
    ) -> None:
        self._binary_path = binary_path
        self._endpoint = endpoint
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._source_bucket = source_bucket
        self._timeout_seconds = timeout_seconds
        self._output_limit_bytes = output_limit_bytes
        self._low_level_retries = low_level_retries
        self._validate_configuration()

    def _validate_configuration(self) -> None:
        if not Path(self._binary_path).is_absolute():
            raise TransferConfigurationError("Rclone binary path must be absolute")
        if not self._access_key_id or not self._secret_access_key:
            raise TransferConfigurationError("Rclone credentials are not configured")
        if not 1 <= self._timeout_seconds <= MAX_RCLONE_TRANSFER_TIMEOUT_SECONDS:
            raise TransferConfigurationError("Rclone timeout is outside the supported range")
        if not (
            MIN_RCLONE_OUTPUT_LIMIT_BYTES
            <= self._output_limit_bytes
            <= MAX_RCLONE_OUTPUT_LIMIT_BYTES
        ):
            raise TransferConfigurationError("Rclone output limit is outside the supported range")
        if not 0 <= self._low_level_retries <= MAX_RCLONE_LOW_LEVEL_RETRIES:
            raise TransferConfigurationError("Rclone retry count is outside the supported range")

        if not is_supported_r2_endpoint(self._endpoint):
            raise UnsupportedTransferTopologyError(
                "Rclone transfer requires one Cloudflare R2 account endpoint"
            )

    def _validate_request(self, request: TransferRequest) -> None:
        if request.source_bucket != self._source_bucket:
            raise UnsupportedTransferTopologyError(
                "Rclone source bucket does not match its configured account profile"
            )
        if request.expected_size < 0 or request.expected_size > R2_COPY_MAX_BYTES:
            raise UnsupportedTransferTopologyError(
                "Rclone transfer requires an R2 server-side-copy-compatible object"
            )
        for bucket_name in (request.source_bucket, request.target_bucket):
            if _R2_BUCKET_NAME.fullmatch(bucket_name) is None:
                raise UnsupportedTransferTopologyError("Rclone bucket name is unsupported")
        if "\x00" in request.source_key or "\x00" in request.target_key:
            raise UnsupportedTransferTopologyError("Rclone object key is unsupported")

    def _child_environment(self) -> dict[str, str]:
        prefix = f"RCLONE_CONFIG_{_RCLONE_REMOTE_NAME.upper()}"
        return {
            f"{prefix}_TYPE": "s3",
            f"{prefix}_PROVIDER": "Cloudflare",
            f"{prefix}_ACCESS_KEY_ID": self._access_key_id,
            f"{prefix}_SECRET_ACCESS_KEY": self._secret_access_key,
            f"{prefix}_ENDPOINT": self._endpoint,
        }

    def _command(self, request: TransferRequest) -> list[str]:
        source = f"{_RCLONE_REMOTE_NAME}:{request.source_bucket}/{request.source_key}"
        target = f"{_RCLONE_REMOTE_NAME}:{request.target_bucket}/{request.target_key}"
        return [
            self._binary_path,
            "--config",
            "/dev/null",
            "copyto",
            source,
            target,
            "--ignore-existing",
            "--s3-no-check-bucket",
            "--retries",
            "1",
            "--low-level-retries",
            str(self._low_level_retries),
            "--stats",
            "0",
            "--use-json-log",
            "--log-level",
            "INFO",
        ]

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            process.terminate()
        try:
            process.wait(timeout=_RCLONE_STOP_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            process.kill()
        try:
            process.wait(timeout=_RCLONE_STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            raise TransferError("Rclone process could not be stopped") from None

    @staticmethod
    def _classify_output(output: bytes) -> TransferOutcome:
        copied = False
        skipped = False
        for raw_line in io.BytesIO(output):
            try:
                record = json.loads(raw_line.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            message = record.get("msg")
            if not isinstance(message, str):
                continue
            normalized = " ".join(message.split()).casefold()
            if "ignore-existing" in normalized or "skipped" in normalized:
                skipped = True
            if normalized.startswith("copied (") or normalized == "copied":
                copied = True
        if skipped or not copied:
            return TransferOutcome.SKIPPED
        return TransferOutcome.COPIED

    def transfer_object(
        self,
        request: TransferRequest,
        cancellation_event: threading.Event | None = None,
    ) -> TransferOutcome:
        self._validate_request(request)
        if cancellation_event is not None and cancellation_event.is_set():
            raise TransferCancelledError("Object transfer was cancelled")

        try:
            process = subprocess.Popen(
                self._command(request),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self._child_environment(),
                shell=False,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError):
            raise TransferBinaryMissingError("Rclone binary is unavailable") from None
        except OSError:
            raise TransferError("Rclone process could not be started") from None

        if process.stdout is None:
            self._stop_process(process)
            raise TransferError("Rclone output pipe is unavailable")
        output = _CappedOutput(self._output_limit_bytes)
        reader = threading.Thread(target=output.drain, args=(process.stdout,), daemon=True)
        reader.start()
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                self._stop_process(process)
                reader.join(timeout=_RCLONE_STOP_GRACE_SECONDS)
                raise TransferCancelledError("Object transfer was cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._stop_process(process)
                reader.join(timeout=_RCLONE_STOP_GRACE_SECONDS)
                raise TransferTimeoutError("Rclone object transfer timed out")
            try:
                return_code = process.wait(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        reader.join(timeout=_RCLONE_STOP_GRACE_SECONDS)
        if reader.is_alive():
            raise TransferError("Rclone output could not be drained")
        if return_code != 0:
            raise TransferError("Rclone object transfer failed")
        return self._classify_output(output.value)
