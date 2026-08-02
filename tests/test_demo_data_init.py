from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "demo" / "init_demo_data.py"
_FIXTURE = _ROOT / "demo" / "fixtures" / "bank-rate-v1.csv"


def test_demo_init_seeds_once_then_verifies_the_persistent_store(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    first = _run(data_dir, mode="demo")
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["state"] == "seeded"
    first_runs = _run_count(data_dir)

    second = _run(data_dir, mode="demo")
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["state"] == "verified"
    assert _run_count(data_dir) == first_runs


def test_demo_init_fails_closed_when_demo_marker_is_opened_in_production(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    assert _run(data_dir, mode="demo").returncode == 0

    production = _run(data_dir, mode="production")

    assert production.returncode != 0
    assert "demo marker" in production.stderr.lower()


def test_demo_init_rejects_a_fixture_that_no_longer_matches_its_marker(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    fixture = tmp_path / "bank-rate.csv"
    fixture.write_bytes(_FIXTURE.read_bytes())
    assert _run(data_dir, mode="demo", fixture=fixture).returncode == 0
    fixture.write_text("DATE,IUDBEDR\n31 Jul 2026,4.00\n", encoding="utf-8")

    tampered = _run(data_dir, mode="demo", fixture=fixture)

    assert tampered.returncode != 0
    assert "fixture checksum" in tampered.stderr.lower()


def _run(
    data_dir: Path,
    *,
    mode: str,
    fixture: Path = _FIXTURE,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--data-dir",
            str(data_dir),
            "--fixture",
            str(fixture),
        ],
        cwd=_ROOT,
        env={**os.environ, "MARKET_DESK_MODE": mode},
        text=True,
        capture_output=True,
        check=False,
    )


def _run_count(data_dir: Path) -> int:
    connection = sqlite3.connect(data_dir / "operational.sqlite3")
    try:
        value = connection.execute("SELECT count(*) FROM ingestion_run").fetchone()
    finally:
        connection.close()
    assert value is not None
    return int(value[0])
