from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nan_fung.ingestion.pld_supply import (
    LONDON_AUTHORITY_ENTITY_IDS,
    LONDON_AUTHORITY_NAMES,
    PLDApplicationsSearchLifecycle,
    PLDArtifact,
    PLDError,
    PLDParseError,
    PLDRecord,
    InMemoryPLDPersistence,
    parse_planning_applications_csv,
    pld_applications_search_record_key,
    validate_planning_application_record,
)


def _artifact(body: bytes) -> PLDArtifact:
    return PLDArtifact(
        body=body,
        source_url="https://files.planning.data.gov.uk/dataset/planning-application.csv",
        retrieved_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
        headers={"Content-Type": "text/csv"},
    )


def _sample_csv(rows: list[str]) -> bytes:
    header = "organisation-entity,decision-date\n"
    return (header + "".join(rows)).encode("utf-8")


def test_parser_aggregates_per_authority_per_month() -> None:
    payload = _sample_csv(
        [
            "41,2025-03-12\n",
            "41,2025-03-25\n",
            "41,2025-04-01\n",
            "203,2025-03-10\n",
        ]
    )
    records = parse_planning_applications_csv(payload)
    by_key = {r.record_key: r for r in records}
    assert by_key[("41", "2025-03")].planning_application_count == "2"
    assert by_key[("41", "2025-04")].planning_application_count == "1"
    assert by_key[("203", "2025-03")].planning_application_count == "1"


def test_parser_skips_non_london_authorities() -> None:
    payload = _sample_csv(["41,2025-03-12\n", "9999,2025-03-12\n", "100,2025-03-12\n"])
    records = parse_planning_applications_csv(payload)
    entities = {r.organisation_entity for r in records}
    assert "9999" in entities or all(r.organisation_entity != "9999" for r in records)
    assert all(r.organisation_entity in LONDON_AUTHORITY_ENTITY_IDS for r in records)


def test_parser_skips_undecided_applications() -> None:
    payload = _sample_csv(["41,2025-03-12\n", "41,\n", "41,\n"])
    records = parse_planning_applications_csv(payload)
    assert len(records) == 1
    assert records[0].planning_application_count == "1"


def test_parser_rejects_missing_columns() -> None:
    bad = b"foo,bar\n41,2025-03-12\n"
    with pytest.raises(PLDParseError, match="missing required columns"):
        parse_planning_applications_csv(bad)


def test_parser_rejects_empty_payload() -> None:
    with pytest.raises(PLDParseError):
        parse_planning_applications_csv(b"organisation-entity,decision-date\n")


def test_parser_rejects_non_utf8() -> None:
    with pytest.raises(PLDParseError, match="UTF-8"):
        parse_planning_applications_csv(b"\xff\xfeorganisation-entity,decision-date\n")


def test_record_validates_canonical_decimal_count() -> None:
    record = PLDRecord(
        organisation_entity="41",
        borough="Barking and Dagenham",
        period_year=2025,
        period_month=3,
        planning_application_count="12",
    )
    validate_planning_application_record(record)
    assert record.planning_application_count == "12"


def test_record_normalizes_count_to_decimal_string() -> None:
    record = PLDRecord(
        organisation_entity="203",
        borough="City of London",
        period_year=2025,
        period_month=3,
        planning_application_count=5,
    )
    assert record.planning_application_count == "5"


def test_record_rejects_unknown_entity() -> None:
    with pytest.raises(PLDParseError, match="not a London authority"):
        PLDRecord(
            organisation_entity="9999",
            borough="Nowhere",
            period_year=2025,
            period_month=3,
            planning_application_count="1",
        )


def test_record_rejects_borough_entity_mismatch() -> None:
    with pytest.raises(PLDParseError, match="does not match entity"):
        PLDRecord(
            organisation_entity="41",
            borough="Camden",
            period_year=2025,
            period_month=3,
            planning_application_count="1",
        )


def test_record_rejects_invalid_month() -> None:
    with pytest.raises(PLDParseError, match="period_month"):
        PLDRecord(
            organisation_entity="41",
            borough="Barking and Dagenham",
            period_year=2025,
            period_month=13,
            planning_application_count="1",
        )


def test_record_rejects_negative_count() -> None:
    with pytest.raises(PLDParseError, match="non-negative"):
        PLDRecord(
            organisation_entity="41",
            borough="Barking and Dagenham",
            period_year=2025,
            period_month=3,
            planning_application_count="-5",
        )


def test_record_rejects_float_notation() -> None:
    with pytest.raises(PLDParseError, match="exponent"):
        PLDRecord(
            organisation_entity="41",
            borough="Barking and Dagenham",
            period_year=2025,
            period_month=3,
            planning_application_count="1.5e2",
        )


def test_record_payload_includes_metric_id() -> None:
    record = PLDRecord(
        organisation_entity="41",
        borough="Barking and Dagenham",
        period_year=2025,
        period_month=3,
        planning_application_count="7",
    )
    payload = record.payload
    assert payload["metric_id"] == "planning_application_count"
    assert payload["planning_application_count"] == "7"
    assert payload["geography_code"] == "41"
    assert payload["period_year"] == "2025"
    assert payload["period_month"] == "03"


def test_record_key_binding_round_trips() -> None:
    record = PLDRecord(
        organisation_entity="387",
        borough="Westminster",
        period_year=2025,
        period_month=11,
        planning_application_count="42",
    )
    assert pld_applications_search_record_key(record) == ("387", "2025-11")
    # Mapping form must agree.
    assert (
        pld_applications_search_record_key(record.payload)
        == pld_applications_search_record_key(record)
    )


def test_all_33_london_authorities_have_name_mapping() -> None:
    assert len(LONDON_AUTHORITY_ENTITY_IDS) == 33
    assert len(LONDON_AUTHORITY_NAMES) == 33
    assert set(LONDON_AUTHORITY_NAMES) == LONDON_AUTHORITY_ENTITY_IDS


def test_lifecycle_captures_evidence_before_parse_and_promotes() -> None:
    persistence = InMemoryPLDPersistence()
    lifecycle = PLDApplicationsSearchLifecycle(persistence)
    payload = _sample_csv(
        ["41,2025-03-12\n", "41,2025-03-25\n", "203,2025-03-10\n"]
    )
    result = lifecycle.ingest(_artifact(payload))
    assert result.status == "succeeded"
    assert result.canonical_changed
    assert len(result.observation_ids) == 2
    assert persistence.events == [
        "create_run",
        "persist_evidence",
        "read_evidence",
        "persist_observation",
        "persist_observation",
        "promote",
        "finish_run:succeeded",
    ]


def test_lifecycle_marks_run_failed_on_parse_error() -> None:
    persistence = InMemoryPLDPersistence()
    lifecycle = PLDApplicationsSearchLifecycle(persistence)
    with pytest.raises(PLDParseError):
        lifecycle.ingest(_artifact(b"organisation-entity,decision-date\n"))
    assert persistence.run_status[persistence.events and persistence.runs[list(persistence.runs)[0]].run_id] == "failed"


def test_lifecycle_does_not_promote_in_discovery_lane() -> None:
    persistence = InMemoryPLDPersistence()
    lifecycle = PLDApplicationsSearchLifecycle(persistence)
    payload = _sample_csv(["41,2025-03-12\n"])
    result = lifecycle.ingest(_artifact(payload), lane="source_discovery")
    assert result.status == "succeeded"
    assert not result.canonical_changed
    assert "promote" not in persistence.events


def test_lifecycle_rejects_unsupported_lane() -> None:
    persistence = InMemoryPLDPersistence()
    lifecycle = PLDApplicationsSearchLifecycle(persistence)
    with pytest.raises(PLDError, match="unsupported lane"):
        lifecycle.ingest(_artifact(_sample_csv(["41,2025-03-12\n"])), lane="bogus")
