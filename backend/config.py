"""Application configuration loaded from the process environment."""

from dataclasses import dataclass
import os
from pathlib import Path


def parse_boolean_environment_value(value: str, variable_name: str) -> bool:
    normalized_value = value.strip().lower()
    if normalized_value in {"1", "true", "yes", "on"}:
        return True
    if normalized_value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{variable_name} must be a boolean value")


@dataclass(frozen=True)
class AppSettings:
    """Non-secret runtime settings used by the application."""

    admin_password: str
    data_directory: Path
    cookie_secure: bool
    session_max_age_seconds: int = 60 * 60 * 12
    object_timeout_seconds: int = 120
    object_retry_count: int = 2

    @classmethod
    def from_environment(cls) -> "AppSettings":
        admin_password = os.environ.get("ADMIN_PASSWORD", "")
        if not admin_password:
            raise RuntimeError("ADMIN_PASSWORD must be set")

        data_directory = Path(os.environ.get("DATA_DIRECTORY", "./data"))
        cookie_secure = parse_boolean_environment_value(
            os.environ.get("COOKIE_SECURE", "false"),
            "COOKIE_SECURE",
        )
        return cls(
            admin_password=admin_password,
            data_directory=data_directory,
            cookie_secure=cookie_secure,
        )
