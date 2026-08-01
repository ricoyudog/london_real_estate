from __future__ import annotations

from pathlib import Path

import pytest

from nan_fung.config import ConfigurationError, load_config, load_cursor_secret


def test_development_uses_local_data_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRE_DATA_DIR", raising=False)
    monkeypatch.delenv("CRE_CONFIG", raising=False)

    config = load_config()

    assert config.data_dir == Path("data")
    assert config.database_path == Path("data/operational.sqlite3")
    assert config.timezone == "Europe/London"


def test_production_requires_explicit_operational_paths() -> None:
    with pytest.raises(ConfigurationError, match="data_dir"):
        load_config(environment="production")


def test_private_toml_config_is_loaded(tmp_path: Path) -> None:
    config_path = tmp_path / "cre.toml"
    cursor_secret = tmp_path / "cursor.secret"
    cursor_secret.write_bytes(b"x" * 32)
    cursor_secret.chmod(0o600)
    config_path.write_text(
        "[runtime]\ndata_dir = 'state'\nbackup_dir = 'backups'\n"
        f"cursor_secret_file = {str(cursor_secret)!r}\n"
        "environment = 'production'\ninstance_id = 'cre-a'\noperator_role = 'admin'\n",
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    config = load_config(config_path=config_path)

    assert config.environment == "production"
    assert config.data_dir == Path("state")
    assert config.backup_dir == Path("backups")
    assert config.operator_role == "admin"
    assert load_cursor_secret(config) == b"x" * 32


def test_production_requires_an_explicit_operator_role(tmp_path: Path) -> None:
    config_path = tmp_path / "cre.toml"
    cursor_secret = tmp_path / "cursor.secret"
    cursor_secret.write_bytes(b"x" * 32)
    cursor_secret.chmod(0o600)
    config_path.write_text(
        "[runtime]\ndata_dir = 'state'\nbackup_dir = 'backups'\n"
        f"cursor_secret_file = {str(cursor_secret)!r}\n"
        "environment = 'production'\n",
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    with pytest.raises(ConfigurationError, match="operator_role"):
        load_config(config_path=config_path)


def test_insecure_config_permissions_are_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "cre.toml"
    config_path.write_text("data_dir = 'state'\n", encoding="utf-8")
    config_path.chmod(0o644)

    with pytest.raises(ConfigurationError, match="private"):
        load_config(config_path=config_path)


def test_development_cursor_secret_is_private_and_not_derived_from_instance_id(
    tmp_path: Path,
) -> None:
    config = load_config(data_dir=tmp_path / "state", instance_id="public-name")

    first = load_cursor_secret(config)
    second = load_cursor_secret(config)

    assert first == second
    assert first != b"cre-cli:public-name"
    assert (config.data_dir / "cursor-hmac.secret").stat().st_mode & 0o077 == 0
