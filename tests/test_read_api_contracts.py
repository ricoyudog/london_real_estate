from datetime import UTC, date, datetime, timedelta

import pytest

from nan_fung.read_api import (
    AccessClass,
    AccessDenied,
    InMemoryReadRepository,
    InvalidCursor,
    InvalidReadRequest,
    ReadContext,
    ReadQuery,
    ReadRecord,
    ReadService,
    query_data_v1,
)


NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


def record(
    observation_id: str,
    *,
    available_at: datetime = NOW,
    access_class: AccessClass = AccessClass.OPEN,
    canonical: bool = True,
    lane: str = "production_ingestion",
    source_date: date | None = None,
) -> ReadRecord:
    return ReadRecord(
        observation_id=observation_id,
        datasource_id="boe.bank_rate.iudbedr",
        query_kind="metrics",
        category="macro",
        record_type="bank_rate",
        access_class=access_class,
        available_at=available_at,
        payload={"metric_id": "bank_rate", "provider": "Bank of England", "value": "5.00"},
        evidence_ids=(f"ev_{observation_id}",),
        source_date=source_date,
        unit="percent",
        definition="Bank Rate",
        canonical=canonical,
        lane=lane,
    )


def service(repository: InMemoryReadRepository, now: datetime = NOW) -> ReadService:
    return ReadService(repository, cursor_secret=b"test-only-secret", clock=lambda: now)


def test_read_contract_exposes_evidence_only_as_observation_references() -> None:
    with pytest.raises(InvalidReadRequest):
        ReadQuery("evidence")


def test_read_contract_bounds_filter_cardinality_and_value_size() -> None:
    with pytest.raises(InvalidReadRequest, match="at most 50"):
        ReadQuery(
            "metrics",
            filters={"datasource_id": [f"source-{index}" for index in range(51)]},
        )
    with pytest.raises(InvalidReadRequest, match="at most 512"):
        ReadQuery("metrics", filters={"provider": "x" * 513})
    with pytest.raises(InvalidReadRequest, match="at most 100"):
        ReadQuery(
            "metrics",
            filters={
                "datasource_id": [f"source-{index}" for index in range(50)],
                "observation_id": [f"observation-{index}" for index in range(50)],
                "evidence_id": [f"evidence-{index}" for index in range(1)],
            },
        )


def test_query_filters_access_before_count_and_preserves_strictest_access() -> None:
    repository = InMemoryReadRepository(
        [
            record("obs_open", access_class=AccessClass.OPEN),
            record("obs_restricted", access_class=AccessClass.RESTRICTED),
            record("obs_discovery", canonical=False, lane="source_discovery"),
        ]
    )
    open_context = ReadContext("agent", frozenset({AccessClass.OPEN}))

    response = query_data_v1(
        service(repository), open_context, ReadQuery("metrics", limit=10)
    )

    assert [item.observation_id for item in response.records] == ["obs_open"]
    assert response.total_count == 1
    assert response.access_class is AccessClass.OPEN
    assert response.canonical is True

    restricted_context = ReadContext(
        "dashboard", frozenset({AccessClass.OPEN, AccessClass.RESTRICTED})
    )
    restricted_response = service(repository).query(
        restricted_context, ReadQuery("metrics", limit=10)
    )
    assert {item.observation_id for item in restricted_response.records} == {
        "obs_open",
        "obs_restricted",
    }
    assert restricted_response.access_class is AccessClass.RESTRICTED


def test_keyset_cursor_is_as_of_anchored_and_rejects_tampering_or_policy_change() -> None:
    repository = InMemoryReadRepository(
        [
            record("obs_3", available_at=NOW - timedelta(minutes=1)),
            record("obs_2", available_at=NOW - timedelta(minutes=2)),
            record("obs_1", available_at=NOW - timedelta(minutes=3)),
        ]
    )
    reader = service(repository)
    context = ReadContext("agent", frozenset({AccessClass.OPEN}))
    first = reader.query(context, ReadQuery("metrics", limit=2))

    assert [item.observation_id for item in first.records] == ["obs_3", "obs_2"]
    assert first.next_cursor is not None
    second = reader.query(context, ReadQuery("metrics", cursor=first.next_cursor, limit=2))
    assert [item.observation_id for item in second.records] == ["obs_1"]
    assert second.total_count == 3

    assert first.next_cursor is not None
    with pytest.raises(InvalidCursor):
        reader.query(context, ReadQuery("metrics", cursor=f"{first.next_cursor}x"))
    changed_context = ReadContext("other-agent", frozenset({AccessClass.OPEN}))
    with pytest.raises(InvalidCursor):
        reader.query(changed_context, ReadQuery("metrics", cursor=first.next_cursor))


def test_as_of_and_allowlisted_filters_are_applied_without_direct_repository_sql() -> None:
    repository = InMemoryReadRepository(
        [
            record(
                "obs_old",
                available_at=NOW - timedelta(days=2),
                source_date=date(2026, 7, 29),
            ),
            record(
                "obs_new",
                available_at=NOW - timedelta(days=1),
                source_date=date(2026, 7, 31),
            ),
        ]
    )
    response = service(repository).query(
        ReadContext("agent", frozenset({AccessClass.OPEN})),
        ReadQuery(
            "metrics",
            filters={"metric_id": "bank_rate", "source_date_from": "2026-07-30"},
            as_of=NOW - timedelta(hours=12),
        ),
    )

    assert [item.observation_id for item in response.records] == ["obs_new"]
    assert response.anchor_as_of == NOW - timedelta(hours=12)


def test_run_scoped_results_require_a_context_capability_and_are_not_canonical() -> None:
    result_ref = "result_capability_opaque"
    run_record = record(
        "obs_ad_hoc",
        canonical=False,
        lane="ad_hoc_research",
        access_class=AccessClass.INTERNAL,
    )
    repository = InMemoryReadRepository(result_records={result_ref: [run_record]})
    reader = service(repository)
    denied = ReadContext("agent", frozenset({AccessClass.INTERNAL}))
    with pytest.raises(AccessDenied):
        reader.query(denied, ReadQuery("metrics", result_ref=result_ref))

    granted = ReadContext(
        "agent", frozenset({AccessClass.INTERNAL}), frozenset({result_ref})
    )
    response = reader.query(granted, ReadQuery("metrics", result_ref=result_ref))
    assert response.canonical is False
    assert [item.observation_id for item in response.records] == ["obs_ad_hoc"]
