from __future__ import annotations

from pathlib import Path
import tomllib


def test_default_pytest_gate_excludes_live_and_network_markers() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    settings = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert settings["tool"]["pytest"]["ini_options"]["addopts"] == "-m 'not live and not network'"
