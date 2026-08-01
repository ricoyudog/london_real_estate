from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

from nan_fung.agent_tools import AgentToolFacade, AgentToolSession
from nan_fung.operational import ApprovalDecisionConflictError
from nan_fung.refresh_api import (
    BackendSubmitResult,
    InMemoryRefreshBackend,
    RefreshBroker,
    RefreshDisposition,
    RefreshProfile,
    RefreshStatus,
)


NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "agent_tools" / "v1"


class CapturingBackend(InMemoryRefreshBackend):
    def __init__(self) -> None:
        super().__init__()
        self.last_job_id: str | None = None
        self.submissions: list[object] = []

    def submit(self, submission):  # type: ignore[no-untyped-def]
        self.submissions.append(submission)
        result = super().submit(submission)
        self.last_job_id = result.job_id
        return result


def _profile() -> RefreshProfile:
    return RefreshProfile(
        profile_id="bank-rate-latest",
        datasource_id="boe.bank_rate.iudbedr",
        definition_version=1,
        effective_lane="production_ingestion",
        allowed_scope_keys=frozenset(),
        cooldown=timedelta(minutes=5),
        poll_after=timedelta(seconds=2),
        promotion_policy="registry_selected",
    )


def _facade(backend: InMemoryRefreshBackend) -> AgentToolFacade:
    broker = RefreshBroker({_profile().profile_id: _profile()}, backend, clock=lambda: NOW)
    return AgentToolFacade(
        refresh_broker=broker,
        handle_secret=b"f" * 32,
        clock=lambda: NOW,
    )


def _fixture(name: str) -> dict[str, object]:
    fixtures = json.loads((FIXTURE_PATH / "requests.json").read_text(encoding="utf-8"))
    return deepcopy(fixtures[name])


def _status_request(job_ref: str) -> dict[str, object]:
    request = _fixture("describe_market_data")
    request["request_id"] = "call_refresh_status_001"
    request["arguments"] = {"job_ref": job_ref}
    host_context = request["host_context"]
    assert isinstance(host_context, dict)
    host_context["tool_call_id"] = "toolcall_status_001"
    return request


def _error_code(result: dict[str, object]) -> str:
    error = result["error"]
    assert isinstance(error, dict)
    code = error["code"]
    assert isinstance(code, str)
    return code


def test_refresh_acknowledgement_uses_host_request_identity_and_an_opaque_job_handle() -> None:
    backend = CapturingBackend()
    facade = _facade(backend)

    acknowledgement = facade.execute("request_data_refresh", _fixture("request_bank_rate_refresh"))

    assert acknowledgement["status"] == "ok"
    data = acknowledgement["data"]
    assert isinstance(data, dict)
    assert data["disposition"] == "accepted"
    assert isinstance(data["job_ref"], str)
    assert data["poll_after_seconds"] == 2
    assert "job_id" not in data
    assert "refresh_request_id" not in json.dumps(acknowledgement)
    assert backend.last_job_id is not None
    assert len(backend.submissions) == 1
    submission = backend.submissions[0]
    assert getattr(submission, "request_id") == "refresh_001"


def test_refresh_status_reveals_only_safe_terminal_state_and_a_session_bound_handle() -> None:
    backend = CapturingBackend()
    facade = _facade(backend)
    acknowledgement = facade.execute("request_data_refresh", _fixture("request_bank_rate_refresh"))
    acknowledgement_data = acknowledgement["data"]
    assert isinstance(acknowledgement_data, dict)
    job_ref = acknowledgement_data["job_ref"]
    assert isinstance(job_ref, str)
    assert backend.last_job_id is not None
    backend.set_status(
        RefreshStatus(
            schema_version="refresh_api.v1",
            job_id=backend.last_job_id,
            job_state="succeeded",
            latest_attempt_status="succeeded",
            promotion_status="approved",
            canonical_changed=True,
            observation_ids=("obs_private",),
            evidence_ids=("ev_private",),
            datasource_health={"secret": "not agent output"},
        )
    )

    status = facade.execute("get_refresh_status", _status_request(job_ref))

    assert status["status"] == "ok"
    data = status["data"]
    assert isinstance(data, dict)
    assert data["job_state"] == "succeeded"
    assert data["promotion_status"] == "approved"
    assert data["canonical_changed"] is True
    assert not {"job_id", "observation_ids", "evidence_ids", "datasource_health", "result_ref"} & set(data)

    another_session = _status_request(job_ref)
    host_context = another_session["host_context"]
    assert isinstance(host_context, dict)
    host_context["capability_scope_id"] = "scope_fedcba9876543210fedcba9876543210"
    denied = facade.execute("get_refresh_status", another_session)
    assert _error_code(denied) == "POLICY_DENIED"

    tampered = facade.execute("get_refresh_status", _status_request(f"{job_ref}x"))
    assert _error_code(tampered) == "INVALID_CURSOR"

    wrong_kind_request = _fixture("get_citation_metadata")
    wrong_kind_arguments = wrong_kind_request["arguments"]
    assert isinstance(wrong_kind_arguments, dict)
    wrong_kind_arguments["citation_refs"] = [job_ref]
    wrong_kind = facade.execute("get_citation_metadata", wrong_kind_request)
    assert _error_code(wrong_kind) == "INVALID_CURSOR"


def test_refresh_retries_keep_the_host_request_id_and_new_logical_refreshes_dedupe() -> None:
    backend = CapturingBackend()
    facade = _facade(backend)
    first_request = _fixture("request_bank_rate_refresh")

    first = facade.execute("request_data_refresh", first_request)
    retry_request = _fixture("request_bank_rate_refresh")
    retry_request["request_id"] = "call_refresh_retry_001"
    retry_context = retry_request["host_context"]
    assert isinstance(retry_context, dict)
    retry_context["tool_call_id"] = "toolcall_refresh_retry_001"
    retry = facade.execute("request_data_refresh", retry_request)
    second_request = _fixture("request_bank_rate_refresh")
    second_request["request_id"] = "call_refresh_002"
    host_context = second_request["host_context"]
    assert isinstance(host_context, dict)
    host_context["tool_call_id"] = "toolcall_refresh_002"
    host_context["refresh_request_id"] = "refresh_002"
    deduplicated = facade.execute("request_data_refresh", second_request)

    assert first["status"] == retry["status"] == deduplicated["status"] == "ok"
    retry_data = retry["data"]
    deduplicated_data = deduplicated["data"]
    assert isinstance(retry_data, dict)
    assert isinstance(deduplicated_data, dict)
    assert retry_data["disposition"] == "accepted"
    assert deduplicated_data["disposition"] == "deduplicated"
    assert [getattr(submission, "request_id") for submission in backend.submissions] == [
        "refresh_001",
        "refresh_001",
        "refresh_002",
    ]


def test_refresh_rejects_model_policy_or_refresh_identity_injection() -> None:
    facade = _facade(CapturingBackend())
    injected = _fixture("request_bank_rate_refresh")
    arguments = injected["arguments"]
    assert isinstance(arguments, dict)
    arguments["lane"] = "source_discovery"
    arguments["refresh_request_id"] = "model_controlled"

    result = facade.execute("request_data_refresh", injected)

    assert result["status"] == "error"
    assert _error_code(result) == "INVALID_ARGUMENT"


def test_already_fresh_refresh_projection_has_no_job_handle() -> None:
    class FreshBackend(InMemoryRefreshBackend):
        def submit(self, submission):  # type: ignore[no-untyped-def]
            return BackendSubmitResult(
                disposition=RefreshDisposition.ALREADY_FRESH,
                job_id=None,
                initial_state="already_fresh",
                canonical_anchor=NOW,
            )

    acknowledgement = _facade(FreshBackend()).execute(
        "request_data_refresh", _fixture("request_bank_rate_refresh")
    )

    assert acknowledgement["status"] == "ok"
    data = acknowledgement["data"]
    assert isinstance(data, dict)
    assert data["disposition"] == "already_fresh"
    assert data["job_ref"] is None
    canonical_anchor = data["canonical_anchor"]
    assert isinstance(canonical_anchor, str)
    assert canonical_anchor.startswith("2026-08-01T12:00:00")
    assert canonical_anchor.endswith("Z")


def test_session_enforces_refresh_poll_cadence_after_ack_and_pending_status() -> None:
    class Host:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def invoke(self, tool_name, request, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(tool_name)
            if tool_name == "request_data_refresh":
                data = {
                    "disposition": "accepted",
                    "job_ref": "job_session_bound",
                    "poll_after_seconds": 1,
                }
            else:
                data = {
                    "job_state": "queued",
                    "latest_attempt_status": "queued",
                    "canonical_changed": None,
                }
            return {
                "schema_version": "agent_tool_result.v1",
                "request_id": request["request_id"],
                "status": "ok",
                "data": data,
                "warnings": [],
                "error": None,
            }

    current = [NOW]
    host = Host()
    session = AgentToolSession(
        host,
        principal="competition-agent",
        allowed_access_classes=("open",),
        allowed_capability_ids=("uk.bank-rate-current",),
        allowed_refresh_profiles=("bank-rate-latest",),
        capability_scope_id="scope_0123456789abcdef0123456789abcdef",
        clock=lambda: current[0],
    )
    refresh_arguments = _fixture("request_bank_rate_refresh")["arguments"]
    assert isinstance(refresh_arguments, dict)
    acknowledgement = session.call(
        "request_data_refresh",
        refresh_arguments,
        turn_id="turn_001",
        tool_call_id="toolcall_refresh_001",
    )
    acknowledgement_data = acknowledgement["data"]
    assert isinstance(acknowledgement_data, dict)
    job_ref = acknowledgement_data["job_ref"]
    assert isinstance(job_ref, str)

    too_early = session.call(
        "get_refresh_status",
        {"job_ref": job_ref},
        turn_id="turn_001",
        tool_call_id="toolcall_status_001",
    )
    assert too_early["status"] == "error"
    assert host.calls == ["request_data_refresh"]

    current[0] += timedelta(seconds=1)
    pending = session.call(
        "get_refresh_status",
        {"job_ref": job_ref},
        turn_id="turn_001",
        tool_call_id="toolcall_status_002",
    )
    assert pending["status"] == "ok"
    immediate_replay = session.call(
        "get_refresh_status",
        {"job_ref": job_ref},
        turn_id="turn_001",
        tool_call_id="toolcall_status_003",
    )
    assert immediate_replay["status"] == "error"
    assert host.calls == ["request_data_refresh", "get_refresh_status"]


def test_onspd_confirmation_projects_only_host_approval_without_the_token() -> None:
    class ConfirmationBackend(InMemoryRefreshBackend):
        def submit(self, submission):  # type: ignore[no-untyped-def]
            return BackendSubmitResult(
                disposition=RefreshDisposition.CONFIRMATION_REQUIRED,
                job_id=None,
                initial_state="confirmation_required",
                confirmation_token="token-must-never-reach-agent-output",
                confirmation_expires_at=NOW + timedelta(minutes=10),
            )

    class ApprovalStore:
        def __init__(self) -> None:
            self.created: dict[str, object] | None = None

        def create_agent_refresh_approval(self, **kwargs: object) -> SimpleNamespace:
            self.created = dict(kwargs)
            return SimpleNamespace(
                approval_id="approval_host_only",
                expires_at=NOW + timedelta(minutes=10),
            )

    profile = RefreshProfile(
        profile_id="onspd-one-postcode",
        datasource_id="ons.onspd.postcode",
        definition_version=1,
        effective_lane="production_ingestion",
        allowed_scope_keys=frozenset({"postcode"}),
        required_scope_keys=frozenset({"postcode"}),
        single_value_scope_keys=frozenset({"postcode"}),
        cooldown=timedelta(minutes=5),
        poll_after=timedelta(seconds=2),
        promotion_policy="registry_selected",
    )
    approval_store = ApprovalStore()
    facade = AgentToolFacade(
        refresh_broker=RefreshBroker(
            {profile.profile_id: profile}, ConfirmationBackend(), clock=lambda: NOW
        ),
        approval_store=approval_store,
        handle_secret=b"a" * 32,
        clock=lambda: NOW,
    )
    request = _fixture("request_bank_rate_refresh")
    request["request_id"] = "call_onspd_refresh_001"
    request["arguments"] = {
        "capability_id": "uk.postcode-resolution",
        "datasource_id": "ons.onspd.postcode",
        "request_profile": "onspd-one-postcode",
        "bounded_scope": {"postcode": "EC2Y 5AZ"},
        "intent": "resolve a postcode",
    }
    host_context = request["host_context"]
    assert isinstance(host_context, dict)
    host_context["tool_call_id"] = "toolcall_onspd_refresh_001"
    host_context["refresh_request_id"] = "refresh_onspd_001"
    host_context["allowed_capability_ids"] = ["uk.postcode-resolution"]
    host_context["allowed_refresh_profiles"] = ["onspd-one-postcode"]

    acknowledgement = facade.execute("request_data_refresh", request)

    assert acknowledgement["status"] == "ok"
    data = acknowledgement["data"]
    assert isinstance(data, dict)
    assert data["disposition"] == "approval_required"
    assert data["job_ref"] is None
    assert data["approval_id"] == "approval_host_only"
    assert isinstance(data["approval_expires_at"], str)
    assert data["canonical_anchor"] is None
    assert data["poll_after_seconds"] is None
    rendered = json.dumps(acknowledgement)
    assert "confirmation_token" not in rendered
    assert "token-must-never-reach-agent-output" not in rendered
    assert approval_store.created is not None
    assert approval_store.created["refresh_request_id"] == "refresh_onspd_001"
    assert approval_store.created["capability_scope_id"] == (
        "scope_0123456789abcdef0123456789abcdef"
    )
    assert approval_store.created["bounded_scope"] == {"postcode": ["EC2Y 5AZ"]}
    assert approval_store.created["manifest_version"] == "2026-08-01.v1"
    assert approval_store.created["profile_version"] == "2026-08-01.v1"
    fingerprint = approval_store.created["request_fingerprint"]
    assert isinstance(fingerprint, str)
    assert len(fingerprint) == 64
    assert "confirmation_token" not in approval_store.created


def test_host_only_onspd_approval_replays_exact_snapshot_and_denial_never_submits() -> None:
    snapshot = {
        "datasource_id": "ons.onspd.postcode",
        "request_profile": "onspd-one-postcode",
        "bounded_scope": {"postcode": ["EC2Y 5AZ"]},
        "intent": "resolve a postcode",
    }

    class ApprovalStore:
        def __init__(self) -> None:
            self.approval = SimpleNamespace(
                approval_id="approval_host_only",
                refresh_request_id="refresh_onspd_001",
                principal="competition-agent",
                capability_scope_id="scope_0123456789abcdef0123456789abcdef",
                capability_id="uk.postcode-resolution",
                manifest_version="2026-08-01.v1",
                profile_version="2026-08-01.v1",
                request_fingerprint="f" * 64,
                snapshot=snapshot,
            )
            self.decision: str | None = None
            self.recover_calls = 0

        def lookup_agent_refresh_approval(self, approval_id: str, **_kwargs: object) -> SimpleNamespace:
            assert approval_id == self.approval.approval_id
            return self.approval

        def decide_agent_refresh_approval(self, _approval_id: str, *, decision: str, **_kwargs: object) -> SimpleNamespace:
            if self.decision is not None and self.decision != decision:
                raise ApprovalDecisionConflictError("conflicting approval decision")
            outcome = "replayed" if self.decision == decision else "recorded"
            self.decision = decision
            return SimpleNamespace(outcome=outcome)

        def recover_agent_refresh_approval(self, _approval_id: str, **_kwargs: object) -> SimpleNamespace:
            self.recover_calls += 1
            return SimpleNamespace(
                snapshot=snapshot,
                confirmation_token="token-must-never-reach-agent-output",
            )

    class ApprovingBackend(InMemoryRefreshBackend):
        def __init__(self) -> None:
            super().__init__()
            self.confirmation_tokens: list[str | None] = []

        def submit(self, submission):  # type: ignore[no-untyped-def]
            self.confirmation_tokens.append(submission.confirmation_token)
            return super().submit(submission)

    profile = RefreshProfile(
        profile_id="onspd-one-postcode",
        datasource_id="ons.onspd.postcode",
        definition_version=1,
        effective_lane="production_ingestion",
        allowed_scope_keys=frozenset({"postcode"}),
        required_scope_keys=frozenset({"postcode"}),
        single_value_scope_keys=frozenset({"postcode"}),
        poll_after=timedelta(seconds=2),
        promotion_policy="registry_selected",
    )

    def facade(store: ApprovalStore, backend: ApprovingBackend) -> AgentToolFacade:
        return AgentToolFacade(
            refresh_broker=RefreshBroker(
                {profile.profile_id: profile}, backend, clock=lambda: NOW
            ),
            approval_store=store,  # type: ignore[arg-type]
            handle_secret=b"h" * 32,
            clock=lambda: NOW,
        )

    def approval_request(
        decision: str,
        *,
        call_suffix: str,
        scope: str = "scope_0123456789abcdef0123456789abcdef",
    ) -> dict[str, object]:
        request = _fixture("describe_market_data")
        request["request_id"] = f"call_approval_{decision}_{call_suffix}"
        request["arguments"] = {"approval_id": "approval_host_only", "decision": decision}
        context = request["host_context"]
        assert isinstance(context, dict)
        context["tool_call_id"] = f"toolcall_approval_{decision}_{call_suffix}"
        context["capability_scope_id"] = scope
        context["allowed_capability_ids"] = ["uk.postcode-resolution"]
        context["allowed_refresh_profiles"] = ["onspd-one-postcode"]
        return request

    store = ApprovalStore()
    backend = ApprovingBackend()
    approved_facade = facade(store, backend)
    approved = approved_facade.execute(
        "approve_refresh", approval_request("approve", call_suffix="first")
    )
    replayed = approved_facade.execute(
        "approve_refresh", approval_request("approve", call_suffix="replay")
    )

    assert approved["status"] == replayed["status"] == "ok"
    approved_data = approved["data"]
    replayed_data = replayed["data"]
    assert isinstance(approved_data, dict)
    assert isinstance(replayed_data, dict)
    assert approved_data["approval_outcome"] == "recorded"
    assert replayed_data["approval_outcome"] == "replayed"
    assert backend.confirmation_tokens == [
        "token-must-never-reach-agent-output",
        "token-must-never-reach-agent-output",
    ]
    assert "token-must-never-reach-agent-output" not in json.dumps(approved)

    conflicting = approved_facade.execute(
        "approve_refresh", approval_request("deny", call_suffix="conflict")
    )
    wrong_scope = approved_facade.execute(
        "approve_refresh",
        approval_request(
            "approve",
            call_suffix="wrong_scope",
            scope="scope_fedcba9876543210fedcba9876543210",
        ),
    )
    assert _error_code(conflicting) == "POLICY_DENIED"
    assert _error_code(wrong_scope) == "ACCESS_DENIED"

    denied_store = ApprovalStore()
    denied_backend = ApprovingBackend()
    denied = facade(denied_store, denied_backend).execute(
        "approve_refresh", approval_request("deny", call_suffix="fresh_denial")
    )
    assert denied["status"] == "ok"
    denied_data = denied["data"]
    assert isinstance(denied_data, dict)
    assert denied_data["disposition"] == "denied"
    assert denied_backend.confirmation_tokens == []
    assert denied_store.recover_calls == 0

    class NeverCalledHost:
        def invoke(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("model session must not dispatch approve_refresh")

    session = AgentToolSession(
        NeverCalledHost(),  # type: ignore[arg-type]
        principal="competition-agent",
        allowed_access_classes=("open",),
        allowed_capability_ids=("uk.postcode-resolution",),
        allowed_refresh_profiles=("onspd-one-postcode",),
        capability_scope_id="scope_0123456789abcdef0123456789abcdef",
    )
    model_attempt = session.call(
        "approve_refresh",
        {"approval_id": "approval_host_only", "decision": "approve"},
        turn_id="turn_001",
        tool_call_id="toolcall_approval_model_attempt",
    )
    assert _error_code(model_attempt) == "POLICY_DENIED"
