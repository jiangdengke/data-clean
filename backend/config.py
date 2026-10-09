"""Application configuration loaded from deployment environment."""

from dataclasses import dataclass
import os
from pathlib import Path
import re

from .transfer import (
    MAX_RCLONE_LOW_LEVEL_RETRIES,
    MAX_RCLONE_OUTPUT_LIMIT_BYTES,
    MAX_RCLONE_TRANSFER_TIMEOUT_SECONDS,
    MIN_RCLONE_OUTPUT_LIMIT_BYTES,
    is_supported_r2_endpoint,
)


def parse_boolean_environment_value(value: str, variable_name: str) -> bool:
    normalized_value = value.strip().lower()
    if normalized_value in {"1", "true", "yes", "on"}:
        return True
    if normalized_value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{variable_name} must be a boolean value")


def parse_integer_environment_value(
    value: str,
    variable_name: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError(f"{variable_name} must be an integer") from error
    if parsed < minimum:
        raise RuntimeError(f"{variable_name} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise RuntimeError(f"{variable_name} must be at most {maximum}")
    return parsed


def parse_choice_environment_value(
    value: str,
    variable_name: str,
    allowed_values: frozenset[str],
) -> str:
    normalized_value = value.strip().lower()
    if normalized_value not in allowed_values:
        choices = ", ".join(sorted(allowed_values))
        raise RuntimeError(f"{variable_name} must be one of: {choices}")
    return normalized_value


@dataclass(frozen=True)
class AppSettings:
    """Non-secret settings plus credentials injected for the process lifetime.

    The secret fields deliberately never leave this object and are not accepted by
    the connection API.  ``AppSettings`` remains constructible with the original
    arguments so existing unit tests and manual workflows remain compatible.
    """

    admin_password: str
    data_directory: Path
    cookie_secure: bool
    session_max_age_seconds: int = 60 * 60 * 12
    object_timeout_seconds: int = 120
    object_retry_count: int = 2
    r2_endpoint: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_credential_refs: tuple[tuple[str, str, str], ...] = ()
    cloudflare_account_id: str = ""
    cloudflare_queue_id: str = ""
    cloudflare_api_token: str = ""
    queue_visibility_timeout_ms: int = 60_000
    queue_batch_size: int = 10
    queue_poll_interval_seconds: int = 2
    queue_idle_poll_interval_seconds: int = 30
    reconcile_interval_seconds: int = 3600
    worker_concurrency: int = 2
    backfill_page_size: int = 1000
    transfer_adapter: str = "boto3"
    rclone_binary_path: str = "/usr/local/bin/rclone"
    rclone_transfer_timeout_seconds: int = 3600
    rclone_output_limit_bytes: int = 64 * 1024
    rclone_low_level_retries: int = 3

    @classmethod
    def from_environment(cls) -> "AppSettings":
        admin_password = os.environ.get("ADMIN_PASSWORD", "")
        if not admin_password:
            raise RuntimeError("ADMIN_PASSWORD must be set")

        data_directory = Path(os.environ.get("DATA_DIRECTORY", "./data"))
        cookie_secure = parse_boolean_environment_value(
            os.environ.get("COOKIE_SECURE", "false"), "COOKIE_SECURE"
        )
        transfer_adapter = parse_choice_environment_value(
            os.environ.get("R2_TRANSFER_ADAPTER", "boto3"),
            "R2_TRANSFER_ADAPTER",
            frozenset({"boto3", "rclone"}),
        )
        rclone_binary_path = os.environ.get(
            "RCLONE_BINARY_PATH", "/usr/local/bin/rclone"
        ).strip()
        if transfer_adapter == "rclone" and not Path(rclone_binary_path).is_absolute():
            raise RuntimeError("RCLONE_BINARY_PATH must be absolute in rclone mode")
        return cls(
            admin_password=admin_password,
            data_directory=data_directory,
            cookie_secure=cookie_secure,
            r2_endpoint=os.environ.get("R2_ENDPOINT", "").strip(),
            r2_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", "").strip(),
            r2_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", "").strip(),
            cloudflare_account_id=os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip(),
            cloudflare_queue_id=os.environ.get("CLOUDFLARE_QUEUE_ID", "").strip(),
            cloudflare_api_token=os.environ.get(
                "CLOUDFLARE_API_TOKEN", os.environ.get("QUEUE_API_TOKEN", "")
            ).strip(),
            queue_visibility_timeout_ms=parse_integer_environment_value(
                os.environ.get("QUEUE_VISIBILITY_TIMEOUT_MS", "60000"),
                "QUEUE_VISIBILITY_TIMEOUT_MS",
                1,
            ),
            queue_batch_size=parse_integer_environment_value(
                os.environ.get("QUEUE_BATCH_SIZE", "10"), "QUEUE_BATCH_SIZE", 1
            ),
            queue_poll_interval_seconds=parse_integer_environment_value(
                os.environ.get("QUEUE_POLL_INTERVAL_SECONDS", "2"),
                "QUEUE_POLL_INTERVAL_SECONDS",
                1,
            ),
            queue_idle_poll_interval_seconds=parse_integer_environment_value(
                os.environ.get("QUEUE_IDLE_POLL_INTERVAL_SECONDS", "30"),
                "QUEUE_IDLE_POLL_INTERVAL_SECONDS",
                1,
            ),
            reconcile_interval_seconds=parse_integer_environment_value(
                os.environ.get("RECONCILE_INTERVAL_SECONDS", "3600"),
                "RECONCILE_INTERVAL_SECONDS",
                1,
            ),
            worker_concurrency=parse_integer_environment_value(
                os.environ.get("INCREMENTAL_WORKER_CONCURRENCY", "2"),
                "INCREMENTAL_WORKER_CONCURRENCY",
                1,
            ),
            backfill_page_size=parse_integer_environment_value(
                os.environ.get("BACKFILL_PAGE_SIZE", "1000"), "BACKFILL_PAGE_SIZE", 1
            ),
            transfer_adapter=transfer_adapter,
            rclone_binary_path=rclone_binary_path,
            rclone_transfer_timeout_seconds=parse_integer_environment_value(
                os.environ.get("RCLONE_TRANSFER_TIMEOUT_SECONDS", "3600"),
                "RCLONE_TRANSFER_TIMEOUT_SECONDS",
                1,
                MAX_RCLONE_TRANSFER_TIMEOUT_SECONDS,
            ),
            rclone_output_limit_bytes=parse_integer_environment_value(
                os.environ.get("RCLONE_OUTPUT_LIMIT_BYTES", str(64 * 1024)),
                "RCLONE_OUTPUT_LIMIT_BYTES",
                MIN_RCLONE_OUTPUT_LIMIT_BYTES,
                MAX_RCLONE_OUTPUT_LIMIT_BYTES,
            ),
            rclone_low_level_retries=parse_integer_environment_value(
                os.environ.get("RCLONE_LOW_LEVEL_RETRIES", "3"),
                "RCLONE_LOW_LEVEL_RETRIES",
                0,
                MAX_RCLONE_LOW_LEVEL_RETRIES,
            ),
        )

    @staticmethod
    def normalize_credential_ref(credential_ref: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", credential_ref.strip()).strip("_").upper()
        if not normalized:
            raise ValueError("Credential reference must contain letters or digits")
        return normalized

    def resolve_r2_credentials(self, credential_ref: str) -> tuple[str, str]:
        """Resolve a non-secret reference from process environment only."""
        if credential_ref == "default":
            return self.r2_access_key_id, self.r2_secret_access_key
        normalized = self.normalize_credential_ref(credential_ref)
        access_key = os.environ.get(f"R2_CREDENTIAL_{normalized}_ACCESS_KEY_ID", "").strip()
        secret_key = os.environ.get(f"R2_CREDENTIAL_{normalized}_SECRET_ACCESS_KEY", "").strip()
        for ref, configured_access, configured_secret in self.r2_credential_refs:
            if ref == credential_ref:
                access_key = configured_access
                secret_key = configured_secret
                break
        return access_key, secret_key

    def credentials_ready_for(self, credential_ref: str) -> bool:
        try:
            access_key, secret_key = self.resolve_r2_credentials(credential_ref)
        except ValueError:
            return False
        return bool(access_key and secret_key)

    def bucket_runtime_ready(self, credential_refs: tuple[str, ...]) -> bool:
        return bool(credential_refs) and all(self.credentials_ready_for(ref) for ref in credential_refs)

    @property
    def transfer_adapter_ready(self) -> bool:
        if self.transfer_adapter == "boto3":
            return True
        binary_path = Path(self.rclone_binary_path)
        return binary_path.is_absolute() and binary_path.is_file() and os.access(binary_path, os.X_OK)

    @property
    def transfer_readiness_message(self) -> str:
        if self.transfer_adapter_ready:
            return "传输适配器已就绪"
        return "RCLONE_BINARY_PATH 必须指向可执行的 rclone 文件"

    def transfer_topology_ready(self, endpoints: tuple[str, ...]) -> bool:
        if self.transfer_adapter == "boto3":
            return True
        return bool(endpoints) and all(is_supported_r2_endpoint(endpoint) for endpoint in endpoints)

    def readiness_message_for(
        self,
        credential_refs: tuple[str, ...],
        endpoints: tuple[str, ...] = (),
    ) -> str:
        """Describe missing non-secret deployment prerequisites for saved profiles."""
        missing: list[str] = []
        if not self.transfer_adapter_ready:
            missing.append("RCLONE_BINARY_PATH")
        if endpoints and any(not endpoint.strip() for endpoint in endpoints):
            missing.append("R2_ENDPOINT")
        if not endpoints:
            missing.append("R2_ENDPOINT")
        elif not self.transfer_topology_ready(endpoints):
            missing.append("R2_ENDPOINT_RCLONE_TOPOLOGY")
        for credential_ref in dict.fromkeys(credential_refs):
            if self.credentials_ready_for(credential_ref):
                continue
            if credential_ref == "default":
                missing.extend(("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"))
            else:
                normalized = self.normalize_credential_ref(credential_ref)
                missing.extend(
                    (
                        f"R2_CREDENTIAL_{normalized}_ACCESS_KEY_ID",
                        f"R2_CREDENTIAL_{normalized}_SECRET_ACCESS_KEY",
                    )
                )
        if not self.cloudflare_account_id:
            missing.append("CLOUDFLARE_ACCOUNT_ID")
        if not self.cloudflare_queue_id:
            missing.append("CLOUDFLARE_QUEUE_ID")
        if not self.cloudflare_api_token:
            missing.append("CLOUDFLARE_API_TOKEN")
        return "运行凭据已就绪" if not missing else f"缺少部署环境配置：{', '.join(dict.fromkeys(missing))}"

    @property
    def r2_credentials_ready(self) -> bool:
        return bool(self.r2_endpoint and self.r2_access_key_id and self.r2_secret_access_key)

    @property
    def queue_credentials_ready(self) -> bool:
        return bool(
            self.cloudflare_account_id
            and self.cloudflare_queue_id
            and self.cloudflare_api_token
        )

    @property
    def runtime_ready(self) -> bool:
        return (
            self.r2_credentials_ready
            and self.queue_credentials_ready
            and self.transfer_adapter_ready
            and self.transfer_topology_ready((self.r2_endpoint,))
        )

    @property
    def readiness_message(self) -> str:
        missing: list[str] = []
        if not self.r2_endpoint:
            missing.append("R2_ENDPOINT")
        if not self.r2_access_key_id:
            missing.append("R2_ACCESS_KEY_ID")
        if not self.r2_secret_access_key:
            missing.append("R2_SECRET_ACCESS_KEY")
        if not self.cloudflare_account_id:
            missing.append("CLOUDFLARE_ACCOUNT_ID")
        if not self.cloudflare_queue_id:
            missing.append("CLOUDFLARE_QUEUE_ID")
        if not self.cloudflare_api_token:
            missing.append("CLOUDFLARE_API_TOKEN")
        if not self.transfer_adapter_ready:
            missing.append("RCLONE_BINARY_PATH")
        if self.r2_endpoint and not self.transfer_topology_ready((self.r2_endpoint,)):
            missing.append("R2_ENDPOINT_RCLONE_TOPOLOGY")
        return "运行凭据已就绪" if not missing else f"缺少部署环境配置：{', '.join(missing)}"
