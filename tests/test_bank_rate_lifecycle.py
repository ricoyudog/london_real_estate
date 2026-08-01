from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nan_fung.ingestion.bank_rate import (
    AcquiredArtifact,
    BankRateError,
    BankRateLifecycle,
    BankRateParseError,
    InMemoryBankRatePersistence,
    StoredAcquiredArtifact,
)
from nan_fung.storage.artifacts import ArtifactStore


def _artifact(body: bytes) -> AcquiredArtifact:
    return AcquiredArtifact(
        body=body,
        source_url="https://www.bankofengland.co.uk/data?Datefrom=01%2FJan%2F2025",
        retrieved_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
        headers={"Authorization": "Bearer secret", "Content-Type": "text/csv"},
    )


def test_bank_rate_artifact_rejects_an_unapproved_source_url() -> None:
    with pytest.raises(BankRateError, match="unapproved provenance"):
        AcquiredArtifact(
            body=b"DATE,IUDBEDR\n31 Jul 2026,3.75\n",
            source_url="https://evil.example/fake.csv",
            retrieved_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
        )


def test_bank_rate_artifact_validates_original_url_before_redaction() -> None:
    with pytest.raises(BankRateError, match="unapproved provenance"):
        AcquiredArtifact(
            body=b"DATE,IUDBEDR\n31 Jul 2026,3.75\n",
            source_url="https://operator:secret@www.bankofengland.co.uk/data.csv",
            retrieved_at=datetime(2026, 8, 3, 12, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (204, {}),
        (206, {"Content-Range": "bytes 0-5/10"}),
        (200, {"Content-Range": "bytes 0-5/10"}),
    ],
)
def test_bank_rate_artifacts_require_a_complete_http_200(
    tmp_path, status: int, headers: dict[str, str]
) -> None:
    arguments = {
        "source_url": "https://www.bankofengland.co.uk/data.csv",
        "retrieved_at": datetime(2026, 8, 3, 12, tzinfo=UTC),
        "status": status,
        "headers": headers,
    }

    with pytest.raises(BankRateError, match="complete HTTP 200"):
        AcquiredArtifact(body=b"DATE,IUDBEDR\n", **arguments)
    with pytest.raises(BankRateError, match="complete HTTP 200"):
        StoredAcquiredArtifact(
            artifact=ArtifactStore(tmp_path).put_bytes(b"DATE,IUDBEDR\n"),
            request_url="https://www.bankofengland.co.uk/data.csv",
            **arguments,
        )


def test_bank_rate_lifecycle_captures_evidence_before_parse_and_promotes() -> None:
    persistence = InMemoryBankRatePersistence()
    lifecycle = BankRateLifecycle(persistence)

    result = lifecycle.ingest(
        _artifact(b"DATE,IUDBEDR\r\n30 Jul 2026,3.75\r\n31 Jul 2026,4.0\r\n")
    )

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
    run = persistence.runs[result.run_id]
    assert "secret" not in str(run.request)
    assert persistence.run_status[result.run_id] == "succeeded"
    assert len(persistence.canonical) == 2


def test_bank_rate_parse_failure_keeps_captured_evidence_and_marks_run_failed() -> None:
    persistence = InMemoryBankRatePersistence()
    lifecycle = BankRateLifecycle(persistence)

    with pytest.raises(BankRateParseError, match="DATE and IUDBEDR"):
        lifecycle.ingest(_artifact(b"DATE,OTHER\n30 Jul 2026,3.75\n"))

    assert persistence.events == [
        "create_run",
        "persist_evidence",
        "read_evidence",
        "finish_run:failed",
    ]
    assert len(persistence.evidence) == 1
    assert len(persistence.observations) == 0
    assert not persistence.canonical
    assert set(persistence.run_status.values()) == {"failed"}


def test_empty_bank_rate_file_finishes_without_promotion() -> None:
    persistence = InMemoryBankRatePersistence()

    result = BankRateLifecycle(persistence).ingest(_artifact(b"DATE,IUDBEDR\n"))

    assert result.status == "empty"
    assert not result.observation_ids
    assert not result.canonical_changed
    assert persistence.events == [
        "create_run",
        "persist_evidence",
        "read_evidence",
        "finish_run:empty",
    ]
    assert not persistence.canonical


def test_bank_rate_revisions_support_a_to_b_to_a_without_mutating_history() -> None:
    persistence = InMemoryBankRatePersistence()
    lifecycle = BankRateLifecycle(persistence)

    first = lifecycle.ingest(_artifact(b"DATE,IUDBEDR\n30 Jul 2026,3.75\n"))
    second = lifecycle.ingest(_artifact(b"DATE,IUDBEDR\n30 Jul 2026,4.00\n"))
    third = lifecycle.ingest(_artifact(b"DATE,IUDBEDR\n30 Jul 2026,3.75\n"))

    assert first.canonical_changed and second.canonical_changed and third.canonical_changed
    assert len(persistence.observations) == 3
    assert persistence.canonical[("IUDBEDR", "2026-07-30")] == third.observation_ids[0]
    assert [record.rate_percent for record in persistence.observations.values()] == [
        "3.75",
        "4",
        "3.75",
    ]
