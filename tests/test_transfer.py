import io
from pathlib import Path
import signal
import subprocess
import threading

import pytest

from backend.config import (
    AppSettings,
    MAX_RCLONE_LOW_LEVEL_RETRIES,
    MAX_RCLONE_OUTPUT_LIMIT_BYTES,
    MAX_RCLONE_TRANSFER_TIMEOUT_SECONDS,
)
from backend.models import ConnectionSettings, SourceObject
from backend.r2_client import Boto3R2Client
from backend.transfer import (
    R2_COPY_MAX_BYTES,
    RcloneTransferAdapter,
    TransferBinaryMissingError,
    TransferCancelledError,
    TransferError,
    TransferOutcome,
    TransferRequest,
    TransferTimeoutError,
    TransferVerificationError,
    UnsupportedTransferTopologyError,
)


class CompletedProcess:
    def __init__(self, output: bytes, return_code: int = 0) -> None:
        self.stdout = io.BytesIO(output)
        self.return_code = return_code
        self.pid = 12345

    def wait(self, timeout: float | None = None) -> int:
        return self.return_code


class FakeS3Client:
    def __init__(self, target_size: int = 42) -> None:
        self.target_size = target_size
        self.copy_calls: list[dict[str, object]] = []
        self.head_calls: list[tuple[str, str]] = []

    def copy_object(self, **parameters) -> None:
        self.copy_calls.append(parameters)

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.head_calls.append((Bucket, Key))
        return {"ContentLength": self.target_size, "ETag": "target-etag"}


class StubAdapter:
    requires_target_verification = True

    def __init__(self, outcome: TransferOutcome) -> None:
        self.outcome = outcome

    def transfer_object(self, request, cancellation_event=None) -> TransferOutcome:
        return self.outcome


def make_adapter(**overrides) -> RcloneTransferAdapter:
    arguments = {
        "binary_path": "/opt/rclone/rclone",
        "endpoint": "https://account-id.r2.cloudflarestorage.com",
        "access_key_id": "access-secret",
        "secret_access_key": "private-secret",
        "source_bucket": "source",
        "timeout_seconds": 30,
        "output_limit_bytes": 1024,
        "low_level_retries": 3,
    }
    arguments.update(overrides)
    return RcloneTransferAdapter(**arguments)


def make_request(size: int = 42) -> TransferRequest:
    return TransferRequest("source", "path/file name.tar.gz", "target", "path/file name.tar.gz", size)


def test_transfer_config_defaults_to_boto3_and_validates_rclone_path(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "password")
    monkeypatch.setenv("DATA_DIRECTORY", str(tmp_path))
    monkeypatch.delenv("R2_TRANSFER_ADAPTER", raising=False)

    assert AppSettings.from_environment().transfer_adapter == "boto3"

    monkeypatch.setenv("R2_TRANSFER_ADAPTER", "rclone")
    monkeypatch.setenv("RCLONE_BINARY_PATH", "relative/rclone")
    with pytest.raises(RuntimeError, match="RCLONE_BINARY_PATH"):
        AppSettings.from_environment()


def test_rclone_uses_shell_free_copyto_and_child_only_credentials(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("RCLONE_CONFIG", "/tmp/user-rclone.conf")
    monkeypatch.setenv("RCLONE_CONFIG_PASS", "user-config-secret")

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return CompletedProcess(b'{"level":"info","msg":"Copied (new)"}\n')

    monkeypatch.setattr("backend.transfer.subprocess.Popen", fake_popen)
    adapter = make_adapter()

    outcome = adapter.transfer_object(make_request())

    assert outcome == TransferOutcome.COPIED
    command = captured["command"]
    assert command[:5] == [
        "/opt/rclone/rclone",
        "--config",
        "/dev/null",
        "copyto",
        "r2transfer:source/path/file name.tar.gz",
    ]
    assert command[5] == "r2transfer:target/path/file name.tar.gz"
    assert "--ignore-existing" in command
    assert "--s3-no-check-bucket" in command
    assert command[command.index("--stats") + 1] == "0"
    assert not {"sync", "move", "moveto", "delete", "mkdir"}.intersection(command)
    assert "access-secret" not in command
    assert "private-secret" not in command
    kwargs = captured["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["RCLONE_CONFIG_R2TRANSFER_ACCESS_KEY_ID"] == "access-secret"
    assert kwargs["env"]["RCLONE_CONFIG_R2TRANSFER_SECRET_ACCESS_KEY"] == "private-secret"
    assert "RCLONE_CONFIG" not in kwargs["env"]
    assert "RCLONE_CONFIG_PASS" not in kwargs["env"]


def test_rclone_exit_zero_skip_is_not_classified_as_copied(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.transfer.subprocess.Popen",
        lambda command, **kwargs: CompletedProcess(
            b'{"level":"info","msg":"Skipped copy as --ignore-existing is set"}\n'
        ),
    )

    assert make_adapter().transfer_object(make_request()) == TransferOutcome.SKIPPED


def test_rclone_caps_output_and_never_exposes_failed_output(monkeypatch) -> None:
    adapter = make_adapter(output_limit_bytes=1024)
    captured_output: list[bytes] = []
    monkeypatch.setattr(
        "backend.transfer.subprocess.Popen",
        lambda command, **kwargs: CompletedProcess(b"x" * 4096),
    )
    monkeypatch.setattr(
        adapter,
        "_classify_output",
        lambda output: captured_output.append(output) or TransferOutcome.SKIPPED,
    )

    assert adapter.transfer_object(make_request()) == TransferOutcome.SKIPPED
    assert len(captured_output[0]) == 1024

    monkeypatch.setattr(
        "backend.transfer.subprocess.Popen",
        lambda command, **kwargs: CompletedProcess(b"private-secret\nprovider detail", 1),
    )
    with pytest.raises(TransferError) as error:
        adapter.transfer_object(make_request())
    assert "private-secret" not in str(error.value)
    assert "provider detail" not in str(error.value)


def test_rclone_missing_binary_is_normalized_without_command_or_secret(monkeypatch) -> None:
    def missing_binary(command, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("backend.transfer.subprocess.Popen", missing_binary)

    with pytest.raises(TransferBinaryMissingError, match="unavailable") as error:
        make_adapter().transfer_object(make_request())

    assert "private-secret" not in str(error.value)
    assert "path/file" not in str(error.value)

    monkeypatch.setattr(
        "backend.transfer.subprocess.Popen",
        lambda command, **kwargs: (_ for _ in ()).throw(PermissionError()),
    )
    with pytest.raises(TransferBinaryMissingError, match="unavailable"):
        make_adapter().transfer_object(make_request())


def test_rclone_timeout_terminates_process_group(monkeypatch) -> None:
    process = CompletedProcess(b"")

    def wait_forever(timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("rclone", timeout)

    process.wait = wait_forever
    stopped: list[CompletedProcess] = []
    monotonic_values = iter((0.0, 2.0))
    monkeypatch.setattr("backend.transfer.subprocess.Popen", lambda command, **kwargs: process)
    monkeypatch.setattr("backend.transfer.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        RcloneTransferAdapter,
        "_stop_process",
        staticmethod(lambda child: stopped.append(child)),
    )

    with pytest.raises(TransferTimeoutError):
        make_adapter(timeout_seconds=1).transfer_object(make_request())

    assert stopped == [process]


def test_rclone_cancellation_terminates_process_group(monkeypatch) -> None:
    cancellation_event = threading.Event()
    process = CompletedProcess(b"")

    def wait_then_cancel(timeout: float | None = None) -> int:
        cancellation_event.set()
        raise subprocess.TimeoutExpired("rclone", timeout)

    process.wait = wait_then_cancel
    stopped: list[CompletedProcess] = []
    monkeypatch.setattr("backend.transfer.subprocess.Popen", lambda command, **kwargs: process)
    monkeypatch.setattr(
        RcloneTransferAdapter,
        "_stop_process",
        staticmethod(lambda child: stopped.append(child)),
    )

    with pytest.raises(TransferCancelledError):
        make_adapter().transfer_object(make_request(), cancellation_event)

    assert stopped == [process]


def test_rclone_stop_process_escalates_to_process_group_kill(monkeypatch) -> None:
    signals: list[int] = []

    class StubbornProcess:
        pid = 321

        def __init__(self) -> None:
            self.wait_calls = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired("redacted", timeout)
            return 0

        def terminate(self) -> None:
            raise AssertionError("process-group signaling should be used")

        def kill(self) -> None:
            raise AssertionError("process-group signaling should be used")

    monkeypatch.setattr(
        "backend.transfer.os.killpg", lambda pid, sent_signal: signals.append(sent_signal)
    )
    process = StubbornProcess()

    RcloneTransferAdapter._stop_process(process)

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.wait_calls == 2


def test_rclone_fails_closed_for_non_r2_endpoint_and_unsupported_routes(monkeypatch) -> None:
    with pytest.raises(UnsupportedTransferTopologyError):
        make_adapter(endpoint="https://s3.example.com")

    monkeypatch.setattr(
        "backend.transfer.subprocess.Popen",
        lambda command, **kwargs: pytest.fail("unsupported copy must not start rclone"),
    )
    with pytest.raises(UnsupportedTransferTopologyError):
        make_adapter().transfer_object(make_request(R2_COPY_MAX_BYTES + 1))
    with pytest.raises(UnsupportedTransferTopologyError):
        make_adapter(source_bucket="other").transfer_object(make_request())
    with pytest.raises(UnsupportedTransferTopologyError):
        make_adapter().transfer_object(
            TransferRequest("source", "item", "Invalid Bucket", "item", 1)
        )


def test_rclone_environment_limits_and_executable_readiness(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "password")
    monkeypatch.setenv("DATA_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("R2_TRANSFER_ADAPTER", "rclone")
    binary = tmp_path / "rclone"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("RCLONE_BINARY_PATH", str(binary))

    settings = AppSettings.from_environment()
    assert settings.transfer_adapter_ready is False
    binary.chmod(0o700)
    ready_settings = AppSettings.from_environment()
    assert ready_settings.transfer_adapter_ready is True
    assert ready_settings.transfer_topology_ready(
        ("https://account-id.r2.cloudflarestorage.com",)
    ) is True
    assert ready_settings.transfer_topology_ready(("https://s3.example.com",)) is False

    invalid_limits = [
        ("RCLONE_TRANSFER_TIMEOUT_SECONDS", 0),
        ("RCLONE_TRANSFER_TIMEOUT_SECONDS", MAX_RCLONE_TRANSFER_TIMEOUT_SECONDS + 1),
        ("RCLONE_OUTPUT_LIMIT_BYTES", 1023),
        ("RCLONE_OUTPUT_LIMIT_BYTES", MAX_RCLONE_OUTPUT_LIMIT_BYTES + 1),
        ("RCLONE_LOW_LEVEL_RETRIES", -1),
        ("RCLONE_LOW_LEVEL_RETRIES", MAX_RCLONE_LOW_LEVEL_RETRIES + 1),
    ]
    for variable_name, invalid_value in invalid_limits:
        monkeypatch.setenv(variable_name, str(invalid_value))
        with pytest.raises(RuntimeError, match=variable_name):
            AppSettings.from_environment()
        monkeypatch.delenv(variable_name)

    monkeypatch.setenv("R2_TRANSFER_ADAPTER", "rsync")
    with pytest.raises(RuntimeError, match="R2_TRANSFER_ADAPTER"):
        AppSettings.from_environment()


def test_boto3_remains_default_and_does_not_add_post_copy_head(monkeypatch) -> None:
    s3_client = FakeS3Client()
    monkeypatch.setattr("backend.r2_client.boto3.client", lambda *args, **kwargs: s3_client)
    client = Boto3R2Client(
        ConnectionSettings(
            endpoint="https://account-id.r2.cloudflarestorage.com",
            access_key_id="access",
            secret_access_key="secret",
            source_bucket="source",
        )
    )

    legacy_result = client.copy_object("source", "item", "target", "item")
    outcome = client.transfer_object(
        TransferRequest("source", "item", "target", "item", 42)
    )

    assert legacy_result is None
    assert outcome == TransferOutcome.COPIED
    assert s3_client.copy_calls == [
        {"Bucket": "target", "Key": "item", "CopySource": {"Bucket": "source", "Key": "item"}},
        {"Bucket": "target", "Key": "item", "CopySource": {"Bucket": "source", "Key": "item"}},
    ]
    assert s3_client.head_calls == []


def test_rclone_result_requires_target_head_size_verification(monkeypatch) -> None:
    s3_client = FakeS3Client(target_size=41)
    monkeypatch.setattr("backend.r2_client.boto3.client", lambda *args, **kwargs: s3_client)
    client = Boto3R2Client(
        ConnectionSettings(
            endpoint="https://account-id.r2.cloudflarestorage.com",
            access_key_id="access",
            secret_access_key="secret",
            source_bucket="source",
        )
    )
    client._transfer_adapter = StubAdapter(TransferOutcome.SKIPPED)

    with pytest.raises(TransferVerificationError):
        client.transfer_object(
            TransferRequest("source", "item", "target", "item", 42)
        )

    assert s3_client.head_calls == [("target", "item")]
