"""Durable-job state primitives with an injectable clock and no database import.

The in-memory queue is a reference implementation for offline tests and local
adapters.  A SQLite repository can persist the same immutable job/attempt
objects and use the transition helpers without exposing a writer to consumers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import calendar
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from random import Random
from threading import RLock
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .canonical import (
    CanonicalizationError,
    freeze_json,
    hash_canonical,
    new_id,
    request_hash,
    thaw_json,
)
from .policies import redact_secrets


class JobError(ValueError):
    """Raised when a job transition violates the durable state model."""


class StaleClaimError(JobError):
    """Raised when a worker attempts to use an expired or replaced lease."""


class JobState(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    EMPTY = "empty"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


class AttemptStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    EMPTY = "empty"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(str, Enum):
    SCHEDULED_INGEST = "scheduled_ingest"
    ON_DEMAND_REFRESH = "on_demand_refresh"
    FANOUT = "fanout"
    BACKFILL = "backfill"
    OFFLINE_REPARSE = "offline_reparse"
    PROMOTION_REACQUIRE = "promotion_reacquire"
    MANUAL_SUBMISSION = "manual_submission"
    HEALTH_RECONCILE = "health_reconcile"
    SNAPSHOT = "snapshot"
    ALERT_EVALUATE = "alert_evaluate"
    WIKI_RENDER = "wiki_render"
    INTEGRITY_CHECK = "integrity_check"
    BACKUP = "backup"
    RETENTION = "retention"


class Trigger(str, Enum):
    SCHEDULE = "schedule"
    AGENT_REQUEST = "agent_request"
    MANUAL = "manual"
    BACKFILL = "backfill"
    FANOUT = "fanout"
    REPARSE = "reparse"
    PROMOTION = "promotion"
    RECOVERY = "recovery"


class CatchupPolicy(str, Enum):
    LATEST_ONLY = "latest_only"
    WINDOWED = "windowed"
    ALL_SLOTS = "all_slots"
    MANUAL = "manual"


_DATASOURCE_JOB_KINDS = frozenset(
    {
        JobKind.SCHEDULED_INGEST,
        JobKind.ON_DEMAND_REFRESH,
        JobKind.FANOUT,
        JobKind.BACKFILL,
        JobKind.OFFLINE_REPARSE,
        JobKind.PROMOTION_REACQUIRE,
        JobKind.MANUAL_SUBMISSION,
    }
)
_TERMINAL_JOB_STATES = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.EMPTY,
        JobState.FAILED,
        JobState.DEAD_LETTER,
        JobState.CANCELLED,
    }
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise JobError("job timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _hash64(value: str | None, name: str, *, required: bool = False) -> None:
    if value is None:
        if required:
            raise JobError(f"{name} is required")
        return
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise JobError(f"{name} must be lower-case SHA-256 hex")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff; inject ``rng`` for deterministic jitter."""

    max_attempts: int = 3
    base_delay_seconds: int = 60
    max_delay_seconds: int = 3_600
    jitter_ratio: float = 0.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 20:
            raise JobError("max_attempts must be in [1, 20]")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < self.base_delay_seconds:
            raise JobError("invalid retry delay bounds")
        if not 0 <= self.jitter_ratio <= 1:
            raise JobError("jitter_ratio must be in [0, 1]")

    def delay_for(self, attempt_no: int, rng: Random | None = None) -> timedelta:
        if attempt_no < 1:
            raise JobError("attempt_no must be positive")
        seconds = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** (attempt_no - 1)),
        )
        if self.jitter_ratio:
            random_value = (rng or Random()).random()
            seconds *= 1 - self.jitter_ratio + (2 * self.jitter_ratio * random_value)
        return timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class WorkflowJob:
    """A single durable unit of work, independent of persistence technology."""

    job_id: str
    dedupe_key: str
    job_kind: JobKind
    trigger: Trigger
    scheduled_for: datetime
    available_at: datetime
    request: Any
    request_hash: str
    created_at: datetime
    datasource_id: str | None = None
    definition_version: int | None = None
    definition_hash: str | None = None
    lane: str | None = None
    priority: int = 100
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    state: JobState = JobState.QUEUED
    attempt_count: int = 0
    claim_token: str | None = None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_error: Any | None = None
    parent_job_id: str | None = None
    generation: int = 0
    request_instance_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_kind", JobKind(self.job_kind))
        object.__setattr__(self, "trigger", Trigger(self.trigger))
        object.__setattr__(self, "state", JobState(self.state))
        for name in ("scheduled_for", "available_at", "created_at"):
            object.__setattr__(self, name, _utc(getattr(self, name)))
        for name in (
            "claimed_at",
            "lease_expires_at",
            "heartbeat_at",
            "started_at",
            "completed_at",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value))
        _hash64(self.dedupe_key, "dedupe_key", required=True)
        _hash64(self.request_hash, "request_hash", required=True)
        _hash64(self.definition_hash, "definition_hash")
        if self.attempt_count < 0 or self.attempt_count > self.retry_policy.max_attempts:
            raise JobError("attempt_count must be within retry policy bounds")
        if self.generation < 0:
            raise JobError("generation cannot be negative")
        if self.job_kind in _DATASOURCE_JOB_KINDS:
            if not all(
                (
                    self.datasource_id,
                    self.definition_version is not None,
                    self.definition_hash,
                    self.lane,
                )
            ):
                raise JobError("datasource job requires datasource definition and lane")
        elif any(
            (
                self.datasource_id is not None,
                self.definition_version is not None,
                self.definition_hash is not None,
                self.lane is not None,
            )
        ):
            raise JobError("system job cannot carry datasource fields")
        if self.trigger in {Trigger.AGENT_REQUEST, Trigger.MANUAL, Trigger.RECOVERY} and not self.request_instance_id:
            raise JobError("agent, manual, and recovery jobs require request_instance_id")
        if self.trigger == Trigger.RECOVERY and (not self.parent_job_id or self.generation < 1):
            raise JobError("recovery job requires parent_job_id and positive generation")
        active = self.state in {JobState.CLAIMED, JobState.RUNNING}
        claim_fields = (
            self.claim_token,
            self.claimed_by,
            self.claimed_at,
            self.lease_expires_at,
        )
        if active and any(field is None for field in claim_fields):
            raise JobError("claimed/running job requires complete lease fields")
        if not active and any(field is not None for field in claim_fields):
            raise JobError("unclaimed job cannot retain lease fields")
        if self.state in _TERMINAL_JOB_STATES and self.completed_at is None:
            raise JobError("terminal job requires completed_at")
        if self.state not in _TERMINAL_JOB_STATES and self.completed_at is not None:
            raise JobError("non-terminal job cannot have completed_at")
        object.__setattr__(self, "request", freeze_json(redact_secrets(thaw_json(self.request))))
        if self.last_error is not None:
            object.__setattr__(self, "last_error", freeze_json(redact_secrets(thaw_json(self.last_error))))

    @classmethod
    def create(
        cls,
        *,
        job_kind: JobKind,
        trigger: Trigger,
        scheduled_for: datetime,
        request: Mapping[str, Any] | None = None,
        datasource_id: str | None = None,
        definition_version: int | None = None,
        definition_hash: str | None = None,
        lane: str | None = None,
        priority: int = 100,
        retry_policy: RetryPolicy | None = None,
        now: datetime | None = None,
        request_instance_id: str | None = None,
        parent_job_id: str | None = None,
        generation: int = 0,
        dedupe_scope: Mapping[str, Any] | None = None,
    ) -> "WorkflowJob":
        created = _utc(now or datetime.now(UTC))
        scheduled = _utc(scheduled_for)
        redacted_request = redact_secrets(request or {})
        hashed_request = request_hash(redacted_request)
        dedupe_key = hash_canonical(
            "job-dedupe",
            {
                "job_kind": JobKind(job_kind).value,
                "trigger": Trigger(trigger).value,
                "datasource_id": datasource_id,
                "definition_version": definition_version,
                "definition_hash": definition_hash,
                "lane": lane,
                "scheduled_for": scheduled,
                "request_hash": hashed_request,
                "dedupe_scope": dedupe_scope or {},
            },
        )
        return cls(
            job_id=new_id("job"),
            dedupe_key=dedupe_key,
            job_kind=JobKind(job_kind),
            trigger=Trigger(trigger),
            scheduled_for=scheduled,
            available_at=scheduled,
            request=redacted_request,
            request_hash=hashed_request,
            created_at=created,
            datasource_id=datasource_id,
            definition_version=definition_version,
            definition_hash=definition_hash,
            lane=lane,
            priority=priority,
            retry_policy=retry_policy or RetryPolicy(),
            parent_job_id=parent_job_id,
            generation=generation,
            request_instance_id=request_instance_id,
        )

    def as_json(self) -> dict[str, Any]:
        def timestamp(value: datetime | None) -> str | None:
            return value.isoformat().replace("+00:00", "Z") if value else None

        return {
            "job_id": self.job_id,
            "dedupe_key": self.dedupe_key,
            "job_kind": self.job_kind.value,
            "datasource_id": self.datasource_id,
            "definition_version": self.definition_version,
            "definition_hash": self.definition_hash,
            "lane": self.lane,
            "trigger": self.trigger.value,
            "scheduled_for": timestamp(self.scheduled_for),
            "available_at": timestamp(self.available_at),
            "request": thaw_json(self.request),
            "request_hash": self.request_hash,
            "state": self.state.value,
            "attempt_count": self.attempt_count,
            "max_attempts": self.retry_policy.max_attempts,
            "claim_token": self.claim_token,
            "claimed_by": self.claimed_by,
            "claimed_at": timestamp(self.claimed_at),
            "lease_expires_at": timestamp(self.lease_expires_at),
            "heartbeat_at": timestamp(self.heartbeat_at),
            "last_error": thaw_json(self.last_error) if self.last_error is not None else None,
            "created_at": timestamp(self.created_at),
            "started_at": timestamp(self.started_at),
            "completed_at": timestamp(self.completed_at),
        }


@dataclass(frozen=True, slots=True)
class WorkflowAttempt:
    """Append-only worker attempt history for a claimed job."""

    attempt_id: str
    job_id: str
    attempt_no: int
    worker_id: str
    status: AttemptStatus
    started_at: datetime
    heartbeat_at: datetime
    completed_at: datetime | None = None
    warnings: Any = field(default_factory=tuple)
    error: Any | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", AttemptStatus(self.status))
        if self.attempt_no < 1 or not self.worker_id:
            raise JobError("attempt requires positive number and worker ID")
        for name in ("started_at", "heartbeat_at", "completed_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value))
        if self.status == AttemptStatus.RUNNING and self.completed_at is not None:
            raise JobError("running attempt cannot be completed")
        if self.status != AttemptStatus.RUNNING and self.completed_at is None:
            raise JobError("terminal attempt requires completed_at")
        object.__setattr__(self, "warnings", freeze_json(list(self.warnings)))
        if self.error is not None:
            object.__setattr__(self, "error", freeze_json(redact_secrets(thaw_json(self.error))))


@dataclass(frozen=True, slots=True)
class JobClaim:
    """Opaque lease credentials handed to a worker after an atomic claim."""

    job_id: str
    claim_token: str
    lease_expires_at: datetime


class Recurrence(Protocol):
    def next_after(self, value: datetime) -> datetime:
        """Return the first scheduled UTC slot strictly after ``value``."""


@dataclass(frozen=True, slots=True)
class IntervalSchedule:
    """Fixed UTC interval schedule with a stable anchor."""

    anchor_at: datetime
    interval: timedelta

    def __post_init__(self) -> None:
        object.__setattr__(self, "anchor_at", _utc(self.anchor_at))
        if self.interval.total_seconds() <= 0:
            raise JobError("interval must be positive")

    def next_after(self, value: datetime) -> datetime:
        current = _utc(value)
        if current < self.anchor_at:
            return self.anchor_at
        elapsed = current - self.anchor_at
        count = int(elapsed.total_seconds() // self.interval.total_seconds()) + 1
        return self.anchor_at + (self.interval * count)


@dataclass(frozen=True, slots=True)
class CalendarSchedule:
    """A local wall-clock recurrence that materialises one slot across DST folds."""

    hour: int
    minute: int
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    timezone: str = "Europe/London"

    def __post_init__(self) -> None:
        if not 0 <= self.hour <= 23 or not 0 <= self.minute <= 59:
            raise JobError("invalid calendar schedule time")
        if not self.weekdays or any(day not in range(7) for day in self.weekdays):
            raise JobError("weekdays must contain ISO weekday indexes 0..6")
        try:
            ZoneInfo(self.timezone)
        except Exception as error:
            raise JobError(f"unknown timezone: {self.timezone!r}") from error
        object.__setattr__(self, "weekdays", tuple(sorted(set(self.weekdays))))

    def _slot_for(self, local_day: date) -> datetime | None:
        if local_day.weekday() not in self.weekdays:
            return None
        zone = ZoneInfo(self.timezone)
        candidate = datetime(
            local_day.year,
            local_day.month,
            local_day.day,
            self.hour,
            self.minute,
            tzinfo=zone,
            fold=0,
        )
        # A nonexistent spring-forward wall time changes on round-trip.  Skip it;
        # an ambiguous fall-back time uses fold=0 exactly once.
        round_trip = candidate.astimezone(UTC).astimezone(zone)
        if round_trip.replace(tzinfo=None) != candidate.replace(tzinfo=None):
            return None
        return candidate.astimezone(UTC)

    def next_after(self, value: datetime) -> datetime:
        current = _utc(value)
        zone = ZoneInfo(self.timezone)
        local_day = current.astimezone(zone).date()
        for offset in range(4_000):
            candidate = self._slot_for(local_day + timedelta(days=offset))
            if candidate is not None and candidate > current:
                return candidate
        raise JobError("could not materialize calendar slot")


@dataclass(frozen=True, slots=True)
class MonthlySchedule:
    """A bounded local monthly recurrence without a cron parser.

    A schedule is either a calendar day (for example, the 15th) or an
    occurrence of a weekday (for example, the first Monday).  Invalid calendar
    days are skipped rather than silently shifted to a different day.
    """

    hour: int
    minute: int
    timezone: str = "Europe/London"
    day: int | None = None
    weekday: int | None = None
    week: int | None = None
    months: tuple[int, ...] = tuple(range(1, 13))

    def __post_init__(self) -> None:
        if not 0 <= self.hour <= 23 or not 0 <= self.minute <= 59:
            raise JobError("invalid monthly schedule time")
        if (self.day is None) == (self.weekday is None):
            raise JobError("monthly schedule requires exactly one of day or weekday")
        if self.day is not None and not 1 <= self.day <= 31:
            raise JobError("monthly day must be in [1, 31]")
        if self.weekday is not None and self.weekday not in range(7):
            raise JobError("monthly weekday must be an ISO weekday index 0..6")
        if self.weekday is not None and self.week not in {1, 2, 3, 4, 5}:
            raise JobError("monthly weekday schedule requires week in [1, 5]")
        selected_months = tuple(sorted(set(self.months)))
        if not selected_months or any(month not in range(1, 13) for month in selected_months):
            raise JobError("monthly schedule months must contain values 1..12")
        try:
            ZoneInfo(self.timezone)
        except Exception as error:
            raise JobError(f"unknown timezone: {self.timezone!r}") from error
        object.__setattr__(self, "months", selected_months)

    def _local_day(self, year: int, month: int) -> date | None:
        if month not in self.months:
            return None
        if self.day is not None:
            if self.day > calendar.monthrange(year, month)[1]:
                return None
            return date(year, month, self.day)
        assert self.weekday is not None and self.week is not None
        first_weekday, number_of_days = calendar.monthrange(year, month)
        delta = (self.weekday - first_weekday) % 7
        day = 1 + delta + ((self.week - 1) * 7)
        if day > number_of_days:
            return None
        return date(year, month, day)

    def _slot_for(self, year: int, month: int) -> datetime | None:
        local_day = self._local_day(year, month)
        if local_day is None:
            return None
        zone = ZoneInfo(self.timezone)
        candidate = datetime(
            local_day.year,
            local_day.month,
            local_day.day,
            self.hour,
            self.minute,
            tzinfo=zone,
            fold=0,
        )
        round_trip = candidate.astimezone(UTC).astimezone(zone)
        if round_trip.replace(tzinfo=None) != candidate.replace(tzinfo=None):
            return None
        return candidate.astimezone(UTC)

    def next_after(self, value: datetime) -> datetime:
        current = _utc(value)
        zone = ZoneInfo(self.timezone)
        local = current.astimezone(zone)
        year, month = local.year, local.month
        for _ in range(1_200):
            candidate = self._slot_for(year, month)
            if candidate is not None and candidate > current:
                return candidate
            month += 1
            if month == 13:
                year += 1
                month = 1
        raise JobError("could not materialize monthly slot")


def recurrence_from_rule(
    rule: Mapping[str, Any],
    *,
    timezone: str,
    anchor_at: datetime,
) -> Recurrence:
    """Build one of the registry's small, auditable schedule specifications."""

    kind = rule.get("kind")
    if kind == "interval":
        seconds = rule.get("seconds")
        if not isinstance(seconds, int):
            raise JobError("interval schedule requires integer seconds")
        return IntervalSchedule(anchor_at=anchor_at, interval=timedelta(seconds=seconds))
    if kind in {"daily", "weekly"}:
        hour = rule.get("hour")
        minute = rule.get("minute")
        if not isinstance(hour, int) or not isinstance(minute, int):
            raise JobError("calendar schedule requires integer hour and minute")
        if kind == "daily":
            weekdays = tuple(range(7))
        else:
            raw_weekdays = rule.get("weekdays", (rule.get("weekday"),))
            if not isinstance(raw_weekdays, Sequence) or isinstance(raw_weekdays, (str, bytes)):
                raise JobError("weekly schedule weekdays must be a sequence")
            if not all(isinstance(day, int) for day in raw_weekdays):
                raise JobError("weekly schedule weekdays must be integers")
            weekdays = tuple(raw_weekdays)
        return CalendarSchedule(hour=hour, minute=minute, weekdays=weekdays, timezone=timezone)
    if kind == "monthly":
        hour = rule.get("hour")
        minute = rule.get("minute")
        if not isinstance(hour, int) or not isinstance(minute, int):
            raise JobError("monthly schedule requires integer hour and minute")
        raw_months = rule.get("months", tuple(range(1, 13)))
        if not isinstance(raw_months, Sequence) or isinstance(raw_months, (str, bytes)):
            raise JobError("monthly schedule months must be a sequence")
        if not all(isinstance(month, int) for month in raw_months):
            raise JobError("monthly schedule months must be integers")
        day = rule.get("day")
        weekday = rule.get("weekday")
        week = rule.get("week")
        if day is not None and not isinstance(day, int):
            raise JobError("monthly schedule day must be an integer")
        if weekday is not None and not isinstance(weekday, int):
            raise JobError("monthly schedule weekday must be an integer")
        if week is not None and not isinstance(week, int):
            raise JobError("monthly schedule week must be an integer")
        return MonthlySchedule(
            hour=hour,
            minute=minute,
            timezone=timezone,
            day=day,
            weekday=weekday,
            week=week,
            months=tuple(raw_months),
        )
    raise JobError(f"unsupported schedule rule kind: {kind!r}")


@dataclass(frozen=True, slots=True)
class ScheduleMaterialization:
    """Due slots and a cursor that atomically records their materialization."""

    slots: tuple[datetime, ...]
    next_cursor_at: datetime | None
    skipped_slots: int = 0


def materialize_due_slots(
    recurrence: Recurrence,
    *,
    cursor_at: datetime | None,
    now: datetime,
    catchup_policy: CatchupPolicy = CatchupPolicy.LATEST_ONLY,
    max_catchup_jobs: int = 1,
    max_catchup_horizon: timedelta | None = None,
) -> ScheduleMaterialization:
    """Materialize due slots without sleeping or consulting wall-clock state."""

    current = _utc(now)
    policy = CatchupPolicy(catchup_policy)
    if max_catchup_jobs < 1:
        raise JobError("max_catchup_jobs must be positive")
    if policy == CatchupPolicy.MANUAL:
        return ScheduleMaterialization((), _utc(cursor_at) if cursor_at else None)
    if cursor_at is None:
        if max_catchup_horizon is None:
            return ScheduleMaterialization((), None)
        cursor = current - max_catchup_horizon
    else:
        cursor = _utc(cursor_at)
        if max_catchup_horizon is not None:
            cursor = max(cursor, current - max_catchup_horizon)
    slots: list[datetime] = []
    candidate = recurrence.next_after(cursor)
    safety_limit = 100_000
    while candidate <= current:
        slots.append(candidate)
        if len(slots) > safety_limit:
            raise JobError("schedule materialization exceeded safety limit")
        candidate = recurrence.next_after(candidate)
    if not slots:
        return ScheduleMaterialization((), cursor_at and _utc(cursor_at))
    if policy == CatchupPolicy.LATEST_ONLY:
        return ScheduleMaterialization((slots[-1],), slots[-1], len(slots) - 1)
    if policy == CatchupPolicy.ALL_SLOTS:
        # ALL_SLOTS is deliberately not constrained by the windowed backlog
        # cap: advancing the cursor while dropping a due slot would make that
        # historical work permanently unrecoverable.
        return ScheduleMaterialization(tuple(slots), slots[-1])
    selected = tuple(slots[:max_catchup_jobs])
    return ScheduleMaterialization(selected, selected[-1], len(slots) - len(selected))


class InMemoryJobQueue:
    """Thread-safe reference queue; storage-backed adapters should preserve its CAS rules."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._jobs: dict[str, WorkflowJob] = {}
        self._dedupe: dict[str, str] = {}
        self._attempts: dict[str, list[WorkflowAttempt]] = {}

    def enqueue(self, job: WorkflowJob) -> tuple[WorkflowJob, bool]:
        """Store a job, returning ``(existing_or_new_job, created)`` on dedupe."""

        with self._lock:
            existing_id = self._dedupe.get(job.dedupe_key)
            if existing_id is not None:
                return self._jobs[existing_id], False
            if job.job_id in self._jobs:
                raise JobError(f"duplicate job ID: {job.job_id}")
            self._jobs[job.job_id] = job
            self._dedupe[job.dedupe_key] = job.job_id
            self._attempts[job.job_id] = []
            return job, True

    def get(self, job_id: str) -> WorkflowJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def attempts(self, job_id: str) -> tuple[WorkflowAttempt, ...]:
        with self._lock:
            if job_id not in self._attempts:
                raise JobError(f"unknown job: {job_id}")
            return tuple(self._attempts[job_id])

    def claim(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease: timedelta = timedelta(minutes=5),
    ) -> JobClaim | None:
        """Atomically claim one due job; competing workers cannot share a token."""

        if not worker_id or lease.total_seconds() <= 0:
            raise JobError("worker_id and positive lease are required")
        current = _utc(now)
        with self._lock:
            self._recover_expired_locked(current)
            due = [
                job
                for job in self._jobs.values()
                if job.state in {JobState.QUEUED, JobState.RETRY_WAIT}
                and job.available_at <= current
            ]
            if not due:
                return None
            selected = min(
                due,
                key=lambda job: (job.priority, job.available_at, job.scheduled_for, job.job_id),
            )
            token = new_id("claim")
            expires = current + lease
            claimed = replace(
                selected,
                state=JobState.CLAIMED,
                claim_token=token,
                claimed_by=worker_id,
                claimed_at=current,
                lease_expires_at=expires,
                heartbeat_at=current,
            )
            self._jobs[selected.job_id] = claimed
            return JobClaim(claimed.job_id, token, expires)

    def start(self, claim: JobClaim, *, now: datetime) -> WorkflowAttempt:
        """Start the claimed work and append a running attempt."""

        current = _utc(now)
        with self._lock:
            job = self._require_claim_locked(claim, current, expected=JobState.CLAIMED)
            attempt = WorkflowAttempt(
                attempt_id=new_id("attempt"),
                job_id=job.job_id,
                attempt_no=job.attempt_count + 1,
                worker_id=job.claimed_by or "",
                status=AttemptStatus.RUNNING,
                started_at=current,
                heartbeat_at=current,
            )
            running = replace(
                job,
                state=JobState.RUNNING,
                attempt_count=attempt.attempt_no,
                started_at=job.started_at or current,
                heartbeat_at=current,
            )
            self._jobs[job.job_id] = running
            self._attempts[job.job_id].append(attempt)
            return attempt

    def heartbeat(
        self,
        claim: JobClaim,
        *,
        now: datetime,
        lease: timedelta = timedelta(minutes=5),
    ) -> WorkflowJob:
        """Extend the current lease only when the caller presents its token."""

        if lease.total_seconds() <= 0:
            raise JobError("lease must be positive")
        current = _utc(now)
        with self._lock:
            job = self._require_claim_locked(claim, current)
            extended = replace(
                job,
                heartbeat_at=current,
                lease_expires_at=current + lease,
            )
            self._jobs[job.job_id] = extended
            attempts = self._attempts[job.job_id]
            if attempts and attempts[-1].status == AttemptStatus.RUNNING:
                attempts[-1] = replace(attempts[-1], heartbeat_at=current)
            return extended

    def finish(
        self,
        claim: JobClaim,
        *,
        status: AttemptStatus,
        now: datetime,
        retryable: bool = False,
        error: Mapping[str, Any] | None = None,
        warnings: Sequence[Any] = (),
        rng: Random | None = None,
    ) -> WorkflowJob:
        """Compare-and-set completion of a running attempt and its job."""

        terminal_status = AttemptStatus(status)
        if terminal_status == AttemptStatus.RUNNING:
            raise JobError("cannot finish an attempt as running")
        current = _utc(now)
        with self._lock:
            job = self._require_claim_locked(claim, current, expected=JobState.RUNNING)
            attempts = self._attempts[job.job_id]
            if not attempts or attempts[-1].status != AttemptStatus.RUNNING:
                raise JobError("running job has no running attempt")
            attempts[-1] = replace(
                attempts[-1],
                status=terminal_status,
                completed_at=current,
                heartbeat_at=current,
                error=error,
                warnings=warnings,
            )
            completed = self._complete_job(
                job,
                status=terminal_status,
                now=current,
                retryable=retryable,
                error=error,
                rng=rng,
            )
            self._jobs[job.job_id] = completed
            return completed

    def recover_expired(self, *, now: datetime, rng: Random | None = None) -> tuple[WorkflowJob, ...]:
        """Recover abandoned claims/running attempts without real sleep."""

        with self._lock:
            return tuple(self._recover_expired_locked(_utc(now), rng=rng))

    def cancel(self, job_id: str, *, now: datetime) -> WorkflowJob:
        """Cancel a queued/retry job; active work must use lease-aware shutdown."""

        current = _utc(now)
        with self._lock:
            try:
                job = self._jobs[job_id]
            except KeyError as error:
                raise JobError(f"unknown job: {job_id}") from error
            if job.state not in {JobState.QUEUED, JobState.RETRY_WAIT}:
                raise JobError("only queued or retry-wait jobs can be cancelled")
            cancelled = replace(job, state=JobState.CANCELLED, completed_at=current)
            self._jobs[job_id] = cancelled
            return cancelled

    def _require_claim_locked(
        self,
        claim: JobClaim,
        now: datetime,
        *,
        expected: JobState | None = None,
    ) -> WorkflowJob:
        try:
            job = self._jobs[claim.job_id]
        except KeyError as error:
            raise StaleClaimError("job no longer exists") from error
        if job.claim_token != claim.claim_token:
            raise StaleClaimError("claim token no longer matches job")
        if job.lease_expires_at is None or job.lease_expires_at <= now:
            raise StaleClaimError("claim lease has expired")
        if expected is not None and job.state != expected:
            raise StaleClaimError(
                f"claim is in {job.state.value}, expected {expected.value}"
            )
        return job

    def _complete_job(
        self,
        job: WorkflowJob,
        *,
        status: AttemptStatus,
        now: datetime,
        retryable: bool,
        error: Mapping[str, Any] | None,
        rng: Random | None,
    ) -> WorkflowJob:
        clear_claim = {
            "claim_token": None,
            "claimed_by": None,
            "claimed_at": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
        }
        if status == AttemptStatus.SUCCEEDED:
            return replace(
                job,
                **clear_claim,
                state=JobState.SUCCEEDED,
                completed_at=now,
                last_error=None,
            )
        if status == AttemptStatus.EMPTY:
            return replace(
                job,
                **clear_claim,
                state=JobState.EMPTY,
                completed_at=now,
                last_error=None,
            )
        if status == AttemptStatus.CANCELLED:
            return replace(
                job,
                **clear_claim,
                state=JobState.CANCELLED,
                completed_at=now,
                last_error=error,
            )
        if retryable and job.attempt_count < job.retry_policy.max_attempts:
            return replace(
                job,
                **clear_claim,
                state=JobState.RETRY_WAIT,
                available_at=now + job.retry_policy.delay_for(job.attempt_count, rng),
                last_error=error,
            )
        return replace(
            job,
            **clear_claim,
            state=JobState.DEAD_LETTER if retryable else JobState.FAILED,
            completed_at=now,
            last_error=error,
        )

    def _recover_expired_locked(
        self, now: datetime, rng: Random | None = None
    ) -> list[WorkflowJob]:
        recovered: list[WorkflowJob] = []
        for job_id, job in tuple(self._jobs.items()):
            if job.state not in {JobState.CLAIMED, JobState.RUNNING}:
                continue
            if job.lease_expires_at is None or job.lease_expires_at > now:
                continue
            if job.state == JobState.CLAIMED:
                recovered_job = replace(
                    job,
                    state=JobState.QUEUED,
                    available_at=now,
                    claim_token=None,
                    claimed_by=None,
                    claimed_at=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    last_error={"code": "lease_expired_before_start"},
                )
            else:
                attempts = self._attempts[job_id]
                if attempts and attempts[-1].status == AttemptStatus.RUNNING:
                    attempts[-1] = replace(
                        attempts[-1],
                        status=AttemptStatus.FAILED,
                        completed_at=now,
                        heartbeat_at=now,
                        error={"code": "lease_expired"},
                    )
                recovered_job = self._complete_job(
                    job,
                    status=AttemptStatus.FAILED,
                    now=now,
                    retryable=True,
                    error={"code": "lease_expired"},
                    rng=rng,
                )
            self._jobs[job_id] = recovered_job
            recovered.append(recovered_job)
        return recovered
