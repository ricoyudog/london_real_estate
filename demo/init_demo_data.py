from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from nan_fung.operational import OperationalStore


MARKER_NAME = ".nan-fung-demo-data.v1.json"
MARKER_SCHEMA = "nan_fung_demo_data.v1"
EXPECTED_VALUE = "5.25"
EXPECTED_DATE = "2026-07-31"
EXPECTED_FIXTURE_SHA256 = "4c732b5d9900ffefa624be616bfef55ea5bd9df229dd5e95886a0b2c3050111d"
SOURCE_URL = "https://www.bankofengland.co.uk/boeapps/database/Bank-Rate.asp"


class DemoInitError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = initialise(args.data_dir, args.fixture, os.environ.get("MARKET_DESK_MODE", "demo"))
    except DemoInitError as error:
        print(f"demo data init failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


def initialise(data_dir: Path, fixture: Path, mode: str) -> dict[str, str]:
    marker = data_dir / MARKER_NAME
    if marker.exists() and mode != "demo":
        raise DemoInitError("demo marker cannot be opened outside demo mode")
    if mode != "demo":
        raise DemoInitError("demo data initializer requires MARKET_DESK_MODE=demo")
    if fixture.is_symlink() or not fixture.is_file():
        raise DemoInitError("fixture must be a regular file")

    fixture_sha256 = hashlib.sha256(fixture.read_bytes()).hexdigest()
    if fixture_sha256 != EXPECTED_FIXTURE_SHA256:
        raise DemoInitError("fixture checksum is not the packaged demo fixture")
    data_dir.mkdir(parents=True, exist_ok=True)
    _cre(data_dir, "db", "migrate")

    if marker.exists():
        saved = _read_marker(marker)
        if saved.get("schema_version") != MARKER_SCHEMA:
            raise DemoInitError("demo marker version is unsupported")
        if saved.get("fixture_sha256") != fixture_sha256:
            raise DemoInitError("fixture checksum does not match the demo marker")
        _verify(data_dir)
        return {"schema_version": MARKER_SCHEMA, "state": "verified"}

    _seed_trusted_fixture(data_dir, fixture.read_bytes())
    _verify(data_dir)
    _write_marker(
        marker,
        {
            "schema_version": MARKER_SCHEMA,
            "fixture_sha256": fixture_sha256,
            "bank_rate_percent": EXPECTED_VALUE,
            "source_date": EXPECTED_DATE,
        },
    )
    return {"schema_version": MARKER_SCHEMA, "state": "seeded"}


def _seed_trusted_fixture(data_dir: Path, fixture_bytes: bytes) -> None:
    retrieved_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    try:
        store = OperationalStore(data_dir)
        store.sync_registry(now=retrieved_at)
        queued = store.enqueue(
            "boe.bank_rate.iudbedr",
            scheduled_for=retrieved_at,
            request={"fixture": MARKER_SCHEMA},
        )
        claim = store.claim_job(queued.job_id, "demo-data-init", now=retrieved_at)
        if claim is None:
            raise DemoInitError("fixture job could not be claimed")
        run = store.start_run(claim, "demo-data-init", now=retrieved_at)
        evidence = store.persist_evidence(
            run,
            fixture_bytes,
            media_type="text/csv",
            request={"method": "GET", "url": SOURCE_URL, "series": "IUDBEDR", "fixture": MARKER_SCHEMA},
            response={
                "status": 200,
                "final_url": SOURCE_URL,
                "title": "Bank Rate history and data",
                "published_at": "2026-08-01T09:00:00Z",
                "source_updated_at": "2026-08-01T10:00:00Z",
            },
            retrieved_at=retrieved_at,
            now=retrieved_at,
        )
        store.persist_observation(
            run,
            record_key=("IUDBEDR", EXPECTED_DATE),
            payload={"date": EXPECTED_DATE, "bank_rate_percent": EXPECTED_VALUE},
            record_type="metric",
            category="interest-rates-monetary-policy",
            evidence=(evidence,),
            locator={"kind": "packaged_fixture", "row_key": EXPECTED_DATE},
            source_date=EXPECTED_DATE,
            unit="percent",
            definition_text="Official Bank of England Bank Rate series IUDBEDR",
            limitations=("Deterministic Docker demo fixture; not live ingestion",),
            now=retrieved_at,
        )
        store.finish_run(run, status="succeeded", promote=True, now=retrieved_at)
    except DemoInitError:
        raise
    except Exception as error:
        raise DemoInitError("trusted fixture seed failed") from error


def _verify(data_dir: Path) -> None:
    integrity = _record(_cre(data_dir, "db", "integrity").get("result"))
    if integrity.get("ok") is not True or integrity.get("integrity_check") != ["ok"]:
        raise DemoInitError("database integrity check failed")
    latest = _record(_record(_cre(data_dir, "observations", "latest").get("result")).get("response"))
    records = latest.get("records")
    if not isinstance(records, list):
        raise DemoInitError("canonical Bank Rate record is unavailable")
    for value in records:
        record = _record(value)
        payload = _record(record.get("payload"))
        if (
            record.get("canonical") is True
            and record.get("datasource_id") == "boe.bank_rate.iudbedr"
            and payload.get("bank_rate_percent") == EXPECTED_VALUE
            and payload.get("date") == EXPECTED_DATE
        ):
            return
    raise DemoInitError("canonical Bank Rate fixture does not match the marker")


def _cre(data_dir: Path, *arguments: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["cre", "--data-dir", str(data_dir), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise DemoInitError(f"cre {' '.join(arguments[:2])} failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise DemoInitError("cre returned malformed JSON") from error
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise DemoInitError("cre reported an unsuccessful operation")
    return payload


def _read_marker(marker: Path) -> dict[str, Any]:
    if marker.is_symlink() or not marker.is_file():
        raise DemoInitError("demo marker must be a regular file")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DemoInitError("demo marker is unreadable") from error
    if not isinstance(value, dict):
        raise DemoInitError("demo marker is malformed")
    return value


def _write_marker(marker: Path, value: dict[str, str]) -> None:
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    encoded = f"{json.dumps(value, separators=(',', ':'), sort_keys=True)}\n".encode()
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
        directory = os.open(marker.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _record(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


if __name__ == "__main__":
    raise SystemExit(main())
