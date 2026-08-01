"""Small, explicit runtime configuration for the local datasource service."""

from __future__ import annotations

import os
import secrets
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigurationError(ValueError):
    """Raised when operational configuration is unsafe or incomplete."""


@dataclass(frozen=True)
class AppConfig:
    """Validated paths and instance identity used by CLI and daemon code."""

    data_dir: Path
    backup_dir: Path | None
    environment: str
    instance_id: str
    operator_role: str = "admin"
    cursor_secret_file: Path | None = None
    timezone: str = "Europe/London"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "operational.sqlite3"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"


def load_config(
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
    environment: str | None = None,
    instance_id: str | None = None,
) -> AppConfig:
    """Load config with CLI overrides, then environment, file, and safe defaults."""

    selected_path = config_path or _environment_path("CRE_CONFIG")
    values = _read_toml(selected_path) if selected_path else {}
    runtime = values.get("runtime", values)
    if not isinstance(runtime, dict):
        raise ConfigurationError("runtime configuration must be a TOML table")

    selected_environment = environment or _string_value(
        os.environ.get("CRE_ENVIRONMENT") or runtime.get("environment") or "development",
        "environment",
    )
    selected_data_dir = data_dir or _environment_path("CRE_DATA_DIR") or _path_value(
        runtime.get("data_dir"), "data_dir", required=False
    )
    selected_backup_dir = _environment_path("CRE_BACKUP_DIR") or _path_value(
        runtime.get("backup_dir"), "backup_dir", required=False
    )
    selected_instance_id = instance_id or _string_value(
        os.environ.get("CRE_INSTANCE_ID") or runtime.get("instance_id") or "local",
        "instance_id",
    )
    selected_cursor_secret_file = (
        _environment_path("CRE_CURSOR_SECRET_FILE")
        or _path_value(runtime.get("cursor_secret_file"), "cursor_secret_file", required=False)
    )
    timezone = _string_value(runtime.get("timezone") or "Europe/London", "timezone")
    _validate_timezone(timezone)

    if selected_environment not in {"development", "test", "production"}:
        raise ConfigurationError("environment must be development, test, or production")
    if selected_data_dir is None:
        if selected_environment == "production":
            raise ConfigurationError("production requires an explicit data_dir")
        selected_data_dir = Path("data")
    if selected_environment == "production" and selected_backup_dir is None:
        raise ConfigurationError("production requires an explicit backup_dir")
    if selected_environment == "production" and selected_cursor_secret_file is None:
        raise ConfigurationError("production requires an explicit cursor_secret_file")
    configured_operator_role = runtime.get("operator_role")
    if configured_operator_role is None:
        if selected_environment == "production":
            raise ConfigurationError("production requires an explicit operator_role")
        selected_operator_role = "admin"
    else:
        selected_operator_role = _string_value(
            configured_operator_role, "operator_role"
        )
    if selected_operator_role not in {"read", "write", "admin"}:
        raise ConfigurationError("operator_role must be read, write, or admin")
    if selected_cursor_secret_file is not None:
        require_private_file(selected_cursor_secret_file)

    return AppConfig(
        data_dir=selected_data_dir,
        backup_dir=selected_backup_dir,
        environment=selected_environment,
        instance_id=selected_instance_id,
        operator_role=selected_operator_role,
        cursor_secret_file=selected_cursor_secret_file,
        timezone=timezone,
    )


def load_cursor_secret(config: AppConfig) -> bytes:
    """Read a private cursor-HMAC key without exposing it in CLI output.

    Production needs an operator-provisioned file. Development and test use a
    generated data-directory secret so cursors remain unforgeable across a
    normal CLI restart without deriving their key from a public identifier.
    """

    path = config.cursor_secret_file or config.data_dir / "cursor-hmac.secret"
    if not path.exists():
        if config.environment == "production":
            raise ConfigurationError("production cursor_secret_file does not exist")
        _create_private_secret(path)
    require_private_file(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ConfigurationError("cursor_secret_file must be a regular file")
        secret = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    if not 32 <= len(secret) <= 4096:
        raise ConfigurationError("cursor_secret_file must contain 32 to 4096 bytes")
    return secret


def require_private_file(path: Path) -> None:
    """Reject readable-by-group/world config and secret files."""

    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError as error:
        raise ConfigurationError(f"configuration file does not exist: {path}") from error
    if mode & 0o077:
        raise ConfigurationError(f"configuration file must be private (mode 0600): {path}")


def _create_private_secret(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return
    try:
        payload = secrets.token_bytes(32)
        total = 0
        while total < len(payload):
            total += os.write(descriptor, payload[total:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_toml(path: Path) -> dict[str, object]:
    require_private_file(path)
    try:
        with path.open("rb") as file:
            value = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"invalid TOML configuration: {path}") from error
    if not isinstance(value, dict):
        raise ConfigurationError("configuration root must be a TOML table")
    return value


def _environment_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _path_value(value: object, name: str, *, required: bool = True) -> Path | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{name} must be a non-empty path")
    return Path(value)


def _string_value(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"{name} must be a non-empty string")
    return value


def _validate_timezone(name: str) -> None:
    try:
        ZoneInfo(name)
    except Exception as error:  # ZoneInfoNotFoundError differs across Python versions.
        raise ConfigurationError(f"unknown timezone: {name!r}") from error
