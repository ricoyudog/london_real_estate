from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nan_fung.ingestion.bank_rate import AcquiredArtifact
from nan_fung.operational import OperationalStore
from nan_fung.read_api import AccessClass, ReadContext, ReadQuery, ReadService
from nan_fung.read_api.sqlite_repository import SQLiteReadRepository
from nan_fung.workflows import ingest_bank_rate_artifact


def test_sqlite_reads_derive_dual_freshness_and_degraded_from_persisted_schedule(
    tmp_path: Path,
) -> None:
    captured_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    result = ingest_bank_rate_artifact(
        store,
        AcquiredArtifact(
            body=b"DATE,IUDBEDR\n31 Jul 2026,3.75\n",
            source_url="https://www.bankofengland.co.uk/boeapps/database/offline.csv",
            retrieved_at=captured_at,
        ),
        execution_at=captured_at,
    )
    reader = ReadService(
        SQLiteReadRepository(store.database_path),
        cursor_secret=b"freshness-test",
        clock=lambda: captured_at,
    )
    context = ReadContext("reader", frozenset({AccessClass.OPEN}))

    fresh = reader.query(context, ReadQuery("metrics", as_of=captured_at)).records[0]
    stale_at = captured_at + timedelta(days=10)
    stale = reader.query(context, ReadQuery("metrics", as_of=stale_at)).records[0]
    health = next(
        record
        for record in reader.query(context, ReadQuery("health", as_of=stale_at)).records
        if record.datasource_id == "boe.bank_rate.iudbedr"
    )

    assert fresh.observation_id == result.observation_ids[0]
    assert (fresh.retrieval_freshness, fresh.observation_freshness, fresh.degraded) == (
        "fresh",
        "fresh",
        False,
    )
    assert (stale.retrieval_freshness, stale.observation_freshness, stale.degraded) == (
        "stale",
        "stale",
        True,
    )
    assert (health.retrieval_freshness, health.observation_freshness, health.degraded) == (
        "stale",
        "stale",
        True,
    )


def test_fresh_acquisition_does_not_hide_a_stale_upstream_observation(tmp_path: Path) -> None:
    captured_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    ingest_bank_rate_artifact(
        store,
        AcquiredArtifact(
            body=b"DATE,IUDBEDR\n31 Jul 2020,0.10\n",
            source_url="https://www.bankofengland.co.uk/boeapps/database/offline.csv",
            retrieved_at=captured_at,
        ),
        execution_at=captured_at,
    )
    reader = ReadService(
        SQLiteReadRepository(store.database_path),
        cursor_secret=b"freshness-test",
        clock=lambda: captured_at,
    )

    record = reader.query(
        ReadContext("reader", frozenset({AccessClass.OPEN})),
        ReadQuery("metrics", as_of=captured_at),
    ).records[0]

    assert record.retrieval_freshness == "fresh"
    assert record.observation_freshness == "stale"
    assert record.degraded is True


def test_discovery_success_is_not_reported_as_canonical_availability(tmp_path: Path) -> None:
    captured_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    ingest_bank_rate_artifact(
        store,
        AcquiredArtifact(
            body=b"DATE,IUDBEDR\n31 Jul 2026,3.75\n",
            source_url="https://www.bankofengland.co.uk/boeapps/database/offline.csv",
            retrieved_at=captured_at,
        ),
        lane="source_discovery",
        execution_at=captured_at,
    )
    reader = ReadService(
        SQLiteReadRepository(store.database_path),
        cursor_secret=b"freshness-test",
        clock=lambda: captured_at,
    )

    health = next(
        record
        for record in reader.query(
            ReadContext("reader", frozenset({AccessClass.OPEN})),
            ReadQuery("health", as_of=captured_at),
        ).records
        if record.datasource_id == "boe.bank_rate.iudbedr"
    )

    assert health.retrieval_freshness == "fresh"
    assert health.observation_freshness == "never_ingested"
    assert health.canonical_available is False
    assert health.payload["last_retrieval_lane"] == "source_discovery"
    assert health.payload["last_promoted_run_id"] is None
