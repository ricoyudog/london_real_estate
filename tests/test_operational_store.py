from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from nan_fung.ingestion.registry import DatasourceRegistry, default_registry
from nan_fung.operational import OperationalError, OperationalStore
from nan_fung.ingestion.bank_rate import AcquiredArtifact, BankRateError
from nan_fung.read_api.access import ReadContext
from nan_fung.read_api.contracts import ReadQuery
from nan_fung.read_api.sqlite_repository import SQLiteReadRepository
from nan_fung.storage.db import connect_database
from nan_fung.workflows import ingest_bank_rate_artifact, reparse_bank_rate_evidence


_BANK_RATE_URL = "https://www.bankofengland.co.uk/boeapps/database/offline.csv"


def test_registry_job_evidence_observation_and_promotion_lifecycle(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)

    synced = store.sync_registry()
    first = store.enqueue("boe.bank_rate.iudbedr")
    duplicate = store.enqueue("boe.bank_rate.iudbedr")
    claim = store.claim_next("worker-a")
    assert claim is not None
    run = store.start_run(claim, "worker-a")
    evidence = store.persist_evidence(
        run,
        b"DATE,IUDBEDR\r\n30 Jul 2026,3.75\r\n",
        media_type="text/csv",
        request={"method": "GET", "url": _BANK_RATE_URL, "series": "IUDBEDR"},
        response={"status": 200, "final_url": _BANK_RATE_URL},
    )
    observation_id = store.persist_observation(
        run,
        record_key=("IUDBEDR", "2026-07-30"),
        payload={"date": "2026-07-30", "bank_rate_percent": "3.75"},
        record_type="metric",
        category="interest-rates-monetary-policy",
        evidence=(evidence,),
        source_date="2026-07-30",
        unit="percent",
        definition_text="Official Bank Rate",
    )
    store.finish_run(run, status="succeeded", promote=True)

    assert synced["definitions_inserted"] >= 13
    assert duplicate.job_id == first.job_id
    assert duplicate.disposition == "deduplicated"
    assert store.verify_evidence()["ok"]
    assert observation_id.startswith("obs_")
    records = tuple(
        SQLiteReadRepository(store.database_path).query_canonical(
            ReadQuery("metrics"),
            as_of=datetime(2099, 8, 1, tzinfo=UTC),
            context=ReadContext("dashboard", frozenset({"open"})),
        )
    )
    assert [(record.observation_id, record.payload["bank_rate_percent"]) for record in records] == [
        (observation_id, "3.75")
    ]


def test_new_definition_version_creates_a_revision_for_stricter_read_access(
    tmp_path: Path,
) -> None:
    seed = default_registry()
    version_one = seed.lookup("boe.bank_rate.iudbedr")
    version_two = replace(
        version_one,
        definition_version=2,
        access_class="internal",
    )
    store = OperationalStore(
        tmp_path,
        registry=DatasourceRegistry((version_one, version_two), seed.sources),
    )
    first_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    second_at = datetime(2026, 8, 2, 12, tzinfo=UTC)
    body = b"DATE,IUDBEDR\n31 Jul 2026,3.75\n"

    first = ingest_bank_rate_artifact(
        store,
        AcquiredArtifact(body, _BANK_RATE_URL, first_at),
        definition_version=1,
        execution_at=first_at,
        isolate_parser=False,
    )
    second = ingest_bank_rate_artifact(
        store,
        AcquiredArtifact(body, _BANK_RATE_URL, second_at),
        definition_version=2,
        execution_at=second_at,
        isolate_parser=False,
    )
    reader = SQLiteReadRepository(store.database_path)

    assert first.observation_ids != second.observation_ids
    assert [
        record.observation_id
        for record in reader.query_canonical(
            ReadQuery("metrics"),
            as_of=first_at + timedelta(seconds=1),
            context=ReadContext("open", frozenset({"open"})),
        )
    ] == list(first.observation_ids)
    assert tuple(
        reader.query_canonical(
            ReadQuery("metrics"),
            as_of=second_at + timedelta(seconds=1),
            context=ReadContext("open", frozenset({"open"})),
        )
    ) == ()
    assert [
        record.observation_id
        for record in reader.query_canonical(
            ReadQuery("metrics"),
            as_of=second_at + timedelta(seconds=1),
            context=ReadContext("internal", frozenset({"internal"})),
        )
    ] == list(second.observation_ids)


def test_persist_evidence_reuses_a_verified_stored_artifact_without_put_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OperationalStore(tmp_path)
    queued = store.enqueue("boe.bank_rate.iudbedr")
    claim = store.claim_job(queued.job_id, "worker")
    assert claim is not None
    run = store.start_run(claim, "worker")
    payload = b"DATE,IUDBEDR\n30 Jul 2026,3.75\n"
    artifact = store.artifacts.put_stream(BytesIO(payload), media_type="text/csv")
    monkeypatch.setattr(
        store.artifacts,
        "put_bytes",
        lambda *_args, **_kwargs: pytest.fail("stored evidence must not be written twice"),
    )

    evidence = store.persist_evidence(
        run,
        artifact=artifact,
        media_type="text/csv",
        request={"method": "GET", "url": _BANK_RATE_URL},
        response={"status": 200, "final_url": _BANK_RATE_URL},
    )

    assert evidence.artifact == artifact
    assert store.read_evidence(evidence) == payload


def test_evidence_verification_reports_unreferenced_cas_objects(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    store.migrate()
    orphan = store.artifacts.put_bytes(b"crash-window artifact")

    verification = store.verify_evidence()

    assert verification["unreferenced"] == [orphan.content_sha256]
    assert store.health()["unreferenced_artifacts"] == [orphan.content_sha256]


@pytest.mark.parametrize(
    "request_url, final_url, status, error",
    (
        (
            "https://evil.example/not-bank-rate.csv",
            "https://evil.example/not-bank-rate.csv",
            200,
            "unapproved source provenance",
        ),
        (
            "https://operator:secret@www.bankofengland.co.uk/not-bank-rate.csv",
            "https://www.bankofengland.co.uk/not-bank-rate.csv",
            200,
            "unapproved source provenance",
        ),
        (_BANK_RATE_URL, _BANK_RATE_URL, 500, "successful HTTP response"),
    ),
)
def test_evidence_boundary_rejects_forged_provenance_before_cas_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_url: str,
    final_url: str,
    status: int,
    error: str,
) -> None:
    store = OperationalStore(tmp_path)
    queued = store.enqueue("boe.bank_rate.iudbedr")
    claim = store.claim_job(queued.job_id, "worker")
    assert claim is not None
    run = store.start_run(claim, "worker")

    def fail_if_called(*_args: object, **_kwargs: object) -> object:
        pytest.fail("unapproved provenance must not enter the evidence store")

    monkeypatch.setattr(store.artifacts, "put_bytes", fail_if_called)
    with pytest.raises(OperationalError, match=error):
        store.persist_evidence(
            run,
            b"forged evidence",
            request={"method": "GET", "url": request_url},
            response={"status": status, "final_url": final_url},
        )


def test_evidence_rejects_unapproved_retention_before_cas_write(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    queued = store.enqueue("boe.mpc_news")
    claim = store.claim_job(queued.job_id, "worker")
    assert claim is not None
    run = store.start_run(claim, "worker")

    with pytest.raises(OperationalError, match="approved retention deadline"):
        store.persist_evidence(
            run,
            b"<rss/>",
            media_type="application/xml",
            request={"method": "GET", "url": "https://www.bankofengland.co.uk/rss/news"},
            response={"status": 200, "final_url": "https://www.bankofengland.co.uk/rss/news"},
        )

    assert not (tmp_path / "evidence").exists()


def test_failed_run_preserves_the_last_promoted_value(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    store.sync_registry()
    first = store.enqueue("boe.bank_rate.iudbedr", request={"window": "first"})
    first_claim = store.claim_next("worker-a")
    assert first_claim is not None and first_claim.job_id == first.job_id
    first_run = store.start_run(first_claim, "worker-a")
    evidence = store.persist_evidence(
        first_run,
        b"first",
        request={"method": "GET", "url": _BANK_RATE_URL},
        response={"status": 200, "final_url": _BANK_RATE_URL},
    )
    observation_id = store.persist_observation(
        first_run,
        record_key=("IUDBEDR", "2026-07-30"),
        payload={"bank_rate_percent": "3.75"},
        record_type="metric",
        category="macro",
        evidence=(evidence,),
    )
    store.finish_run(first_run, status="succeeded", promote=True)

    store.enqueue("boe.bank_rate.iudbedr", request={"window": "second"})
    second_claim = store.claim_next("worker-a")
    assert second_claim is not None
    second_run = store.start_run(second_claim, "worker-a")
    store.persist_evidence(
        second_run,
        b"bad response",
        request={"method": "GET", "url": _BANK_RATE_URL},
        response={"status": 200, "final_url": _BANK_RATE_URL},
    )
    store.finish_run(
        second_run,
        status="failed",
        error={"code": "PARSE_INVALID", "retryable": False},
    )

    records = tuple(
        SQLiteReadRepository(store.database_path).query_canonical(
            ReadQuery("metrics"),
            as_of=datetime.now(UTC),
            context=ReadContext("dashboard", frozenset({"open"})),
        )
    )
    assert [record.observation_id for record in records] == [observation_id]


def test_observation_rejects_evidence_from_another_running_lifecycle(
    tmp_path: Path,
) -> None:
    store = OperationalStore(tmp_path)
    bank_rate_job = store.enqueue("boe.bank_rate.iudbedr")
    bank_rate_claim = store.claim_job(bank_rate_job.job_id, "worker")
    assert bank_rate_claim is not None
    bank_rate_run = store.start_run(bank_rate_claim, "worker")
    bank_rate_evidence = store.persist_evidence(
        bank_rate_run,
        b"DATE,IUDBEDR\n31 Jul 2026,3.75\n",
        media_type="text/csv",
        request={"method": "GET", "url": _BANK_RATE_URL},
        response={"status": 200, "final_url": _BANK_RATE_URL},
    )
    store.finish_run(bank_rate_run, status="failed")
    macro_job = store.enqueue("ons.gdp.ecyx")
    macro_claim = store.claim_job(macro_job.job_id, "worker")
    assert macro_claim is not None
    macro_run = store.start_run(macro_claim, "worker")

    with pytest.raises(OperationalError, match="attached to its ingestion run"):
        store.persist_observation(
            macro_run,
            record_key=("ECYX", "2026 JUN"),
            payload={"value": "2.6"},
            record_type="metric",
            category="macro",
            evidence=(bank_rate_evidence,),
        )

    connection = connect_database(store.database_path, read_only=True)
    try:
        linked = connection.execute(
            """
            SELECT 1 FROM observation_evidence
            WHERE run_id = ? AND evidence_id = ?
            """,
            (macro_run.run_id, bank_rate_evidence.evidence_id),
        ).fetchone()
    finally:
        connection.close()
    assert linked is None


def test_bank_rate_fixture_uses_saved_evidence_and_isolated_parser(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    artifact = AcquiredArtifact(
        body=b"DATE,IUDBEDR\r\n30 Jul 2026,3.75\r\n",
        source_url="https://www.bankofengland.co.uk/boeapps/database/example.csv",
        retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
    )

    result = ingest_bank_rate_artifact(store, artifact)

    assert result.status == "succeeded"
    assert result.canonical_changed is True
    assert store.read_evidence(result.evidence_id) == artifact.body
    records = tuple(
        SQLiteReadRepository(store.database_path).query_canonical(
            ReadQuery("metrics"),
            as_of=datetime(2099, 1, 1, tzinfo=UTC),
            context=ReadContext("dashboard", frozenset({"open"})),
        )
    )
    assert [record.payload["bank_rate_percent"] for record in records] == ["3.75"]


def test_bank_rate_fixture_uses_one_injected_execution_clock(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    execution_at = datetime(2020, 1, 1, 12, tzinfo=UTC)
    artifact = AcquiredArtifact(
        body=b"DATE,IUDBEDR\n31 Dec 2019,0.75\n",
        source_url="https://www.bankofengland.co.uk/boeapps/database/offline.csv",
        retrieved_at=execution_at,
    )

    result = ingest_bank_rate_artifact(
        store,
        artifact,
        execution_at=execution_at,
    )

    assert result.status == "succeeded"


def test_promotion_revocation_preserves_as_of_history(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    approved_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    revoked_at = datetime(2026, 8, 2, 12, tzinfo=UTC)
    result = ingest_bank_rate_artifact(
        store,
        AcquiredArtifact(
            body=b"DATE,IUDBEDR\n31 Jul 2026,3.75\n",
            source_url=_BANK_RATE_URL,
            retrieved_at=approved_at,
        ),
        execution_at=approved_at,
    )
    reader = SQLiteReadRepository(store.database_path)
    context = ReadContext("dashboard", frozenset({"open"}))
    before = tuple(
        reader.query_canonical(
            ReadQuery("metrics"), as_of=approved_at, context=context
        )
    )

    revocation = store.revoke_promotion(
        result.run_id, actor_id="operator", reason="superseded source", now=revoked_at
    )
    repeated = store.revoke_promotion(
        result.run_id, actor_id="operator", now=revoked_at
    )
    after = tuple(
        reader.query_canonical(
            ReadQuery("metrics"), as_of=revoked_at, context=context
        )
    )
    historical = tuple(
        reader.query_canonical(
            ReadQuery("metrics"), as_of=approved_at, context=context
        )
    )
    connection = connect_database(store.database_path, read_only=True)
    try:
        decisions = connection.execute(
            "SELECT decision FROM run_promotion WHERE run_id = ? ORDER BY promotion_seq",
            (result.run_id,),
        ).fetchall()
    finally:
        connection.close()

    assert [record.observation_id for record in before] == list(result.observation_ids)
    assert revocation.created is True
    assert repeated.created is False
    assert repeated.promotion_id == revocation.promotion_id
    assert after == ()
    assert [record.observation_id for record in historical] == list(result.observation_ids)
    assert [row["decision"] for row in decisions] == ["approved", "revoked"]


def test_bank_rate_reparse_rejects_evidence_from_another_datasource(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    manual = store.import_manual_evidence(
        "rightmove.commercial_insights_tracker",
        b"manual tracker note",
        media_type="text/plain",
        attestation="licensed manual evidence",
        retention_until=datetime(2030, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(OperationalError, match="not bound to Bank Rate provenance"):
        reparse_bank_rate_evidence(store, manual.evidence_id)


def test_bank_rate_reparse_preserves_the_captured_definition_version(tmp_path: Path) -> None:
    seed = default_registry()
    version_one = seed.lookup("boe.bank_rate.iudbedr")
    version_two = replace(version_one, definition_version=2)
    store = OperationalStore(
        tmp_path,
        registry=DatasourceRegistry(
            (version_one, version_two), (seed.lookup_source("boe.iadb"),)
        ),
    )
    original = ingest_bank_rate_artifact(
        store,
        AcquiredArtifact(
            body=b"DATE,IUDBEDR\n30 Jul 2026,3.75\n",
            source_url=_BANK_RATE_URL,
            retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        definition_version=2,
    )

    replay = reparse_bank_rate_evidence(store, original.evidence_id)

    connection = connect_database(store.database_path, read_only=True)
    try:
        lineage = connection.execute(
            """
            SELECT r.definition_version, j.trigger, j.job_kind, j.request_json
            FROM ingestion_run AS r
            JOIN workflow_job AS j ON j.job_id = r.job_id
            WHERE r.run_id = ?
            """,
            (replay.run_id,),
        ).fetchone()
    finally:
        connection.close()
    assert lineage is not None
    assert lineage["definition_version"] == 2
    assert lineage["trigger"] == "reparse"
    assert lineage["job_kind"] == "offline_reparse"
    assert json.loads(lineage["request_json"]) == {
        "reparse_evidence_id": original.evidence_id
    }


def test_bank_rate_refuses_a_definition_with_changed_executable_bindings(
    tmp_path: Path,
) -> None:
    seed = default_registry()
    version_one = seed.lookup("boe.bank_rate.iudbedr")
    changed = replace(version_one, definition_version=2, parser_version="v2")
    store = OperationalStore(
        tmp_path,
        registry=DatasourceRegistry(
            (version_one, changed), (seed.lookup_source("boe.iadb"),)
        ),
    )

    with pytest.raises(BankRateError, match="new bound lifecycle"):
        ingest_bank_rate_artifact(
            store,
            AcquiredArtifact(
                body=b"DATE,IUDBEDR\n30 Jul 2026,3.75\n",
                source_url=_BANK_RATE_URL,
                retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
            definition_version=2,
        )

    assert not (tmp_path / "evidence").exists()


def test_bank_rate_refuses_a_definition_with_changed_source_binding(tmp_path: Path) -> None:
    seed = default_registry()
    version_one = seed.lookup("boe.bank_rate.iudbedr")
    changed = replace(
        version_one,
        definition_version=2,
        source_bindings=(replace(version_one.source_bindings[0], role="supporting"),),
    )
    store = OperationalStore(
        tmp_path,
        registry=DatasourceRegistry(
            (version_one, changed), (seed.lookup_source("boe.iadb"),)
        ),
    )

    with pytest.raises(BankRateError, match="new bound lifecycle"):
        ingest_bank_rate_artifact(
            store,
            AcquiredArtifact(
                body=b"DATE,IUDBEDR\n30 Jul 2026,3.75\n",
                source_url=_BANK_RATE_URL,
                retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
            definition_version=2,
        )

    assert not (tmp_path / "evidence").exists()


@pytest.mark.parametrize("lane", ("source_discovery", "ad_hoc_research"))
def test_bank_rate_reparse_inherits_its_captured_nonproduction_lane(
    tmp_path: Path, lane: str
) -> None:
    store = OperationalStore(tmp_path)
    original = ingest_bank_rate_artifact(
        store,
        AcquiredArtifact(
            body=b"DATE,IUDBEDR\n30 Jul 2026,3.75\n",
            source_url="https://www.bankofengland.co.uk/data.csv",
            retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        lane=lane,
    )

    replay = reparse_bank_rate_evidence(store, original.evidence_id)

    assert replay.canonical_changed is False
    with pytest.raises(OperationalError, match="must match the source lane"):
        reparse_bank_rate_evidence(
            store, original.evidence_id, lane="production_ingestion"
        )


def test_full_snapshot_tombstone_requires_scope_and_completeness_proof(tmp_path: Path) -> None:
    seed = default_registry()
    definition = replace(seed.lookup("boe.bank_rate.iudbedr"), snapshot_mode="full_snapshot")
    store = OperationalStore(
        tmp_path,
        registry=DatasourceRegistry((definition,), (seed.lookup_source("boe.iadb"),)),
    )
    store.sync_registry()

    first = store.enqueue("boe.bank_rate.iudbedr", request={"run": "first"})
    first_claim = store.claim_job(first.job_id, "worker")
    assert first_claim is not None
    first_run = store.start_run(first_claim, "worker")
    first_evidence = store.persist_evidence(
        first_run,
        b"first",
        request={"method": "GET", "url": _BANK_RATE_URL},
        response={"status": 200, "final_url": _BANK_RATE_URL},
    )
    first_scope = store.begin_full_snapshot(first_run, scope={"series": "IUDBEDR"})
    first_observation = store.persist_observation(
        first_run,
        record_key=("IUDBEDR", "2026-07-30"),
        payload={"bank_rate_percent": "3.75"},
        record_type="metric",
        category="macro",
        evidence=(first_evidence,),
    )
    assert store.finalize_full_snapshot(
        first_run,
        completeness_proof={
            "schema_version": "snapshot_proof.v1",
            "snapshot_scope_hash": first_scope,
            "complete": True,
            "count": 1,
        },
    ) == ()
    store.finish_run(first_run, status="succeeded", promote=True)

    second = store.enqueue("boe.bank_rate.iudbedr", request={"run": "second"})
    second_claim = store.claim_job(second.job_id, "worker")
    assert second_claim is not None
    second_run = store.start_run(second_claim, "worker")
    store.persist_evidence(
        second_run,
        b"empty but complete",
        request={"method": "GET", "url": _BANK_RATE_URL},
        response={"status": 200, "final_url": _BANK_RATE_URL},
    )
    second_scope = store.begin_full_snapshot(second_run, scope={"series": "IUDBEDR"})
    tombstones = store.finalize_full_snapshot(
        second_run,
        completeness_proof={
            "schema_version": "snapshot_proof.v1",
            "snapshot_scope_hash": second_scope,
            "complete": True,
            "count": 0,
        },
    )
    store.finish_run(second_run, status="succeeded", promote=True)

    records = tuple(
        SQLiteReadRepository(store.database_path).query_canonical(
            ReadQuery("metrics"),
            as_of=datetime(2099, 1, 1, tzinfo=UTC),
            context=ReadContext("dashboard", frozenset({"open"})),
        )
    )
    assert first_observation not in {record.observation_id for record in records}
    assert len(tombstones) == 1


def test_full_snapshot_rejects_unvalidated_or_wrong_scope_proof(tmp_path: Path) -> None:
    seed = default_registry()
    definition = replace(seed.lookup("boe.bank_rate.iudbedr"), snapshot_mode="full_snapshot")
    store = OperationalStore(
        tmp_path,
        registry=DatasourceRegistry((definition,), (seed.lookup_source("boe.iadb"),)),
    )
    queued = store.enqueue("boe.bank_rate.iudbedr")
    claim = store.claim_job(queued.job_id, "worker")
    assert claim is not None
    run = store.start_run(claim, "worker")
    store.persist_evidence(
        run,
        b"complete",
        request={"method": "GET", "url": _BANK_RATE_URL},
        response={"status": 200, "final_url": _BANK_RATE_URL},
    )
    store.begin_full_snapshot(run, scope={"series": "IUDBEDR"})

    with pytest.raises(RuntimeError, match="exact scope"):
        store.finalize_full_snapshot(
            run, completeness_proof={"schema_version": "snapshot_proof.v1", "complete": True}
        )
