from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess
import sys

import pytest

from nan_fung.ingestion.registry import DatasourceRegistry, default_registry
from nan_fung.operational import OperationalError, OperationalStore
from nan_fung.refresh_api import (
    InvalidRefreshRequest,
    OperationalRefreshBackend,
    RefreshAccessDenied,
    RefreshBroker,
    RefreshContext,
    RefreshDisposition,
    RefreshProfile,
    RefreshRequest,
)
from nan_fung.storage.db import connect_database


def test_scheduler_materializes_only_runtime_bound_bank_rate_schedule(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    store.sync_registry()
    now = datetime(2026, 8, 3, 20, tzinfo=UTC)  # Monday after the 19:00 UK slot.
    connection = connect_database(store.database_path)
    try:
        connection.execute(
            """
            UPDATE workflow_schedule SET cursor_at = ?
            WHERE datasource_id = 'boe.bank_rate.iudbedr'
            """,
            ("2026-08-03T17:59:00.000000Z",),
        )
    finally:
        connection.close()

    report = store.scheduler_tick(now=now)

    rows = [item for item in report["schedules"] if item["datasource_id"] == "boe.bank_rate.iudbedr"]
    assert rows[0]["accepted"] == 1
    assert any(item["reason"] == "runtime_unbound" for item in report["blocked"])


def test_writer_session_rejects_another_process_but_permits_read_health(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    store.sync_registry()
    writer = "\n".join(
        (
            "from nan_fung.operational import OperationalStore, WriterAlreadyRunningError",
            f"store = OperationalStore({str(tmp_path)!r})",
            "try:",
            "    store.enqueue('boe.bank_rate.iudbedr')",
            "except WriterAlreadyRunningError:",
            "    raise SystemExit(0)",
            "raise SystemExit(1)",
        )
    )
    reader = "\n".join(
        (
            "from nan_fung.operational import OperationalStore",
            f"store = OperationalStore({str(tmp_path)!r})",
            "raise SystemExit(0 if store.health()['state'] == 'ready' else 1)",
        )
    )

    with store.writer_session():
        blocked = subprocess.run(
            (sys.executable, "-c", writer),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        readable = subprocess.run(
            (sys.executable, "-c", reader),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    assert blocked.returncode == 0, blocked.stderr
    assert readable.returncode == 0, readable.stderr
    assert store.jobs() == ()


def test_scheduler_uses_its_injected_clock_when_bootstrapping_registry(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    bootstrap_at = datetime(2020, 1, 7, 18, tzinfo=UTC)
    store.sync_registry(now=bootstrap_at)

    report = store.scheduler_tick(now=datetime(2020, 1, 7, 20, tzinfo=UTC))

    bank_rate = next(
        item
        for item in report["schedules"]
        if item["datasource_id"] == "boe.bank_rate.iudbedr"
    )
    assert bank_rate["accepted"] == 1


def test_registry_status_is_read_only_on_a_fresh_store(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)

    status = store.registry_status()

    assert len(status) == len(store.registry.definitions)
    assert not store.database_path.exists()
    assert len(store.registry_diff()["missing_in_store"]) == len(store.registry.definitions)


def test_scheduler_pauses_an_invalid_schedule_after_its_first_error(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    store.sync_registry()
    connection = connect_database(store.database_path)
    try:
        schedule = connection.execute(
            """
            SELECT schedule_id FROM workflow_schedule
            WHERE datasource_id = 'boe.bank_rate.iudbedr'
            """
        ).fetchone()
        assert schedule is not None
        connection.execute(
            "UPDATE workflow_schedule SET rule_json = ? WHERE schedule_id = ?",
            ('{"kind":"invalid"}', schedule["schedule_id"]),
        )
    finally:
        connection.close()

    first = store.scheduler_tick(now=datetime(2026, 8, 3, 20, tzinfo=UTC))
    second = store.scheduler_tick(now=datetime(2026, 8, 3, 21, tzinfo=UTC))

    assert [item["code"] for item in first["errors"]] == ["SCHEDULE_INVALID"]
    assert second["errors"] == []
    connection = connect_database(store.database_path, read_only=True)
    try:
        paused = connection.execute(
            "SELECT paused_reason FROM workflow_schedule WHERE schedule_id = ?",
            (schedule["schedule_id"],),
        ).fetchone()
    finally:
        connection.close()
    assert paused["paused_reason"] == "SCHEDULE_INVALID"


def test_expired_running_lease_is_retried_with_new_claim_token(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    now = datetime(2026, 8, 1, 12, tzinfo=UTC)
    queued = store.enqueue("boe.bank_rate.iudbedr", scheduled_for=now)
    claim = store.claim_job(queued.job_id, "worker", now=now, lease_seconds=1)
    assert claim is not None
    store.start_run(claim, "worker", now=now)

    recovered = store.recover_expired(now=now + timedelta(seconds=2))

    assert recovered == (queued.job_id,)
    assert store.get_job(queued.job_id)["state"] == "retry_wait"  # type: ignore[index]


def test_expired_projection_system_job_is_recovered_for_retry(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    now = datetime(2026, 8, 1, 12, tzinfo=UTC)
    queued = store.enqueue_projection_delivery(tmp_path / "published", as_of_at=now)
    claim = store.claim_job(queued.job_id, "worker", now=now, lease_seconds=1)
    assert claim is not None
    store.start_system_job(claim, "worker", now=now)

    recovered = store.recover_expired(now=now + timedelta(seconds=2))

    assert recovered == (queued.job_id,)
    assert store.get_job(queued.job_id)["state"] == "retry_wait"  # type: ignore[index]


def test_refresh_backend_enqueues_only_profile_selected_job(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    backend = OperationalRefreshBackend(store)
    broker = RefreshBroker(
        {
            "bank-rate": RefreshProfile(
                profile_id="bank-rate",
                datasource_id="boe.bank_rate.iudbedr",
                definition_version=1,
                effective_lane="production_ingestion",
                allowed_scope_keys=frozenset({"period"}),
            )
        },
        backend,
        clock=lambda: datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    acknowledgement = broker.request(
        RefreshContext("agent", "request-1", frozenset({"bank-rate"})),
        RefreshRequest(
            datasource_id="boe.bank_rate.iudbedr",
            request_profile="bank-rate",
            bounded_scope={"period": "latest"},
        ),
    )

    assert acknowledgement.job_id is not None
    status = backend.get_status(acknowledgement.job_id, principal="agent")
    assert status is not None
    assert status.job_state == "queued"


def test_refresh_ledger_enforces_restart_idempotency_cooldown_and_visibility(
    tmp_path: Path,
) -> None:
    store = OperationalStore(tmp_path)
    current = [datetime(2026, 8, 1, 12, tzinfo=UTC)]

    def broker() -> RefreshBroker:
        return RefreshBroker(
            {
                "bank-rate": RefreshProfile(
                    profile_id="bank-rate",
                    datasource_id="boe.bank_rate.iudbedr",
                    definition_version=1,
                    effective_lane="production_ingestion",
                    allowed_scope_keys=frozenset({"period"}),
                )
            },
            OperationalRefreshBackend(store),
            clock=lambda: current[0],
        )

    request = RefreshRequest(
        datasource_id="boe.bank_rate.iudbedr",
        request_profile="bank-rate",
        bounded_scope={"period": "latest"},
    )
    first = broker().request(
        RefreshContext("alice", "request-1", frozenset({"bank-rate"})), request
    )
    assert first.job_id is not None

    current[0] += timedelta(minutes=1)
    restarted = broker()
    replay = restarted.request(
        RefreshContext("alice", "request-1", frozenset({"bank-rate"})), request
    )
    assert replay == first
    with pytest.raises(RefreshAccessDenied, match="another principal"):
        restarted.request(
            RefreshContext("bob", "request-1", frozenset({"bank-rate"})), request
        )
    with pytest.raises(InvalidRefreshRequest, match="different request"):
        restarted.request(
            RefreshContext("alice", "request-1", frozenset({"bank-rate"})),
            RefreshRequest(
                datasource_id="boe.bank_rate.iudbedr",
                request_profile="bank-rate",
                bounded_scope={"period": "historical"},
            ),
        )

    deduplicated = restarted.request(
        RefreshContext("bob", "request-2", frozenset({"bank-rate"})), request
    )
    assert deduplicated.disposition is RefreshDisposition.DEDUPLICATED
    assert deduplicated.job_id == first.job_id
    assert restarted.get_status(
        RefreshContext("bob", "status-2", frozenset({"bank-rate"})), first.job_id
    ) is not None
    assert broker().get_status(
        RefreshContext("alice", "status-1", frozenset({"bank-rate"})), first.job_id
    ) is not None
    with pytest.raises(RefreshAccessDenied, match="not visible"):
        broker().get_status(
            RefreshContext("mallory", "status-3", frozenset({"bank-rate"})),
            first.job_id,
        )


def test_onspd_agent_refresh_daily_cap_requires_a_durable_second_confirmation(
    tmp_path: Path,
) -> None:
    store = OperationalStore(tmp_path)
    current = [datetime(2026, 8, 1, 22, 30, tzinfo=UTC)]  # 23:30 Europe/London.
    profile = RefreshProfile(
        profile_id="onspd-postcode",
        datasource_id="ons.onspd.postcode",
        definition_version=1,
        effective_lane="production_ingestion",
        allowed_scope_keys=frozenset({"postcode"}),
        required_scope_keys=frozenset({"postcode"}),
        single_value_scope_keys=frozenset({"postcode"}),
    )

    def broker() -> RefreshBroker:
        return RefreshBroker(
            {profile.profile_id: profile},
            OperationalRefreshBackend(store),
            clock=lambda: current[0],
        )

    def request(postcode: str, *, token: str | None = None) -> RefreshRequest:
        return RefreshRequest(
            datasource_id="ons.onspd.postcode",
            request_profile=profile.profile_id,
            bounded_scope={"postcode": postcode},
            confirmation_token=token,
        )

    first_job_id = None
    for index in range(20):
        acknowledgement = broker().request(
            RefreshContext(
                "competition-agent",
                f"onspd-{index}",
                frozenset({profile.profile_id}),
            ),
            request(f"EC2Y 5A{chr(ord('A') + index)}"),
        )
        assert acknowledgement.disposition is RefreshDisposition.ACCEPTED
        assert acknowledgement.job_id is not None
        first_job_id = first_job_id or acknowledgement.job_id

    assert first_job_id is not None
    assert broker().get_status(
        RefreshContext(
            "competition-agent", "onspd-status", frozenset({profile.profile_id})
        ),
        first_job_id,
    ) is not None
    deduplicated = broker().request(
        RefreshContext(
            "competition-agent", "onspd-deduplicated", frozenset({profile.profile_id})
        ),
        request("EC2Y 5AA"),
    )
    assert deduplicated.disposition is RefreshDisposition.DEDUPLICATED
    assert len(store.jobs()) == 20

    blocked_context = RefreshContext(
        "competition-agent", "onspd-20", frozenset({profile.profile_id})
    )
    blocked_request = request("EC2Y 5AZ")
    blocked = broker().request(blocked_context, blocked_request)

    assert blocked.disposition is RefreshDisposition.CONFIRMATION_REQUIRED
    assert blocked.job_id is None
    assert blocked.confirmation_token is not None
    assert blocked.confirmation_expires_at == current[0] + timedelta(minutes=10)
    assert len(store.jobs()) == 20

    restarted = broker()
    repeated_notice = restarted.request(blocked_context, blocked_request)
    assert repeated_notice == blocked
    with pytest.raises(InvalidRefreshRequest, match="confirmation is invalid"):
        restarted.request(
            blocked_context,
            request("EC2Y 5AZ", token="not-the-issued-confirmation"),
        )

    confirmed = restarted.request(
        blocked_context,
        request("EC2Y 5AZ", token=blocked.confirmation_token),
    )
    assert confirmed.disposition is RefreshDisposition.ACCEPTED
    assert confirmed.job_id is not None
    assert len(store.jobs()) == 21

    current[0] = datetime(2026, 8, 1, 23, 30, tzinfo=UTC)  # 00:30 next London day.
    reset = broker().request(
        RefreshContext(
            "competition-agent", "onspd-next-day", frozenset({profile.profile_id})
        ),
        request("EC2Y 5BA"),
    )
    assert reset.disposition is RefreshDisposition.ACCEPTED
    assert reset.job_id is not None


def test_refresh_dedupe_recovers_a_pre_ledger_crash_orphan(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    submitted_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    profile = RefreshProfile(
        profile_id="bank-rate",
        datasource_id="boe.bank_rate.iudbedr",
        definition_version=1,
        effective_lane="production_ingestion",
        allowed_scope_keys=frozenset({"period"}),
    )
    request = RefreshRequest(
        datasource_id="boe.bank_rate.iudbedr",
        request_profile="bank-rate",
        bounded_scope={"period": "latest"},
    )
    fingerprint = RefreshBroker._request_fingerprint(request)
    dedupe_key = RefreshBroker._dedupe_key(profile, request)
    definition = store.registry.lookup(request.datasource_id, profile.definition_version)
    orphan = store.enqueue(
        request.datasource_id,
        definition_version=profile.definition_version,
        lane=profile.effective_lane,
        trigger="agent_request",
        scheduled_for=submitted_at,
        request_instance_id="request-1",
        request={
            **dict(definition.default_request),
            "refresh_profile": profile.profile_id,
            "scope": {"period": ["latest"]},
            "intent": request.intent,
            "_refresh_control": {
                "principal": "alice",
                "request_fingerprint": fingerprint,
                "dedupe_key": dedupe_key,
                "submitted_at": submitted_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "cooldown_until": (submitted_at + timedelta(minutes=5)).strftime(
                    "%Y-%m-%dT%H:%M:%S.%fZ"
                ),
                "initial_state": "queued",
            },
        },
    )
    assert orphan.disposition == "accepted"

    now = submitted_at + timedelta(minutes=1)
    broker = RefreshBroker(
        {profile.profile_id: profile},
        OperationalRefreshBackend(store),
        clock=lambda: now,
    )
    deduplicated = broker.request(
        RefreshContext("bob", "request-2", frozenset({profile.profile_id})), request
    )
    recovered = broker.request(
        RefreshContext("alice", "request-1", frozenset({profile.profile_id})), request
    )

    assert deduplicated.disposition is RefreshDisposition.DEDUPLICATED
    assert deduplicated.job_id == orphan.job_id
    assert recovered.disposition is RefreshDisposition.ACCEPTED
    assert recovered.job_id == orphan.job_id
    assert len(store.jobs()) == 1


def test_refresh_profile_policy_mismatch_is_rejected_before_enqueue(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    broker = RefreshBroker(
        {
            "bank-rate": RefreshProfile(
                profile_id="bank-rate",
                datasource_id="boe.bank_rate.iudbedr",
                definition_version=1,
                effective_lane="production_ingestion",
                promotion_policy="manual_review",
            )
        },
        OperationalRefreshBackend(store),
        clock=lambda: datetime(2026, 8, 1, 12, tzinfo=UTC),
    )

    with pytest.raises(InvalidRefreshRequest, match="promotion policy"):
        broker.request(
            RefreshContext("agent", "request-1", frozenset({"bank-rate"})),
            RefreshRequest(
                datasource_id="boe.bank_rate.iudbedr",
                request_profile="bank-rate",
            ),
        )
    assert store.jobs() == ()


def test_backfill_persists_a_bounded_window(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2025, 2, 1, tzinfo=UTC)

    queued = store.enqueue_backfill(
        "boe.bank_rate.iudbedr", window_start=start, window_end=end
    )

    connection = connect_database(store.database_path, read_only=True)
    try:
        row = connection.execute(
            "SELECT job_kind, window_start, window_end FROM workflow_job WHERE job_id = ?",
            (queued.job_id,),
        ).fetchone()
    finally:
        connection.close()
    assert tuple(row) == (
        "backfill",
        "2025-01-01T00:00:00.000000Z",
        "2025-02-01T00:00:00.000000Z",
    )


def test_backfill_request_window_cannot_disagree_with_durable_window(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)

    with pytest.raises(OperationalError, match="durable job window"):
        store.enqueue(
            "boe.bank_rate.iudbedr",
            request={
                "window_start": "2025-02-01T00:00:00.000000Z",
                "window_end": "2025-02-02T00:00:00.000000Z",
            },
            trigger="backfill",
            job_kind="backfill",
            window_start=datetime(2025, 1, 1, tzinfo=UTC),
            window_end=datetime(2025, 1, 2, tzinfo=UTC),
        )


def test_retry_preserves_backfill_kind_window_and_original_definition_version(
    tmp_path: Path,
) -> None:
    seed = default_registry()
    version_one = seed.lookup("boe.bank_rate.iudbedr")
    version_two = replace(version_one, definition_version=2)
    store = OperationalStore(
        tmp_path,
        registry=DatasourceRegistry(
            (version_one, version_two), (seed.lookup_source("boe.iadb"),)
        ),
    )
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2025, 2, 1, tzinfo=UTC)
    original = store.enqueue(
        "boe.bank_rate.iudbedr",
        definition_version=1,
        request={
            "window_start": "2025-01-01T00:00:00.000000Z",
            "window_end": "2025-02-01T00:00:00.000000Z",
        },
        trigger="backfill",
        job_kind="backfill",
        window_start=start,
        window_end=end,
        scheduled_for=start,
    )
    original_claim = store.claim_job(
        original.job_id, "worker", now=datetime(2025, 3, 1, tzinfo=UTC)
    )
    assert original_claim is not None
    original_run = store.start_run(
        original_claim, "worker", now=datetime(2025, 3, 1, tzinfo=UTC)
    )
    store.finish_run(
        original_run, status="failed", now=datetime(2025, 3, 1, 0, 1, tzinfo=UTC)
    )

    retried = store.retry(original.job_id)
    retried_claim = store.claim_job(
        retried.job_id, "worker", now=datetime(2027, 1, 1, tzinfo=UTC)
    )

    assert retried_claim is not None
    assert retried_claim.definition_version == 1
    assert retried_claim.job_kind == "backfill"
    assert retried_claim.window_start == start
    assert retried_claim.window_end == end


def test_manual_report_evidence_creates_a_review_task_without_promotion(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    report = b"%PDF-1.7\n%%EOF\n"

    result = store.import_manual_evidence(
        "bnp.central_london_office_report",
        report,
        media_type="application/pdf",
        source_url="https://www.realestate.bnpparibas.co.uk/report.pdf",
        attestation="terms reviewed",
        retention_until=datetime(2030, 1, 1, tzinfo=UTC),
    )

    assert result.review_id is not None
    assert store.read_evidence(result.evidence_id) == report
    connection = connect_database(store.database_path, read_only=True)
    try:
        review = connection.execute(
            "SELECT state, run_id FROM review_task WHERE review_id = ?",
            (result.review_id,),
        ).fetchone()
    finally:
        connection.close()
    assert tuple(review) == ("open", result.run_id)
    assert store.decide_review(result.review_id, decision="approved", actor_id="reviewer")
    assert store.review_tasks(state="approved")[0]["review_id"] == result.review_id


def test_manual_evidence_provenance_rejection_closes_its_submission_run(
    tmp_path: Path,
) -> None:
    store = OperationalStore(tmp_path)

    with pytest.raises(OperationalError, match="approved network host"):
        store.import_manual_evidence(
            "custom.london_office_submarkets",
            b'{"name":"West End","locations":["Mayfair"]}',
            media_type="application/json",
            attestation="mapping checked",
            source_url="https://example.com/submarkets.json",
        )

    connection = connect_database(store.database_path, read_only=True)
    try:
        row = connection.execute(
            """
            SELECT job.job_kind, job.state, run.status
            FROM workflow_job AS job
            JOIN ingestion_run AS run ON run.job_id = job.job_id
            WHERE job.datasource_id = ?
            """,
            ("custom.london_office_submarkets",),
        ).fetchone()
        evidence_count = connection.execute(
            "SELECT COUNT(*) FROM evidence_artifact"
        ).fetchone()[0]
    finally:
        connection.close()

    assert tuple(row) == ("manual_submission", "failed", "failed")
    assert evidence_count == 0


def test_approved_manual_review_can_be_explicitly_promoted_once_with_audit(
    tmp_path: Path,
) -> None:
    store = OperationalStore(tmp_path)
    submitted = store.import_manual_evidence(
        "custom.london_office_submarkets",
        b'{"name":"West End","locations":["Mayfair"]}',
        media_type="application/json",
        attestation="mapping checked",
        actor_id="submitter",
    )
    assert submitted.review_id is not None

    with pytest.raises(OperationalError, match="approved"):
        store.promote_review(submitted.review_id, actor_id="operator")

    assert store.decide_review(submitted.review_id, decision="approved", actor_id="reviewer")
    promoted = store.promote_review(
        submitted.review_id,
        actor_id="operator",
        reason="mapping checked",
    )
    repeated = store.promote_review(submitted.review_id, actor_id="operator")

    assert promoted.created
    assert not repeated.created
    assert repeated.promotion_id == promoted.promotion_id
    connection = connect_database(store.database_path, read_only=True)
    try:
        promotion = connection.execute(
            """
            SELECT run_id, decision, approval_mode, actor_type, actor_id, reason
            FROM run_promotion WHERE promotion_id = ?
            """,
            (promoted.promotion_id,),
        ).fetchone()
        link = connection.execute(
            """
            SELECT review_id, run_id, promotion_id FROM manual_review_promotion
            WHERE review_id = ?
            """,
            (submitted.review_id,),
        ).fetchone()
        audit = connection.execute(
            """
            SELECT action, target_type, target_id FROM audit_event
            WHERE target_id = ?
            """,
            (promoted.promotion_id,),
        ).fetchone()
    finally:
        connection.close()
    assert tuple(promotion) == (
        submitted.run_id,
        "approved",
        "manual",
        "operator",
        "operator",
        "mapping checked",
    )
    assert tuple(link) == (submitted.review_id, submitted.run_id, promoted.promotion_id)
    assert tuple(audit) == ("manual_promotion_approved", "run_promotion", promoted.promotion_id)


def test_approved_review_cannot_promote_a_discovery_lane_run(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    submitted = store.import_manual_evidence(
        "bnp.central_london_office_report",
        b"%PDF-1.7\n%%EOF\n",
        media_type="application/pdf",
        retention_until=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert submitted.review_id is not None
    assert store.decide_review(submitted.review_id, decision="approved", actor_id="reviewer")

    with pytest.raises(OperationalError, match="production datasource"):
        store.promote_review(submitted.review_id, actor_id="operator")
