from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nan_fung.ingestion import parser_runner
from nan_fung.ingestion.submarket_mapping import SubmarketMappingError, parse_submarket_mapping_json
from nan_fung.operational import OperationalError, OperationalStore
from nan_fung.projections import rebuild_sqlite_projections
from nan_fung.read_api import ReadContext, ReadQuery, SQLiteReadRepository
from nan_fung.storage.db import connect_database


_DATASOURCE_ID = "custom.london_office_submarkets"
_NOW = datetime(2026, 8, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "body",
    (
        b'{}',
        b'{"name":"West End","locations":[]}',
        b'{"name":"West End","locations":["Mayfair"],"unexpected":true}',
        b'{"name":"West End","name":"City","locations":["Mayfair"]}',
    ),
)
def test_submarket_mapping_parser_rejects_a_non_contract_payload(body: bytes) -> None:
    with pytest.raises(SubmarketMappingError):
        parse_submarket_mapping_json(body)


def test_manual_mapping_requires_an_attestation(tmp_path) -> None:
    with pytest.raises(OperationalError, match="SUBMARKET_MAPPING_ATTESTATION_REQUIRED"):
        OperationalStore(tmp_path).import_manual_evidence(
            _DATASOURCE_ID,
            b'{"name":"West End","locations":["Mayfair"]}',
            media_type="application/json",
        )


def test_invalid_manual_mapping_preserves_evidence_and_fails_the_run(tmp_path) -> None:
    store = OperationalStore(tmp_path)
    body = b'{"name":"West End","locations":[]}'

    with pytest.raises(OperationalError, match="SUBMARKET_MAPPING_INVALID"):
        store.import_manual_evidence(
            _DATASOURCE_ID,
            body,
            media_type="application/json",
            attestation="mapping checked",
        )

    connection = connect_database(store.database_path, read_only=True)
    try:
        row = connection.execute(
            """
            SELECT run.run_id, run.status, job.state AS job_state, evidence.evidence_id
            FROM ingestion_run AS run
            JOIN workflow_job AS job ON job.job_id = run.job_id
            JOIN run_evidence AS run_evidence ON run_evidence.run_id = run.run_id
            JOIN evidence_artifact AS evidence ON evidence.evidence_id = run_evidence.evidence_id
            WHERE run.datasource_id = ?
            """,
            (_DATASOURCE_ID,),
        ).fetchone()
        assert row is not None
        observation_count = connection.execute(
            "SELECT COUNT(*) FROM run_observation WHERE run_id = ?", (row["run_id"],)
        ).fetchone()[0]
        review_count = connection.execute(
            "SELECT COUNT(*) FROM review_task WHERE run_id = ?", (row["run_id"],)
        ).fetchone()[0]
    finally:
        connection.close()

    assert (row["status"], row["job_state"]) == ("failed", "failed")
    assert store.read_evidence(row["evidence_id"]) == body
    assert observation_count == review_count == 0


def test_manual_mapping_fails_closed_without_parser_isolation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        parser_runner,
        "parser_isolation_status",
        lambda: {
            "available": False,
            "backend": None,
            "reason": "PARSER_ISOLATION_UNAVAILABLE",
        },
    )
    store = OperationalStore(tmp_path)

    with pytest.raises(OperationalError, match="PARSER_ISOLATION_UNAVAILABLE"):
        store.import_manual_evidence(
            _DATASOURCE_ID,
            b'{"name":"West End","locations":["Mayfair"]}',
            media_type="application/json",
            attestation="mapping checked",
        )

    assert store.jobs()[0]["state"] == "failed"


def test_approved_manual_mapping_becomes_a_canonical_geography_record(tmp_path) -> None:
    store = OperationalStore(tmp_path)
    submitted = store.import_manual_evidence(
        _DATASOURCE_ID,
        b'{"name":"West  End","locations":["Mayfair","  Soho "],"version":"2026.08"}',
        media_type="application/json",
        attestation="mapping checked against the approved rule set",
    )
    assert submitted.review_id is not None

    reader = SQLiteReadRepository(store.database_path)
    context = ReadContext("operator", frozenset({"internal"}))
    assert tuple(
        reader.query_canonical(
            ReadQuery("geographies"), as_of=_NOW + timedelta(days=1), context=context
        )
    ) == ()

    assert store.decide_review(submitted.review_id, decision="approved", actor_id="reviewer")
    promoted = store.promote_review(submitted.review_id, actor_id="operator")
    records = tuple(
        reader.query_canonical(
            ReadQuery("geographies"), as_of=_NOW + timedelta(days=1), context=context
        )
    )
    report = rebuild_sqlite_projections(store.database_path)

    assert promoted.created
    assert len(records) == 1
    assert records[0].observation_id.startswith("obs_")
    assert records[0].evidence_ids == (submitted.evidence_id,)
    assert records[0].payload == {
        "geography_code": "custom-submarket:west-end",
        "geography_name": "West End",
        "locations": ["Mayfair", "Soho"],
        "mapping_name": "West End",
        "mapping_version": "2026.08",
        "mapping_type": "custom_submarket",
    }
    assert report.geography_count == 1


def test_other_manual_sources_remain_generic_evidence_only(tmp_path) -> None:
    store = OperationalStore(tmp_path)
    result = store.import_manual_evidence(
        "rightmove.commercial_insights_tracker",
        b'{"name":"not a submarket mapping"}',
        media_type="application/json",
        retention_until=_NOW + timedelta(days=30),
    )

    assert result.review_id is not None
    connection = connect_database(store.database_path, read_only=True)
    try:
        observation_count = connection.execute(
            "SELECT COUNT(*) FROM run_observation WHERE run_id = ?", (result.run_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert observation_count == 0
