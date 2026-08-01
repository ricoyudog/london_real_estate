from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from nan_fung.ingestion.bank_rate import AcquiredArtifact
from nan_fung.operational import OperationalStore
from nan_fung.projections import ThresholdAlertRule, deliver_canonical_projections
from nan_fung.projections import delivery as delivery_module
from nan_fung.storage.db import connect_database
from nan_fung.workflows import ingest_bank_rate_artifact


ANCHOR = datetime(2026, 8, 1, 12, tzinfo=UTC)


def _artifact(value: str) -> AcquiredArtifact:
    return AcquiredArtifact(
        body=f"DATE,IUDBEDR\n31 Jul 2026,{value}\n".encode(),
        source_url="https://www.bankofengland.co.uk/data.csv",
        retrieved_at=ANCHOR - timedelta(minutes=2),
    )


def _seed_store(tmp_path: Path) -> OperationalStore:
    store = OperationalStore(tmp_path / "data")
    ingest_bank_rate_artifact(
        store,
        _artifact("3.75"),
        execution_at=ANCHOR - timedelta(minutes=1),
        isolate_parser=False,
    )
    ingest_bank_rate_artifact(
        store,
        _artifact("9.99"),
        lane="source_discovery",
        execution_at=ANCHOR - timedelta(minutes=1),
        isolate_parser=False,
    )
    return store


def _rules() -> tuple[ThresholdAlertRule, ...]:
    return (
        ThresholdAlertRule(
            rule_id="bank-rate-high",
            field="bank_rate_percent",
            comparator="gte",
            threshold="3.5",
            match={"projection_kind": "metrics"},
        ),
    )


def test_delivery_is_deterministic_auditable_and_canonical_only(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    output_directory = tmp_path / "published"

    first = deliver_canonical_projections(
        store.database_path,
        output_directory,
        as_of_at=ANCHOR,
        alert_rules=_rules(),
    )
    first_contents = {
        artifact.artifact_type: artifact.path.read_bytes() for artifact in first.artifacts
    }
    second = deliver_canonical_projections(
        store.database_path,
        output_directory,
        as_of_at=ANCHOR,
        alert_rules=_rules(),
    )

    assert first.as_json() == second.as_json()
    assert first.delivery_id == second.delivery_id
    assert first.rebuild.metric_count == 1
    assert [artifact.artifact_type for artifact in first.artifacts] == [
        "wiki",
        "daily",
        "weekly",
        "alerts",
    ]
    assert {artifact.path.relative_to(output_directory) for artifact in first.artifacts} == {
        Path("wiki/market.md"),
        Path("daily/2026-08-01.json"),
        Path("weekly/2026-W31.json"),
        Path("alerts/2026-08-01.json"),
    }
    assert all(len(artifact.source_hash) == 64 for artifact in first.artifacts)
    assert all("observation_ids" in artifact.details for artifact in first.artifacts[:3])
    assert first.alert_count == 1
    assert first.audit_path.is_file()
    assert first.audit_path.relative_to(output_directory) == Path(
        "audit"
    ) / f"{first.delivery_id}.json"

    all_published = b"\n".join(first_contents.values()) + first.audit_path.read_bytes()
    assert b"3.75" in all_published
    assert b"9.99" not in all_published
    assert b"source_discovery" not in all_published
    assert json.loads(first.audit_path.read_text())["canonical_only"] is True
    assert json.loads(first_contents["alerts"])["alerts"][0]["rule_id"] == "bank-rate-high"


def test_failed_atomic_publish_keeps_existing_artifact_and_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _seed_store(tmp_path)
    output_directory = tmp_path / "published"
    baseline = deliver_canonical_projections(
        store.database_path,
        output_directory,
        as_of_at=ANCHOR,
        alert_rules=_rules(),
    )
    wiki_path = next(
        artifact.path for artifact in baseline.artifacts if artifact.artifact_type == "wiki"
    )
    original_wiki = wiki_path.read_bytes()
    original_audit = baseline.audit_path.read_bytes()
    real_replace = delivery_module.os.replace

    def fail_wiki(source: str | Path, target: str | Path) -> None:
        if Path(target) == wiki_path:
            raise OSError("simulated publish failure")
        real_replace(source, target)

    monkeypatch.setattr(delivery_module.os, "replace", fail_wiki)

    with pytest.raises(OSError, match="simulated publish failure"):
        deliver_canonical_projections(
            store.database_path,
            output_directory,
            as_of_at=ANCHOR,
            alert_rules=_rules(),
        )

    assert wiki_path.read_bytes() == original_wiki
    assert baseline.audit_path.read_bytes() == original_audit
    assert not list(wiki_path.parent.glob(".market.md.*.tmp"))


def test_operational_publish_records_output_and_alert_lineage_idempotently(tmp_path: Path) -> None:
    store = _seed_store(tmp_path)
    output_directory = tmp_path / "published"

    first = store.publish_projections(
        output_directory,
        as_of_at=ANCHOR,
        alert_rules=_rules(),
        actor_id="operator",
    )
    second = store.publish_projections(
        output_directory,
        as_of_at=ANCHOR,
        alert_rules=_rules(),
        actor_id="operator",
    )
    connection = connect_database(store.database_path, read_only=True)
    try:
        outputs = connection.execute(
            "SELECT output_type FROM output_artifact ORDER BY output_type"
        ).fetchall()
        alerts = connection.execute(
            "SELECT alert_id, state FROM operational_alert"
        ).fetchall()
        audits = connection.execute(
            "SELECT action FROM audit_event WHERE action = 'projection_delivery_recorded'"
        ).fetchall()
    finally:
        connection.close()

    assert first.delivery_id == second.delivery_id
    assert [row["output_type"] for row in outputs] == [
        "projection_alerts",
        "projection_audit",
        "projection_daily",
        "projection_weekly",
        "projection_wiki",
    ]
    assert [(row["alert_id"], row["state"]) for row in alerts] == [
        (first.artifacts[-1].details["alert_ids"][0], "open")
    ]
    assert len(audits) == 1
