from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

import pytest

from nan_fung.operational import (
    ApprovalDecisionConflictError,
    OperationalStore,
)
from nan_fung.read_api import (
    ReadContext,
    SQLiteReadRepository,
    citation_projection_v1,
)
from nan_fung.storage.db import connect_database


NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
_BANK_RATE_URL = "https://www.bankofengland.co.uk/boeapps/database/offline.csv"


def test_citation_projection_preserves_exact_anchor_run_evidence_and_locator(
    tmp_path: Path,
) -> None:
    store, observation_id, evidence_id, run_id = _canonical_bank_rate(store_path=tmp_path)
    repository = SQLiteReadRepository(store.database_path)
    anchor = NOW + timedelta(minutes=1)

    projections = citation_projection_v1(
        repository,
        ReadContext("agent", frozenset({"open"})),
        anchor_as_of=anchor,
        observation_ids=(observation_id,),
    )

    assert len(projections) == 1
    projection = projections[0]
    assert projection.anchor_as_of == anchor
    assert projection.canonical_run_id == run_id
    assert projection.observation_id == observation_id
    assert projection.evidence_id == evidence_id
    assert projection.datasource_id == "boe.bank_rate.iudbedr"
    assert projection.publisher == "Bank of England"
    assert projection.retrieved_at == NOW
    assert projection.locator["record_locator"] == {
        "kind": "csv_row",
        "row_key": "2026-07-30",
    }
    assert projection.public_url == _BANK_RATE_URL
    assert projection.title is None
    assert {
        "title_unavailable",
        "published_at_unavailable",
        "source_updated_at_unavailable",
    } <= set(projection.warnings)
    assert "request_json" not in projection.__dict__
    assert "response_json" not in projection.__dict__
    assert "artifact_uri" not in projection.__dict__


def test_citation_projection_fails_closed_when_context_lacks_evidence_access(
    tmp_path: Path,
) -> None:
    store, observation_id, _evidence_id, _run_id = _canonical_bank_rate(store_path=tmp_path)

    hidden = citation_projection_v1(
        SQLiteReadRepository(store.database_path),
        ReadContext("agent", frozenset({"internal"})),
        anchor_as_of=NOW + timedelta(minutes=1),
        observation_ids=(observation_id,),
    )

    assert hidden == ()


def test_citation_projection_uses_the_canonical_run_selected_at_its_anchor(
    tmp_path: Path,
) -> None:
    store, first_observation_id, first_evidence_id, first_run_id = _canonical_bank_rate(
        store_path=tmp_path
    )
    later = NOW + timedelta(hours=1)
    second_observation_id, second_evidence_id, second_run_id = _append_canonical_bank_rate(
        store,
        at=later,
        value="4.00",
    )
    repository = SQLiteReadRepository(store.database_path)
    context = ReadContext("agent", frozenset({"open"}))

    historical = citation_projection_v1(
        repository,
        context,
        anchor_as_of=NOW + timedelta(minutes=1),
        observation_ids=(first_observation_id, second_observation_id),
    )
    current = citation_projection_v1(
        repository,
        context,
        anchor_as_of=later + timedelta(minutes=1),
        observation_ids=(first_observation_id, second_observation_id),
    )

    assert [(item.canonical_run_id, item.evidence_id) for item in historical] == [
        (first_run_id, first_evidence_id)
    ]
    assert [(item.canonical_run_id, item.evidence_id) for item in current] == [
        (second_run_id, second_evidence_id)
    ]


def test_agent_refresh_approval_is_immutable_and_recovers_token_only_for_host_context(
    tmp_path: Path,
) -> None:
    store = OperationalStore(tmp_path)
    store.sync_registry(now=NOW)
    request_id = "refresh-onspd-21"
    fingerprint = _request_fingerprint(
        datasource_id="ons.onspd.postcode",
        request_profile="onspd-one-postcode",
        bounded_scope={"postcode": ("EC2Y 5AZ",)},
        intent="resolve a postcode",
    )
    _insert_confirmation(
        store,
        request_id=request_id,
        principal="competition-agent",
        fingerprint=fingerprint,
        token="token-must-never-reach-agent-output",
    )

    approval = store.create_agent_refresh_approval(
        refresh_request_id=request_id,
        principal="competition-agent",
        capability_scope_id="scope-session-a",
        capability_id="uk.postcode-resolution",
        manifest_version="capabilities.v1",
        profile_version="profiles.v1",
        request_fingerprint=fingerprint,
        datasource_id="ons.onspd.postcode",
        request_profile="onspd-one-postcode",
        bounded_scope={"postcode": ("EC2Y 5AZ",)},
        intent="resolve a postcode",
        now=NOW,
    )
    replayed_mapping = store.create_agent_refresh_approval(
        refresh_request_id=request_id,
        principal="competition-agent",
        capability_scope_id="scope-session-a",
        capability_id="uk.postcode-resolution",
        manifest_version="capabilities.v1",
        profile_version="profiles.v1",
        request_fingerprint=fingerprint,
        datasource_id="ons.onspd.postcode",
        request_profile="onspd-one-postcode",
        bounded_scope={"postcode": ("EC2Y 5AZ",)},
        intent="resolve a postcode",
        now=NOW,
    )

    assert approval == replayed_mapping
    assert approval.snapshot["datasource_id"] == "ons.onspd.postcode"
    assert approval.snapshot["request_profile"] == "onspd-one-postcode"
    assert approval.snapshot["intent"] == "resolve a postcode"
    assert approval.snapshot["bounded_scope"] == {"postcode": ("EC2Y 5AZ",)}
    assert "token-must-never-reach-agent-output" not in repr(approval)

    recovered = store.recover_agent_refresh_approval(
        approval.approval_id,
        principal="competition-agent",
        capability_scope_id="scope-session-a",
        capability_id="uk.postcode-resolution",
        manifest_version="capabilities.v1",
        profile_version="profiles.v1",
        request_fingerprint=fingerprint,
        now=NOW,
    )
    assert recovered.snapshot == approval.snapshot
    assert recovered.confirmation_token == "token-must-never-reach-agent-output"
    assert "token-must-never-reach-agent-output" not in repr(recovered)

    first = store.decide_agent_refresh_approval(
        approval.approval_id,
        decision="approve",
        principal="competition-agent",
        capability_scope_id="scope-session-a",
        capability_id="uk.postcode-resolution",
        manifest_version="capabilities.v1",
        profile_version="profiles.v1",
        request_fingerprint=fingerprint,
        actor_id="session-host",
        now=NOW,
    )
    same = store.decide_agent_refresh_approval(
        approval.approval_id,
        decision="approve",
        principal="competition-agent",
        capability_scope_id="scope-session-a",
        capability_id="uk.postcode-resolution",
        manifest_version="capabilities.v1",
        profile_version="profiles.v1",
        request_fingerprint=fingerprint,
        actor_id="session-host",
        now=NOW,
    )

    assert first.outcome == "recorded"
    assert same.outcome == "replayed"
    with pytest.raises(ApprovalDecisionConflictError):
        store.decide_agent_refresh_approval(
            approval.approval_id,
            decision="deny",
            principal="competition-agent",
            capability_scope_id="scope-session-a",
            capability_id="uk.postcode-resolution",
            manifest_version="capabilities.v1",
            profile_version="profiles.v1",
            request_fingerprint=fingerprint,
            actor_id="session-host",
            now=NOW,
        )

    connection = connect_database(store.database_path)
    try:
        events = connection.execute(
            """
            SELECT event_type, decision
            FROM agent_refresh_approval_event
            WHERE approval_id = ?
            ORDER BY event_seq
            """,
            (approval.approval_id,),
        ).fetchall()
        snapshot_json = connection.execute(
            "SELECT request_snapshot_json FROM agent_refresh_approval WHERE approval_id = ?",
            (approval.approval_id,),
        ).fetchone()["request_snapshot_json"]
        with pytest.raises(Exception, match="IMMUTABLE_AGENT_REFRESH_APPROVAL"):
            connection.execute(
                "UPDATE agent_refresh_approval SET principal = 'other' WHERE approval_id = ?",
                (approval.approval_id,),
            )
    finally:
        connection.close()
    assert [tuple(event) for event in events] == [
        ("decision", "approve"),
        ("replay", "approve"),
    ]
    assert "token-must-never-reach-agent-output" not in snapshot_json


def _canonical_bank_rate(
    *, store_path: Path
) -> tuple[OperationalStore, str, str, str]:
    store = OperationalStore(store_path)
    observation_id, evidence_id, run_id = _append_canonical_bank_rate(
        store,
        at=NOW,
        value="3.75",
    )
    return store, observation_id, evidence_id, run_id


def _append_canonical_bank_rate(
    store: OperationalStore,
    *,
    at: datetime,
    value: str,
) -> tuple[str, str, str]:
    queued = store.enqueue(
        "boe.bank_rate.iudbedr",
        scheduled_for=at,
        request={"fixture": f"citation-{at.isoformat()}"},
    )
    claim = store.claim_job(queued.job_id, "worker", now=at)
    assert claim is not None
    run = store.start_run(claim, "worker", now=at)
    evidence = store.persist_evidence(
        run,
        b"DATE,IUDBEDR\n30 Jul 2026,3.75\n",
        media_type="text/csv",
        request={"method": "GET", "url": _BANK_RATE_URL},
        response={
            "status": 200,
            "final_url": f"{_BANK_RATE_URL}?opaque=do-not-leak",
        },
        retrieved_at=at,
        now=at,
    )
    observation_id = store.persist_observation(
        run,
        record_key=("IUDBEDR", "2026-07-30"),
        payload={"bank_rate_percent": value},
        record_type="metric",
        category="macro",
        evidence=(evidence,),
        locator={"kind": "csv_row", "row_key": "2026-07-30"},
        source_date="2026-07-30",
        unit="percent",
        definition_text="Official Bank Rate",
        now=at,
    )
    store.finish_run(run, status="succeeded", promote=True, now=at)
    return observation_id, evidence.evidence_id, run.run_id


def _request_fingerprint(
    *,
    datasource_id: str,
    request_profile: str,
    bounded_scope: dict[str, tuple[str, ...]],
    intent: str,
) -> str:
    return sha256(
        json.dumps(
            {
                "datasource_id": datasource_id,
                "request_profile": request_profile,
                "bounded_scope": {
                    key: list(value) for key, value in sorted(bounded_scope.items())
                },
                "intent": intent,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _insert_confirmation(
    store: OperationalStore,
    *,
    request_id: str,
    principal: str,
    fingerprint: str,
    token: str,
) -> None:
    connection = connect_database(store.database_path)
    try:
        connection.execute(
            """
            INSERT INTO refresh_confirmation (
                confirmation_token, request_id, principal, request_fingerprint,
                datasource_id, definition_version, day_start_at, issued_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                request_id,
                principal,
                fingerprint,
                "ons.onspd.postcode",
                1,
                "2026-08-01T00:00:00.000000Z",
                "2026-08-01T12:00:00.000000Z",
                "2026-08-01T12:10:00.000000Z",
            ),
        )
    finally:
        connection.close()
