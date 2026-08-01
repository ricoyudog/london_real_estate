from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import subprocess
import sys

from nan_fung.ingestion.bank_rate import AcquiredArtifact
from nan_fung.operational import OperationalStore
from nan_fung.projections import rebuild_sqlite_projections
from nan_fung.storage.db import connect_database
from nan_fung.workflows import ingest_bank_rate_artifact


def test_projection_rebuild_uses_only_canonical_observations(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    artifact = AcquiredArtifact(
        body=b"DATE,IUDBEDR\n31 Jul 2026,3.75\n",
        source_url="https://www.bankofengland.co.uk/data.csv",
        retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    result = ingest_bank_rate_artifact(store, artifact)

    report = rebuild_sqlite_projections(store.database_path)
    again = rebuild_sqlite_projections(store.database_path)
    connection = connect_database(store.database_path, read_only=True)
    try:
        row = connection.execute(
            "SELECT metric_name, numeric_text FROM metric_value WHERE observation_id = ?",
            (result.observation_ids[0],),
        ).fetchone()
    finally:
        connection.close()

    assert report.metric_count == again.metric_count == 1
    assert tuple(row) == ("boe.bank_rate.iudbedr", "3.75")


def test_public_projection_rebuild_respects_the_store_writer_lease(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    store.sync_registry()
    child = "\n".join(
        (
            "from nan_fung.operational import WriterAlreadyRunningError",
            "from nan_fung.projections import rebuild_sqlite_projections",
            "try:",
            f"    rebuild_sqlite_projections({str(store.database_path)!r})",
            "except WriterAlreadyRunningError:",
            "    raise SystemExit(0)",
            "raise SystemExit(1)",
        )
    )

    with store.writer_session():
        result = subprocess.run(
            (sys.executable, "-c", child),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    assert result.returncode == 0, result.stderr
