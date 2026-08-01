"""Trusted broker for bounded, durable refresh requests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json

from .contracts import (
    REFRESH_SCHEMA_VERSION,
    BackendSubmitResult,
    InvalidRefreshRequest,
    RefreshAcknowledgement,
    RefreshAccessDenied,
    RefreshBackend,
    RefreshContext,
    RefreshDisposition,
    RefreshProfile,
    RefreshRequest,
    RefreshStatus,
    RefreshSubmission,
    normalise_utc,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RefreshBroker:
    """Validate agent-safe input then delegate only a fixed job to a backend.

    The broker has no database connection, collector function, promotion
    callable, or arbitrary network request capability.  A daemon adapter owns
    the actual durable enqueue operation.
    """

    def __init__(
        self,
        profiles: Mapping[str, RefreshProfile],
        backend: RefreshBackend,
        *,
        clock: Callable[[], datetime] = _utc_now,
        maximum_status_wait: timedelta = timedelta(seconds=30),
    ) -> None:
        if maximum_status_wait <= timedelta(0):
            raise ValueError("maximum_status_wait must be positive")
        normalised_profiles = dict(profiles)
        if not normalised_profiles:
            raise ValueError("at least one refresh profile is required")
        if any(key != profile.profile_id for key, profile in normalised_profiles.items()):
            raise ValueError("profile mapping keys must match profile_id")
        self._profiles = normalised_profiles
        self._backend = backend
        self._clock = clock
        self._maximum_status_wait = maximum_status_wait

    def request(
        self, context: RefreshContext, request: RefreshRequest
    ) -> RefreshAcknowledgement:
        profile = self._profile_for(context, request)
        self._validate_scope(profile, request)
        fingerprint = self._request_fingerprint(request)
        submitted_at = normalise_utc(self._clock())
        dedupe_key = self._dedupe_key(profile, request)
        result = self._backend.submit(
            RefreshSubmission(
                request_id=context.request_instance_id,
                dedupe_key=dedupe_key,
                principal=context.principal,
                datasource_id=profile.datasource_id,
                definition_version=profile.definition_version,
                request_profile=profile.profile_id,
                effective_lane=profile.effective_lane,
                bounded_scope=request.bounded_scope,
                intent=request.intent,
                submitted_at=submitted_at,
                promotion_policy=profile.promotion_policy,
                request_fingerprint=fingerprint,
                cooldown_until=submitted_at + profile.cooldown,
                confirmation_token=request.confirmation_token,
            )
        )
        return self._acknowledgement(profile, context, submitted_at, result)

    def get_status(
        self,
        context: RefreshContext,
        job_id: str,
        *,
        wait_deadline: datetime | None = None,
    ) -> RefreshStatus | None:
        if not job_id:
            raise InvalidRefreshRequest("job_id must be non-empty")
        if wait_deadline is not None:
            wait_deadline = normalise_utc(wait_deadline)
            now = normalise_utc(self._clock())
            if wait_deadline < now or wait_deadline - now > self._maximum_status_wait:
                raise InvalidRefreshRequest("wait_deadline exceeds the broker wait budget")
        return self._backend.get_status(
            job_id,
            principal=context.principal,
            wait_deadline=wait_deadline,
        )

    def _profile_for(
        self, context: RefreshContext, request: RefreshRequest
    ) -> RefreshProfile:
        if request.request_profile not in context.allowed_profiles:
            raise RefreshAccessDenied("refresh profile is not granted to this context")
        try:
            profile = self._profiles[request.request_profile]
        except KeyError as error:
            raise InvalidRefreshRequest("refresh profile is not registered") from error
        if profile.datasource_id != request.datasource_id:
            raise InvalidRefreshRequest("profile is not valid for this datasource")
        return profile

    @staticmethod
    def _validate_scope(profile: RefreshProfile, request: RefreshRequest) -> None:
        scope_keys = frozenset(request.bounded_scope)
        if not scope_keys <= profile.allowed_scope_keys:
            raise InvalidRefreshRequest("scope has keys not allowed by this profile")
        if not profile.required_scope_keys <= scope_keys:
            raise InvalidRefreshRequest("scope is missing a required key")
        for key, values in request.bounded_scope.items():
            if len(values) > profile.max_scope_values:
                raise InvalidRefreshRequest("scope exceeds the profile value limit")
            if key in profile.single_value_scope_keys and len(values) != 1:
                raise InvalidRefreshRequest("scope key requires exactly one value")
            if key.lower() in {"url", "host", "endpoint", "path"} or any(
                value.startswith(("http://", "https://")) for value in values
            ):
                raise InvalidRefreshRequest("refresh scope cannot supply a network location")

    @staticmethod
    def _request_fingerprint(request: RefreshRequest) -> str:
        # The confirmation token proves a second deliberate call; it does not
        # alter the bounded refresh semantics that the durable request ID binds.
        return sha256(
            json.dumps(
                {
                    "datasource_id": request.datasource_id,
                    "request_profile": request.request_profile,
                    "bounded_scope": {
                        key: list(value)
                        for key, value in sorted(request.bounded_scope.items())
                    },
                    "intent": request.intent,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def _dedupe_key(cls, profile: RefreshProfile, request: RefreshRequest) -> str:
        return sha256(
            json.dumps(
                {
                    "datasource_id": profile.datasource_id,
                    "definition_version": profile.definition_version,
                    "profile": profile.profile_id,
                    "scope": {
                        key: list(value)
                        for key, value in sorted(request.bounded_scope.items())
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _acknowledgement(
        profile: RefreshProfile,
        context: RefreshContext,
        submitted_at: datetime,
        result: BackendSubmitResult,
    ) -> RefreshAcknowledgement:
        return RefreshAcknowledgement(
            schema_version=REFRESH_SCHEMA_VERSION,
            request_id=context.request_instance_id,
            job_id=result.job_id,
            disposition=result.disposition,
            effective_lane=profile.effective_lane,
            initial_state=result.initial_state,
            submitted_at=result.submitted_at or submitted_at,
            poll_after=profile.poll_after,
            canonical_anchor=result.canonical_anchor,
            confirmation_token=result.confirmation_token,
            confirmation_expires_at=result.confirmation_expires_at,
        )

TrustedRefreshBroker = RefreshBroker


def request_refresh_v1(
    broker: RefreshBroker,
    context: RefreshContext,
    request: RefreshRequest,
) -> RefreshAcknowledgement:
    """Stable effectful entry point; it only enqueues a durable job."""

    return broker.request(context, request)


def get_refresh_status_v1(
    broker: RefreshBroker,
    context: RefreshContext,
    job_id: str,
    *,
    wait_deadline: datetime | None = None,
) -> RefreshStatus | None:
    return broker.get_status(context, job_id, wait_deadline=wait_deadline)
