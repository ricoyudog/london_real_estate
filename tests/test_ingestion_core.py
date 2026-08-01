from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from zipfile import ZipInfo

import pytest

from nan_fung.ingestion.canonical import (
    CanonicalizationError,
    canonical_json,
    hash_canonical,
    parse_canonical_json,
)
from nan_fung.ingestion.jobs import (
    AttemptStatus,
    CalendarSchedule,
    CatchupPolicy,
    InMemoryJobQueue,
    IntervalSchedule,
    JobKind,
    JobState,
    RetryPolicy,
    StaleClaimError,
    Trigger,
    WorkflowJob,
    materialize_due_slots,
)
from nan_fung.ingestion.policies import (
    ArtifactPolicy,
    PolicyError,
    SourcePolicy,
    redact_secrets,
    validate_source_url,
    validate_zip_members,
)
from nan_fung.ingestion.registry import default_registry, default_runtime_bindings


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 3, hour, minute, tzinfo=UTC)


def test_canonical_identity_and_audit_redaction_are_deterministic() -> None:
    composed = {"caf\u00e9": ["2026-08-03", {"value": "3.75"}]}
    decomposed = {"cafe\u0301": ["2026-08-03", {"value": "3.75"}]}

    assert canonical_json(composed) == canonical_json(decomposed)
    assert hash_canonical("observation", composed) == hash_canonical(
        "observation", decomposed
    )
    assert parse_canonical_json(canonical_json(composed)) == composed
    with pytest.raises(CanonicalizationError, match="floats"):
        canonical_json({"value": 3.75})

    redacted = redact_secrets(
        {
            "source_url": "https://api.example.test/data?page=2&access_token=secret",
            "headers": {
                "Authorization": "Bearer secret",
                "Content-Type": "application/json",
            },
            "nested": {"api_key": "secret"},
        }
    )
    assert redacted["source_url"].endswith("access_token=%3Credacted%3E")
    assert redacted["headers"] == {"Content-Type": "application/json"}
    assert redacted["nested"]["api_key"] == "<redacted>"


def test_source_policy_rejects_ssrf_and_unsafe_archives() -> None:
    policy = SourcePolicy(
        ("api.example.test",),
        allowed_query_keys=("page",),
        artifact=ArtifactPolicy(max_archive_members=2, max_compression_ratio=10),
    )
    parsed = validate_source_url(
        "https://api.example.test/data?page=2",
        policy,
        resolver=lambda _host: ("8.8.8.8",),
    )
    assert parsed.hostname == "api.example.test"

    with pytest.raises(PolicyError, match="non-public"):
        validate_source_url(
            "https://api.example.test/data?page=2",
            policy,
            resolver=lambda _host: ("127.0.0.1",),
        )
    with pytest.raises(PolicyError, match="allowlisted"):
        validate_source_url(
            "https://metadata.google.internal/data?page=2",
            policy,
            resolver=lambda _host: ("8.8.8.8",),
        )

    traversal = ZipInfo("../../outside.csv")
    traversal.file_size = 1
    traversal.compress_size = 1
    with pytest.raises(PolicyError, match="unsafe archive member path"):
        validate_zip_members((traversal,), policy.artifact)

    symlink = ZipInfo("linked.csv")
    symlink.file_size = 1
    symlink.compress_size = 1
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(PolicyError, match="symlink"):
        validate_zip_members((symlink,), policy.artifact)

    expansion_bomb = ZipInfo("large.csv")
    expansion_bomb.file_size = 101
    expansion_bomb.compress_size = 1
    with pytest.raises(PolicyError, match="compression ratio"):
        validate_zip_members((expansion_bomb,), policy.artifact)


def test_seed_registry_marks_only_fixed_production_workflows_runtime_ready() -> None:
    registry = default_registry()
    bindings = default_runtime_bindings()
    definition_ids = {definition.datasource_id for definition in registry.definitions}

    assert {
        "boe.bank_rate.iudbedr",
        "voa.ndr_office_stock",
        "pld.applications_search",
        "bnp.central_london_office_report",
        "rightmove.commercial_insights_tracker",
        "ons.gdp.ecyx",
        "nomis.nm_59_1.london_lfs",
        "ons.opn.hybrid_working",
        "mhclg.epc.live_table_a_london",
        "govuk.search.market_news",
        "ons.onspd.postcode",
        "gla.town_centre_boundaries",
        "custom.london_office_submarkets",
    }.issubset(definition_ids)

    bank_rate = registry.lookup("boe.bank_rate.iudbedr")
    ons = registry.lookup("ons.gdp.ecyx")
    pld = registry.lookup("pld.applications_search")
    rightmove = registry.lookup("rightmove.commercial_insights_tracker")
    assert bank_rate.status == "production"
    assert bank_rate.capabilities["runtime_migration"] == "bound"
    assert ons.capabilities["runtime_migration"] == "bound"
    assert ons.capabilities["backfill"] == "unsupported_current_vintage"
    assert pld.status == "discovery"
    assert pld.default_lane == "source_discovery"
    assert pld.promotion_policy == "never_canonical"
    assert rightmove.automation_mode == "manual"
    assert rightmove.access_class == "reference_only"

    status = registry.runtime_status(bindings)
    assert status["boe.bank_rate.iudbedr@1"].ready
    assert status["ons.gdp.ecyx@1"].ready
    assert status["nomis.nm_59_1.london_lfs@1"].ready
    assert not status["boe.mpc_news@1"].ready
    assert not status["rightmove.commercial_insights_tracker@1"].ready


def test_job_queue_preserves_lease_retry_and_recovery_semantics() -> None:
    queue = InMemoryJobQueue()
    retry_policy = RetryPolicy(max_attempts=2, base_delay_seconds=30)
    job = WorkflowJob.create(
        job_kind=JobKind.SCHEDULED_INGEST,
        trigger=Trigger.SCHEDULE,
        scheduled_for=_at(10),
        now=_at(9),
        datasource_id="boe.bank_rate.iudbedr",
        definition_version=1,
        definition_hash="a" * 64,
        lane="production_ingestion",
        retry_policy=retry_policy,
    )
    assert queue.enqueue(job)[1]

    claim = queue.claim("worker-a", now=_at(10), lease=timedelta(seconds=10))
    assert claim is not None
    assert queue.claim("worker-b", now=_at(10)) is None
    queue.start(claim, now=_at(10))
    retry = queue.finish(
        claim,
        status=AttemptStatus.FAILED,
        now=_at(10, 0) + timedelta(seconds=1),
        retryable=True,
        error={"token": "must-not-persist"},
    )
    assert retry.state == JobState.RETRY_WAIT
    assert retry.available_at == _at(10, 0) + timedelta(seconds=31)
    assert retry.last_error == {"token": "<redacted>"}
    with pytest.raises(StaleClaimError):
        queue.heartbeat(claim, now=_at(10, 1))

    second_claim = queue.claim("worker-b", now=_at(10, 2))
    assert second_claim is not None
    queue.start(second_claim, now=_at(10, 2))
    dead_letter = queue.finish(
        second_claim,
        status=AttemptStatus.FAILED,
        now=_at(10, 3),
        retryable=True,
    )
    assert dead_letter.state == JobState.DEAD_LETTER

    abandoned = WorkflowJob.create(
        job_kind=JobKind.SCHEDULED_INGEST,
        trigger=Trigger.SCHEDULE,
        scheduled_for=_at(11),
        now=_at(10),
        datasource_id="boe.bank_rate.iudbedr",
        definition_version=1,
        definition_hash="b" * 64,
        lane="production_ingestion",
    )
    queue.enqueue(abandoned)
    abandoned_claim = queue.claim("worker-c", now=_at(11), lease=timedelta(seconds=5))
    assert abandoned_claim is not None
    recovered = queue.recover_expired(now=_at(11, 1))
    assert [(item.job_id, item.state) for item in recovered] == [
        (abandoned.job_id, JobState.QUEUED)
    ]


def test_calendar_dst_and_missed_slot_materialization_are_explicit() -> None:
    london_0130 = CalendarSchedule(hour=1, minute=30)
    # 01:30 local time does not exist on the London spring-forward date.
    assert london_0130.next_after(datetime(2026, 3, 29, 0, 0, tzinfo=UTC)) == datetime(
        2026, 3, 30, 0, 30, tzinfo=UTC
    )
    # The autumn fold materialises exactly the first local 01:30 occurrence.
    assert london_0130.next_after(datetime(2026, 10, 25, 0, 0, tzinfo=UTC)) == datetime(
        2026, 10, 25, 0, 30, tzinfo=UTC
    )
    assert london_0130.next_after(datetime(2026, 10, 25, 0, 40, tzinfo=UTC)) == datetime(
        2026, 10, 26, 1, 30, tzinfo=UTC
    )

    schedule = IntervalSchedule(_at(10), timedelta(minutes=5))
    latest = materialize_due_slots(
        schedule,
        cursor_at=_at(10),
        now=_at(10, 20),
        catchup_policy=CatchupPolicy.LATEST_ONLY,
    )
    assert latest.slots == (_at(10, 20),)
    assert latest.next_cursor_at == _at(10, 20)
    assert latest.skipped_slots == 3

    windowed = materialize_due_slots(
        schedule,
        cursor_at=_at(10),
        now=_at(10, 20),
        catchup_policy=CatchupPolicy.WINDOWED,
        max_catchup_jobs=2,
    )
    assert windowed.slots == (_at(10, 5), _at(10, 10))
    assert windowed.next_cursor_at == _at(10, 10)
    assert windowed.skipped_slots == 2
