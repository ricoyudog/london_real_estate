"""Bounded refresh-request contracts.

The public request intentionally contains neither an endpoint, a lane, a
definition version, nor a promotion instruction.  Those are selected by an
approved profile held by the trusted broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable


REFRESH_SCHEMA_VERSION = "refresh_api.v1"
LANES = frozenset({"production_ingestion", "source_discovery", "ad_hoc_research"})


class RefreshApiError(ValueError):
    code = "REFRESH_API_ERROR"


class InvalidRefreshRequest(RefreshApiError):
    code = "INVALID_REFRESH_REQUEST"


class RefreshAccessDenied(RefreshApiError):
    code = "REFRESH_ACCESS_DENIED"


class RefreshDisposition(StrEnum):
    ACCEPTED = "accepted"
    DEDUPLICATED = "deduplicated"
    ALREADY_FRESH = "already_fresh"


def normalise_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidRefreshRequest("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _normalise_scope(scope: Mapping[str, object]) -> Mapping[str, tuple[str, ...]]:
    normalised: dict[str, tuple[str, ...]] = {}
    for key, value in scope.items():
        if not isinstance(key, str) or not key:
            raise InvalidRefreshRequest("scope keys must be non-empty strings")
        if isinstance(value, str):
            values = (value,)
        elif isinstance(value, tuple) and all(isinstance(item, str) for item in value):
            values = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            values = tuple(value)
        else:
            raise InvalidRefreshRequest(f"scope {key!r} must contain strings")
        if not values or any(not item for item in values):
            raise InvalidRefreshRequest(f"scope {key!r} must not be empty")
        if any(len(item) > 256 for item in values):
            raise InvalidRefreshRequest(f"scope {key!r} contains an overlong value")
        normalised[key] = values
    return MappingProxyType(normalised)


@dataclass(frozen=True)
class RefreshContext:
    """Trusted host capability for a single request instance."""

    principal: str
    request_instance_id: str
    allowed_profiles: frozenset[str]

    def __post_init__(self) -> None:
        if not self.principal or not self.principal.strip():
            raise ValueError("RefreshContext.principal must be non-empty")
        if len(self.principal) > 256:
            raise ValueError("RefreshContext.principal must be at most 256 characters")
        if not self.request_instance_id or not self.request_instance_id.strip():
            raise ValueError("RefreshContext.request_instance_id must be non-empty")
        if len(self.request_instance_id) > 256:
            raise ValueError(
                "RefreshContext.request_instance_id must be at most 256 characters"
            )
        profiles = frozenset(self.allowed_profiles)
        if not profiles or any(not profile or not profile.strip() for profile in profiles):
            raise ValueError("RefreshContext.allowed_profiles must be non-empty")
        object.__setattr__(self, "allowed_profiles", profiles)


@dataclass(frozen=True)
class RefreshRequest:
    """The only datasource write-like request exposed to an agent adapter."""

    datasource_id: str
    request_profile: str
    bounded_scope: Mapping[str, object] = field(default_factory=dict)
    intent: str = "user_requested_refresh"

    def __post_init__(self) -> None:
        if not self.datasource_id or not self.datasource_id.strip():
            raise InvalidRefreshRequest("datasource_id must be non-empty")
        if not self.request_profile or not self.request_profile.strip():
            raise InvalidRefreshRequest("request_profile must be non-empty")
        if not self.intent or len(self.intent) > 240:
            raise InvalidRefreshRequest("intent must be between 1 and 240 characters")
        object.__setattr__(self, "bounded_scope", _normalise_scope(self.bounded_scope))


@dataclass(frozen=True)
class RefreshProfile:
    """Registry-derived fixed request template selected by the broker."""

    profile_id: str
    datasource_id: str
    definition_version: int
    effective_lane: str
    allowed_scope_keys: frozenset[str] = field(default_factory=frozenset)
    required_scope_keys: frozenset[str] = field(default_factory=frozenset)
    single_value_scope_keys: frozenset[str] = field(default_factory=frozenset)
    max_scope_values: int = 10
    cooldown: timedelta = timedelta(minutes=5)
    poll_after: timedelta = timedelta(seconds=2)
    promotion_policy: str = "registry_selected"

    def __post_init__(self) -> None:
        if not self.profile_id or not self.datasource_id:
            raise ValueError("profile_id and datasource_id must be non-empty")
        if self.definition_version < 1:
            raise ValueError("definition_version must be positive")
        if self.effective_lane not in LANES:
            raise ValueError("effective_lane is not valid")
        scope_keys = frozenset(self.allowed_scope_keys)
        if any(not key or not key.strip() for key in scope_keys):
            raise ValueError("allowed scope keys must be non-empty")
        required_keys = frozenset(self.required_scope_keys)
        single_value_keys = frozenset(self.single_value_scope_keys)
        if not required_keys <= scope_keys:
            raise ValueError("required scope keys must be allowed")
        if not single_value_keys <= scope_keys:
            raise ValueError("single-value scope keys must be allowed")
        if not 1 <= self.max_scope_values <= 100:
            raise ValueError("max_scope_values must be between 1 and 100")
        if self.cooldown < timedelta(0):
            raise ValueError("cooldown must not be negative")
        if self.poll_after <= timedelta(0):
            raise ValueError("poll_after must be positive")
        object.__setattr__(self, "allowed_scope_keys", scope_keys)
        object.__setattr__(self, "required_scope_keys", required_keys)
        object.__setattr__(self, "single_value_scope_keys", single_value_keys)


@dataclass(frozen=True)
class RefreshSubmission:
    """Private broker-to-daemon adapter request with policy-selected fields."""

    request_id: str
    dedupe_key: str
    principal: str
    datasource_id: str
    definition_version: int
    request_profile: str
    effective_lane: str
    bounded_scope: Mapping[str, tuple[str, ...]]
    intent: str
    submitted_at: datetime
    promotion_policy: str
    request_fingerprint: str
    cooldown_until: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("request_id", self.request_id),
            ("principal", self.principal),
            ("dedupe_key", self.dedupe_key),
            ("request_fingerprint", self.request_fingerprint),
        ):
            if not isinstance(value, str) or not value:
                raise InvalidRefreshRequest(f"{name} must be a non-empty string")
        for name, value in (
            ("dedupe_key", self.dedupe_key),
            ("request_fingerprint", self.request_fingerprint),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise InvalidRefreshRequest(f"{name} must be a SHA-256 hex digest")
        submitted_at = normalise_utc(self.submitted_at)
        cooldown_until = normalise_utc(self.cooldown_until)
        if cooldown_until < submitted_at:
            raise InvalidRefreshRequest("cooldown_until must not precede submitted_at")
        object.__setattr__(self, "submitted_at", submitted_at)
        object.__setattr__(self, "cooldown_until", cooldown_until)


@dataclass(frozen=True)
class BackendSubmitResult:
    """Narrow daemon-adapter response; it cannot return a collector callable."""

    disposition: RefreshDisposition
    job_id: str | None
    initial_state: str
    canonical_anchor: datetime | None = None
    submitted_at: datetime | None = None

    def __post_init__(self) -> None:
        disposition = RefreshDisposition(self.disposition)
        if disposition is not RefreshDisposition.ALREADY_FRESH and not self.job_id:
            raise ValueError("a queued refresh result requires a job_id")
        object.__setattr__(self, "disposition", disposition)
        if self.canonical_anchor is not None:
            object.__setattr__(self, "canonical_anchor", normalise_utc(self.canonical_anchor))
        if self.submitted_at is not None:
            object.__setattr__(self, "submitted_at", normalise_utc(self.submitted_at))


@dataclass(frozen=True)
class RefreshAcknowledgement:
    schema_version: str
    request_id: str
    job_id: str | None
    disposition: RefreshDisposition
    effective_lane: str
    initial_state: str
    submitted_at: datetime
    poll_after: timedelta
    canonical_anchor: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "disposition", RefreshDisposition(self.disposition))
        object.__setattr__(self, "submitted_at", normalise_utc(self.submitted_at))
        if self.canonical_anchor is not None:
            object.__setattr__(self, "canonical_anchor", normalise_utc(self.canonical_anchor))


@dataclass(frozen=True)
class RefreshStatus:
    schema_version: str
    job_id: str
    job_state: str
    latest_attempt_status: str | None = None
    retry_after: timedelta | None = None
    terminal_run_id: str | None = None
    terminal_error: Mapping[str, object] | None = None
    promotion_status: str | None = None
    canonical_changed: bool | None = None
    result_ref: str | None = None
    observation_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    datasource_health: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.job_id:
            raise ValueError("job_id must be non-empty")
        if self.retry_after is not None and self.retry_after < timedelta(0):
            raise ValueError("retry_after must not be negative")
        observation_ids = tuple(self.observation_ids)
        evidence_ids = tuple(self.evidence_ids)
        if any(not value for value in observation_ids + evidence_ids):
            raise ValueError("lineage IDs must be non-empty")
        object.__setattr__(self, "observation_ids", observation_ids)
        object.__setattr__(self, "evidence_ids", evidence_ids)
        if self.terminal_error is not None:
            object.__setattr__(self, "terminal_error", MappingProxyType(dict(self.terminal_error)))
        object.__setattr__(self, "datasource_health", MappingProxyType(dict(self.datasource_health)))


@runtime_checkable
class RefreshBackend(Protocol):
    """Trusted daemon-socket adapter boundary used by ``RefreshBroker``."""

    def submit(self, submission: RefreshSubmission) -> BackendSubmitResult: ...

    def get_status(
        self,
        job_id: str,
        *,
        principal: str,
        wait_deadline: datetime | None = None,
    ) -> RefreshStatus | None: ...


class InMemoryRefreshBackend:
    """A deterministic backend for tests and local contract demonstrations."""

    def __init__(self) -> None:
        self._status_by_job: dict[str, RefreshStatus] = {}
        self._requests: dict[str, tuple[str, str, BackendSubmitResult]] = {}
        self._dedupe: dict[str, tuple[datetime, BackendSubmitResult]] = {}
        self._job_principals: dict[str, set[str]] = {}
        self._counter = 0

    def submit(self, submission: RefreshSubmission) -> BackendSubmitResult:
        existing = self._requests.get(submission.request_id)
        if existing is not None:
            principal, fingerprint, result = existing
            if principal != submission.principal:
                raise RefreshAccessDenied("request_instance_id belongs to another principal")
            if fingerprint != submission.request_fingerprint:
                raise InvalidRefreshRequest(
                    "request_instance_id was reused for a different request"
                )
            return result
        prior = self._dedupe.get(submission.dedupe_key)
        if prior is not None and submission.submitted_at < prior[0]:
            prior_result = prior[1]
            result = BackendSubmitResult(
                disposition=RefreshDisposition.DEDUPLICATED,
                job_id=prior_result.job_id,
                initial_state=prior_result.initial_state,
                canonical_anchor=prior_result.canonical_anchor,
                submitted_at=submission.submitted_at,
            )
        else:
            self._counter += 1
            job_id = f"job_in_memory_{self._counter}"
            result = BackendSubmitResult(
                disposition=RefreshDisposition.ACCEPTED,
                job_id=job_id,
                initial_state="queued",
                submitted_at=submission.submitted_at,
            )
            self._status_by_job[job_id] = RefreshStatus(
                schema_version=REFRESH_SCHEMA_VERSION,
                job_id=job_id,
                job_state="queued",
            )
            self._dedupe[submission.dedupe_key] = (submission.cooldown_until, result)
        self._requests[submission.request_id] = (
            submission.principal,
            submission.request_fingerprint,
            result,
        )
        if result.job_id is not None:
            self._job_principals.setdefault(result.job_id, set()).add(submission.principal)
        return result

    def get_status(
        self,
        job_id: str,
        *,
        principal: str,
        wait_deadline: datetime | None = None,
    ) -> RefreshStatus | None:
        if principal not in self._job_principals.get(job_id, set()):
            raise RefreshAccessDenied("job is not visible to this context")
        return self._status_by_job.get(job_id)

    def set_status(self, status: RefreshStatus) -> None:
        self._status_by_job[status.job_id] = status
