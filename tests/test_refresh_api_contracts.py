from datetime import UTC, datetime, timedelta

import pytest

from nan_fung.refresh_api import (
    REFRESH_SCHEMA_VERSION,
    BackendSubmitResult,
    InMemoryRefreshBackend,
    InvalidRefreshRequest,
    RefreshAccessDenied,
    RefreshBroker,
    RefreshContext,
    RefreshDisposition,
    RefreshProfile,
    RefreshRequest,
    RefreshStatus,
    get_refresh_status_v1,
    request_refresh_v1,
)


NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


class CapturingBackend(InMemoryRefreshBackend):
    def __init__(self) -> None:
        super().__init__()
        self.submissions = []

    def submit(self, submission):  # type: ignore[no-untyped-def]
        self.submissions.append(submission)
        return super().submit(submission)


def profile() -> RefreshProfile:
    return RefreshProfile(
        profile_id="official_bank_rate",
        datasource_id="boe.bank_rate.iudbedr",
        definition_version=1,
        effective_lane="production_ingestion",
        allowed_scope_keys=frozenset({"period"}),
        cooldown=timedelta(minutes=5),
        poll_after=timedelta(seconds=3),
        promotion_policy="automatic_after_validation",
    )


def context(request_id: str, principal: str = "agent") -> RefreshContext:
    return RefreshContext(principal, request_id, frozenset({"official_bank_rate"}))


def request() -> RefreshRequest:
    return RefreshRequest(
        datasource_id="boe.bank_rate.iudbedr",
        request_profile="official_bank_rate",
        bounded_scope={"period": "latest"},
        intent="user asks for the latest Bank Rate",
    )


def test_broker_selects_fixed_policy_and_enqueues_only_a_bounded_submission() -> None:
    backend = CapturingBackend()
    broker = RefreshBroker({"official_bank_rate": profile()}, backend, clock=lambda: NOW)

    acknowledgement = request_refresh_v1(broker, context("request_1"), request())

    assert acknowledgement.disposition is RefreshDisposition.ACCEPTED
    assert acknowledgement.effective_lane == "production_ingestion"
    assert acknowledgement.initial_state == "queued"
    assert acknowledgement.job_id is not None
    assert len(backend.submissions) == 1
    submission = backend.submissions[0]
    assert submission.definition_version == 1
    assert submission.effective_lane == "production_ingestion"
    assert submission.promotion_policy == "automatic_after_validation"
    assert submission.bounded_scope == {"period": ("latest",)}


def test_broker_deduplicates_and_status_is_capability_scoped() -> None:
    backend = CapturingBackend()
    broker = RefreshBroker({"official_bank_rate": profile()}, backend, clock=lambda: NOW)
    first = broker.request(context("request_1", "first"), request())
    second = broker.request(context("request_2", "second"), request())

    assert first.disposition is RefreshDisposition.ACCEPTED
    assert second.disposition is RefreshDisposition.DEDUPLICATED
    assert second.job_id == first.job_id
    # The broker delegates idempotency to its backend so an operational
    # implementation can enforce it across process restarts.
    assert len(backend.submissions) == 2
    assert first.job_id is not None
    backend.set_status(
        RefreshStatus(
            schema_version=REFRESH_SCHEMA_VERSION,
            job_id=first.job_id,
            job_state="succeeded",
            latest_attempt_status="succeeded",
            promotion_status="approved",
            canonical_changed=True,
            observation_ids=("obs_bank_rate",),
            evidence_ids=("ev_bank_rate",),
        )
    )
    status = get_refresh_status_v1(broker, context("status_1", "second"), first.job_id)
    assert status is not None
    assert status.canonical_changed is True
    with pytest.raises(RefreshAccessDenied):
        broker.get_status(context("status_2", "outsider"), first.job_id)


def test_broker_rejects_untrusted_profile_scope_and_arbitrary_network_location() -> None:
    broker = RefreshBroker(
        {"official_bank_rate": profile()}, InMemoryRefreshBackend(), clock=lambda: NOW
    )
    with pytest.raises(RefreshAccessDenied):
        broker.request(
            RefreshContext("agent", "request_1", frozenset({"other"})), request()
        )
    with pytest.raises(InvalidRefreshRequest):
        broker.request(
            context("request_2"),
            RefreshRequest(
                datasource_id="boe.bank_rate.iudbedr",
                request_profile="official_bank_rate",
                bounded_scope={"url": "https://example.test"},
            ),
        )
    with pytest.raises(InvalidRefreshRequest):
        broker.request(
            context("request_3"),
            RefreshRequest(
                datasource_id="wrong.datasource",
                request_profile="official_bank_rate",
            ),
        )


def test_idempotent_request_ids_and_bounded_status_wait_are_enforced() -> None:
    backend = InMemoryRefreshBackend()
    broker = RefreshBroker({"official_bank_rate": profile()}, backend, clock=lambda: NOW)
    first = broker.request(context("request_1"), request())
    replay = broker.request(context("request_1"), request())
    assert replay == first
    with pytest.raises(RefreshAccessDenied):
        broker.request(context("request_1", "another-principal"), request())
    with pytest.raises(InvalidRefreshRequest):
        broker.request(
            context("request_1"),
            RefreshRequest(
                datasource_id="boe.bank_rate.iudbedr",
                request_profile="official_bank_rate",
                bounded_scope={"period": "2025"},
            ),
        )
    assert first.job_id is not None
    with pytest.raises(InvalidRefreshRequest):
        broker.get_status(
            context("status_1"), first.job_id, wait_deadline=NOW + timedelta(seconds=31)
        )


def test_backend_can_explicitly_return_already_fresh_without_a_job() -> None:
    class FreshBackend(InMemoryRefreshBackend):
        def submit(self, submission):  # type: ignore[no-untyped-def]
            return BackendSubmitResult(
                disposition=RefreshDisposition.ALREADY_FRESH,
                job_id=None,
                initial_state="already_fresh",
                canonical_anchor=NOW - timedelta(minutes=1),
            )

    broker = RefreshBroker({"official_bank_rate": profile()}, FreshBackend(), clock=lambda: NOW)
    acknowledgement = broker.request(context("request_1"), request())

    assert acknowledgement.disposition is RefreshDisposition.ALREADY_FRESH
    assert acknowledgement.job_id is None
    assert acknowledgement.canonical_anchor == NOW - timedelta(minutes=1)


def test_onspd_profile_requires_exactly_one_bounded_postcode() -> None:
    profile = RefreshProfile(
        profile_id="onspd-postcode",
        datasource_id="ons.onspd.postcode",
        definition_version=1,
        effective_lane="production_ingestion",
        allowed_scope_keys=frozenset({"postcode"}),
        required_scope_keys=frozenset({"postcode"}),
        single_value_scope_keys=frozenset({"postcode"}),
    )
    broker = RefreshBroker({profile.profile_id: profile}, InMemoryRefreshBackend(), clock=lambda: NOW)
    context = RefreshContext("agent", "onspd-one", frozenset({profile.profile_id}))

    acknowledgement = broker.request(
        context,
        RefreshRequest(
            datasource_id="ons.onspd.postcode",
            request_profile=profile.profile_id,
            bounded_scope={"postcode": "EC2Y 5AS"},
        ),
    )

    assert acknowledgement.disposition is RefreshDisposition.ACCEPTED
    with pytest.raises(InvalidRefreshRequest, match="required key"):
        broker.request(
            RefreshContext("agent", "onspd-missing", frozenset({profile.profile_id})),
            RefreshRequest(
                datasource_id="ons.onspd.postcode",
                request_profile=profile.profile_id,
            ),
        )
    with pytest.raises(InvalidRefreshRequest, match="exactly one"):
        broker.request(
            RefreshContext("agent", "onspd-many", frozenset({profile.profile_id})),
            RefreshRequest(
                datasource_id="ons.onspd.postcode",
                request_profile=profile.profile_id,
                bounded_scope={"postcode": ["EC2Y 5AS", "SW1A 1AA"]},
            ),
        )
