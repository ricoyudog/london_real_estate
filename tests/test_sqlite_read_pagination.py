from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nan_fung.ingestion.registry import DatasourceRegistry, default_registry
from nan_fung.operational import OperationalStore
from nan_fung.read_api import AccessClass, ReadContext, ReadQuery, ReadService
from nan_fung.read_api.sqlite_repository import SQLiteReadRepository


BASE = datetime(2026, 8, 1, 9, tzinfo=UTC)


def _store(tmp_path: Path) -> OperationalStore:
    seed = default_registry()
    bank_rate = seed.lookup("boe.bank_rate.iudbedr")
    internal_metric = replace(
        seed.lookup("custom.london_office_submarkets"),
        datasource_id="test.internal_metric",
        display_name="Internal metric fixture",
        promotion_policy="automatic",
        record_key_builder_name="test.internal_metric.record_key",
    )
    registry = DatasourceRegistry(
        (bank_rate, internal_metric),
        (seed.lookup_source("boe.iadb"), seed.lookup_source("custom.submarkets")),
    )
    return OperationalStore(tmp_path, registry=registry)


def _write_metric(
    store: OperationalStore,
    *,
    datasource_id: str,
    record_key: str,
    available_at: datetime,
    lane: str = "production_ingestion",
    finish: bool = True,
) -> tuple[str, object, object]:
    queued = store.enqueue(
        datasource_id,
        request={"fixture_key": record_key},
        lane=lane,
        scheduled_for=available_at,
    )
    claim = store.claim_job(queued.job_id, "reader-test", now=available_at)
    assert claim is not None
    run = store.start_run(claim, "reader-test", now=available_at)
    evidence = store.persist_evidence(
        run,
        f"evidence:{record_key}".encode(),
        media_type="text/plain",
        retrieved_at=available_at,
        now=available_at,
        request=(
            {
                "method": "GET",
                "url": "https://www.bankofengland.co.uk/boeapps/database/offline.csv",
            }
            if datasource_id.startswith("boe.")
            else {"method": "MANUAL_IMPORT"}
        ),
        response=(
            {
                "status": 200,
                "final_url": "https://www.bankofengland.co.uk/boeapps/database/offline.csv",
            }
            if datasource_id.startswith("boe.")
            else {}
        ),
    )
    provider = "Bank of England" if datasource_id.startswith("boe.") else "Internal"
    observation_id = store.persist_observation(
        run,
        record_key=(record_key,),
        payload={
            "metric_id": record_key,
            "provider": provider,
            "value": record_key,
        },
        record_type="metric",
        category="macro",
        evidence=(evidence,),
        source_date=available_at.date().isoformat(),
        now=available_at,
    )
    if finish:
        store.finish_run(
            run,
            status="succeeded",
            promote=lane == "production_ingestion",
            now=available_at,
        )
    return observation_id, run, evidence


class _NoLegacySQLiteRepository(SQLiteReadRepository):
    def query_canonical(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("the SQL page path must not load every canonical record")

    def query_result(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("the SQL page path must not load every result record")


class _TrackingConnection:
    def __init__(self, connection: object, statements: list[tuple[str, tuple[object, ...]]]) -> None:
        self._connection = connection
        self._statements = statements

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> object:
        self._statements.append((statement, tuple(parameters)))
        return self._connection.execute(statement, parameters)  # type: ignore[attr-defined]

    def close(self) -> None:
        self._connection.close()  # type: ignore[attr-defined]


def test_sqlite_canonical_page_uses_sql_keyset_filters_access_and_one_evidence_batch(
    tmp_path: Path, monkeypatch: object
) -> None:
    store = _store(tmp_path)
    expected = [
        _write_metric(
            store,
            datasource_id="boe.bank_rate.iudbedr",
            record_key="bank_old",
            available_at=BASE,
        )[0],
        _write_metric(
            store,
            datasource_id="boe.bank_rate.iudbedr",
            record_key="bank_middle",
            available_at=BASE + timedelta(hours=1),
        )[0],
        _write_metric(
            store,
            datasource_id="boe.bank_rate.iudbedr",
            record_key="bank_new",
            available_at=BASE + timedelta(hours=2),
        )[0],
    ]
    internal_id, _, _ = _write_metric(
        store,
        datasource_id="test.internal_metric",
        record_key="internal_only",
        available_at=BASE + timedelta(minutes=90),
    )

    import nan_fung.read_api.sqlite_repository as sqlite_module

    statements: list[tuple[str, tuple[object, ...]]] = []
    real_connect = sqlite_module.connect_database

    def tracking_connect(path: Path, *, read_only: bool = False) -> _TrackingConnection:
        return _TrackingConnection(real_connect(path, read_only=read_only), statements)

    monkeypatch.setattr(sqlite_module, "connect_database", tracking_connect)  # type: ignore[attr-defined]
    reader = ReadService(
        _NoLegacySQLiteRepository(store.database_path),
        cursor_secret=b"sqlite-pagination",
        clock=lambda: BASE + timedelta(hours=4),
    )
    open_context = ReadContext("open-reader", frozenset({AccessClass.OPEN}))
    first = reader.query(
        open_context,
        ReadQuery(
            "metrics",
            filters={"provider": "Bank of England"},
            as_of=BASE + timedelta(hours=3),
            limit=2,
        ),
    )

    assert [record.observation_id for record in first.records] == [expected[2], expected[1]]
    assert first.total_count == 3
    assert first.next_cursor is not None
    evidence_statements = [
        statement
        for statement, _ in statements
        if "SELECT oe.run_id, oe.observation_id, oe.evidence_id" in statement
    ]
    assert len(evidence_statements) == 1
    page_statements = [
        (statement, parameters)
        for statement, parameters in statements
        if "ORDER BY candidate.available_at DESC, candidate.observation_id DESC LIMIT ?" in statement
    ]
    assert len(page_statements) == 1
    assert page_statements[0][1][-1] == 3

    second = reader.query(
        open_context,
        ReadQuery(
            "metrics",
            filters={"provider": "Bank of England"},
            cursor=first.next_cursor,
            limit=2,
        ),
    )
    assert [record.observation_id for record in second.records] == [expected[0]]
    assert set(item.observation_id for item in first.records).isdisjoint(
        item.observation_id for item in second.records
    )

    open_without_provider = reader.query(
        open_context,
        ReadQuery("metrics", as_of=BASE + timedelta(hours=3), limit=10),
    )
    internal_context = ReadContext(
        "internal-reader", frozenset({AccessClass.OPEN, AccessClass.INTERNAL})
    )
    internal_response = reader.query(
        internal_context,
        ReadQuery("metrics", as_of=BASE + timedelta(hours=3), limit=10),
    )
    as_of_response = reader.query(
        open_context,
        ReadQuery("metrics", as_of=BASE + timedelta(minutes=75), limit=10),
    )
    assert internal_id not in {record.observation_id for record in open_without_provider.records}
    assert internal_id in {record.observation_id for record in internal_response.records}
    assert [record.observation_id for record in as_of_response.records] == [
        expected[1],
        expected[0],
    ]


def test_sqlite_run_result_page_uses_the_same_keyset_contract(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_id, run, evidence = _write_metric(
        store,
        datasource_id="boe.bank_rate.iudbedr",
        record_key="research_first",
        available_at=BASE,
        lane="source_discovery",
        finish=False,
    )
    second_id = store.persist_observation(
        run,
        record_key=("research_second",),
        payload={
            "metric_id": "research_second",
            "provider": "Bank of England",
            "value": "research_second",
        },
        record_type="metric",
        category="macro",
        evidence=(evidence,),
        source_date=BASE.date().isoformat(),
        now=BASE,
    )
    store.finish_run(run, status="succeeded", now=BASE)

    reader = ReadService(
        _NoLegacySQLiteRepository(store.database_path),
        cursor_secret=b"sqlite-result-pagination",
        clock=lambda: BASE + timedelta(hours=1),
    )
    context = ReadContext(
        "research-reader",
        frozenset({AccessClass.OPEN}),
        frozenset({run.run_id}),
    )
    first = reader.query(context, ReadQuery("metrics", result_ref=run.run_id, limit=1))
    assert first.next_cursor is not None
    second = reader.query(
        context,
        ReadQuery("metrics", result_ref=run.run_id, cursor=first.next_cursor, limit=1),
    )

    assert first.canonical is False
    assert second.canonical is False
    assert {record.observation_id for record in first.records + second.records} == {
        first_id,
        second_id,
    }
    assert first.total_count == second.total_count == 2
