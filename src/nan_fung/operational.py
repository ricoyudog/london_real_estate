"""Single-writer SQLite operations that join registry, evidence, and lifecycle."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
import fcntl
from email.utils import parsedate_to_datetime
from functools import wraps
from hashlib import sha256
import os
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping
from zoneinfo import ZoneInfo

from nan_fung.ingestion.canonical import (
    canonical_json,
    definition_hash,
    locator_hash,
    new_id,
    observation_hash,
    record_key_hash,
    request_hash,
)
from nan_fung.ingestion.registry import (
    DatasourceDefinitionDescriptor,
    DatasourceRegistry,
    RegistryError,
    SourceBinding,
    default_registry,
    default_runtime_bindings,
    RuntimeBindings,
)
from nan_fung.ingestion.jobs import (
    CatchupPolicy,
    JobError,
    materialize_due_slots,
    recurrence_from_rule,
)
from nan_fung.ingestion.parser_runner import parser_isolation_status
from nan_fung.ingestion.policies import (
    ArtifactPolicy,
    PolicyError,
    SourcePolicy,
    redact_secrets,
    validate_artifact_bytes,
    validate_artifact_file,
    validate_source_url,
)
from nan_fung.ingestion.submarket_mapping import (
    SUBMARKET_MAPPING_DATASOURCE_ID,
    SubmarketMappingError,
    mapping_import_error_code,
    persist_submarket_mapping_observation,
    validate_submarket_mapping_submission,
)
from nan_fung.datasources.catalog import datasource_workflow_catalog
from nan_fung.datasources.common import HostThrottleBlocked
from nan_fung.storage.artifacts import ArtifactStore, StoredArtifact
from nan_fung.storage.db import (
    MigrationRunner,
    backup_database,
    connect_database,
    integrity_check,
    transaction,
)


class OperationalError(RuntimeError):
    """A stable domain error for CLI and trusted daemon adapters."""


class WriterAlreadyRunningError(OperationalError):
    """A second process attempted to mutate one datasource data directory."""


class RefreshRequestAccessError(OperationalError):
    """A durable refresh request belongs to a different principal."""


class RefreshRequestReplayError(OperationalError):
    """A durable request ID was reused with different request semantics."""


class RefreshConfirmationError(OperationalError):
    """A bounded refresh did not satisfy its required second confirmation."""


class RefreshApprovalError(OperationalError):
    """A host-only approval mapping is invalid or unavailable."""


class RefreshApprovalAccessError(RefreshApprovalError):
    """An approval is not bound to the caller's trusted host context."""


class RefreshApprovalExpiredError(RefreshApprovalError):
    """A durable approval mapping or its confirmation has expired."""


class RefreshApprovalReplayError(RefreshApprovalError):
    """A host attempted to reuse an approval with changed request semantics."""


class ApprovalDecisionConflictError(RefreshApprovalError):
    """A conflicting approve/deny decision followed an immutable decision."""


_ONSPD_REFRESH_DATASOURCE_ID = "ons.onspd.postcode"
_ONSPD_REFRESH_DAILY_LIMIT = 20
_ONSPD_REFRESH_TIMEZONE = ZoneInfo("Europe/London")
_REFRESH_CONFIRMATION_TTL = timedelta(minutes=10)


def _single_writer(method: Callable[..., Any]) -> Callable[..., Any]:
    """Keep every OperationalStore mutation under its re-entrant writer lease."""

    @wraps(method)
    def guarded(self: "OperationalStore", *args: Any, **kwargs: Any) -> Any:
        with self.writer_session():
            return method(self, *args, **kwargs)

    return guarded


@dataclass(frozen=True)
class EnqueueResult:
    job_id: str
    disposition: str
    state: str


@dataclass(frozen=True)
class DurableRefreshResult:
    """Persisted refresh submission identity returned to the trusted broker."""

    job_id: str | None
    disposition: str
    initial_state: str
    submitted_at: datetime
    confirmation_token: str | None = None
    confirmation_expires_at: datetime | None = None


@dataclass(frozen=True)
class AgentRefreshApproval:
    """Durable host-only binding for an approval-required refresh request.

    The normalized snapshot deliberately contains no confirmation token.  It
    is sufficient for a trusted host to rebuild the original ``RefreshRequest``
    after a facade subprocess restart.
    """

    approval_id: str
    refresh_request_id: str
    principal: str
    capability_scope_id: str
    capability_id: str
    manifest_version: str
    profile_version: str
    request_fingerprint: str
    snapshot: Mapping[str, object]
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class RecoveredAgentRefreshApproval:
    """Trusted host recovery result; token repr is deliberately suppressed."""

    approval: AgentRefreshApproval
    confirmation_token: str = field(repr=False)

    @property
    def snapshot(self) -> Mapping[str, object]:
        return self.approval.snapshot


@dataclass(frozen=True)
class AgentRefreshApprovalDecision:
    """One append-only host decision or its idempotent replay event."""

    approval_id: str
    event_id: str
    decision: str
    outcome: str
    decided_at: datetime


@dataclass(frozen=True)
class ClaimedJob:
    job_id: str
    claim_token: str
    datasource_id: str | None
    definition_version: int | None
    lane: str | None
    job_kind: str
    window_start: datetime | None
    window_end: datetime | None
    request: Mapping[str, Any]


@dataclass(frozen=True)
class RunHandle:
    run_id: str
    attempt_id: str
    job_id: str
    claim_token: str
    datasource_id: str
    definition_version: int
    lane: str


@dataclass(frozen=True)
class SystemJobHandle:
    """A claimed system task with no datasource ingestion run."""

    attempt_id: str
    job_id: str
    claim_token: str
    job_kind: str


@dataclass(frozen=True)
class PersistedEvidence:
    evidence_id: str
    artifact: StoredArtifact


@dataclass(frozen=True)
class ManualEvidenceResult:
    run_id: str
    evidence_id: str
    review_id: str | None
    state: str


@dataclass(frozen=True)
class ManualPromotionResult:
    review_id: str
    run_id: str
    promotion_id: str
    created: bool


@dataclass(frozen=True)
class PromotionRevocationResult:
    """Append-only result of withdrawing a canonical run promotion."""

    run_id: str
    promotion_id: str
    created: bool


@dataclass(frozen=True)
class HostThrottleState:
    rate_limit_group: str
    next_allowed_at: datetime | None
    blocked_until: datetime | None
    last_http_status: int | None
    updated_at: datetime


_HOST_PERMIT_INTERVAL = timedelta(seconds=1)


class OperationalStore:
    """The only mutation path used by CLI/daemon integrations.

    The class is intentionally small and synchronous.  A service daemon owns
    its process-level writer lock; each method nevertheless uses short
    transactions and compare-and-set claim tokens to avoid stale workers
    committing work after a lease has been recovered.
    """

    def __init__(
        self,
        data_dir: str | Path,
        *,
        backup_dir: str | Path | None = None,
        registry: DatasourceRegistry | None = None,
        runtime_bindings: RuntimeBindings | None = None,
        app_version: str = "0.1.0",
    ) -> None:
        self.data_dir = Path(data_dir)
        self.database_path = self.data_dir / "operational.sqlite3"
        self.backup_dir = Path(backup_dir) if backup_dir is not None else None
        self.registry = registry or default_registry()
        self.runtime_bindings = runtime_bindings or default_runtime_bindings()
        self.app_version = app_version
        self.artifacts = ArtifactStore(self.data_dir)
        self._writer_guard = threading.RLock()
        self._writer_depth = 0
        self._writer_descriptor: int | None = None

    @contextmanager
    def writer_session(self) -> Iterator[None]:
        """Hold the one local cross-process writer lease for this data directory.

        The session is re-entrant for nested store operations on one instance.
        A second process fails explicitly instead of racing a daemon's SQLite
        transaction or evidence publication.
        """

        with self._writer_guard:
            if self._writer_depth == 0:
                self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                descriptor = os.open(
                    self.data_dir / "writer.lock", os.O_RDWR | os.O_CREAT, 0o600
                )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    os.close(descriptor)
                    raise WriterAlreadyRunningError(
                        "datasource writer is already running"
                    ) from error
                self._writer_descriptor = descriptor
            self._writer_depth += 1
            try:
                yield
            finally:
                self._writer_depth -= 1
                if self._writer_depth == 0:
                    descriptor = self._writer_descriptor
                    self._writer_descriptor = None
                    assert descriptor is not None
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)

    def _is_initialized(self) -> bool:
        """Whether a migrated database exists without opening a write connection."""

        return self.database_path.is_file()

    @_single_writer
    def migrate(self) -> tuple[str, ...]:
        runner = MigrationRunner(self.database_path, app_version=self.app_version)
        if self._is_initialized():
            pending = runner.validate()
            if pending:
                if self.backup_dir is None:
                    raise OperationalError(
                        "pending migrations require a configured backup_dir"
                    )
                from nan_fung.backups import BackupError, create_pre_migration_backup

                try:
                    create_pre_migration_backup(self, self.backup_dir)
                except BackupError as error:
                    raise OperationalError(f"pre-migration backup failed: {error}") from error
        return tuple(
            migration.filename
            for migration in runner.migrate()
        )

    def host_throttle_gate(
        self, *, clock: Callable[[], datetime] | None = None
    ) -> "HostThrottleGate":
        """Return a durable request gate without introducing a sleep loop."""

        return HostThrottleGate(self, clock=clock)

    @_single_writer
    def permit_host(
        self,
        host: str,
        *,
        continuation: bool = False,
        now: datetime | None = None,
    ) -> None:
        """Permit one request or an already-reserved redirect continuation.

        A redirect chain is one logical acquisition under the daemon's single
        worker lease.  Its later hops still record their response and always
        honour a 429 ``blocked_until``, but do not compete with the first hop's
        one-second pacing reservation.
        """

        self.migrate()
        group = _rate_limit_group(host)
        anchor = _as_utc(now)
        timestamp = _timestamp(anchor)
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                row = connection.execute(
                    """
                    SELECT next_allowed_at, blocked_until
                    FROM host_throttle
                    WHERE rate_limit_group = ?
                    """,
                    (group,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO host_throttle (
                            rate_limit_group, next_allowed_at, blocked_until,
                            last_http_status, updated_at
                        ) VALUES (?, ?, NULL, NULL, ?)
                        """,
                        (group, _timestamp(anchor + _HOST_PERMIT_INTERVAL), timestamp),
                    )
                    return
                blocked_until = _optional_timestamp(row["blocked_until"])
                if blocked_until is not None and blocked_until > anchor:
                    raise HostThrottleBlocked(group, blocked_until)
                if continuation:
                    return
                next_allowed_at = _optional_timestamp(row["next_allowed_at"])
                if next_allowed_at is not None and next_allowed_at > anchor:
                    raise HostThrottleBlocked(group, next_allowed_at)
                connection.execute(
                    """
                    UPDATE host_throttle
                    SET next_allowed_at = ?, updated_at = ?
                    WHERE rate_limit_group = ?
                    """,
                    (
                        _timestamp(anchor + _HOST_PERMIT_INTERVAL),
                        timestamp,
                        group,
                    ),
                )
        finally:
            connection.close()

    @_single_writer
    def record_host_response(
        self,
        host: str,
        *,
        status: int,
        retry_after: str | None,
        now: datetime | None = None,
    ) -> HostThrottleState:
        """Persist response status and a 429 Retry-After block for one host."""

        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise OperationalError("HTTP status must be an integer from 100 to 599")
        self.migrate()
        group = _rate_limit_group(host)
        anchor = _as_utc(now)
        retry_until = _retry_after_deadline(retry_after, now=anchor) if status == 429 else None
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                row = connection.execute(
                    """
                    SELECT next_allowed_at, blocked_until
                    FROM host_throttle
                    WHERE rate_limit_group = ?
                    """,
                    (group,),
                ).fetchone()
                previous_next = (
                    _optional_timestamp(row["next_allowed_at"]) if row is not None else None
                )
                previous_block = (
                    _optional_timestamp(row["blocked_until"]) if row is not None else None
                )
                blocked_until = _latest_timestamp(
                    previous_block if previous_block is not None and previous_block > anchor else None,
                    retry_until,
                )
                next_allowed_at = _latest_timestamp(
                    previous_next if previous_next is not None and previous_next > anchor else anchor,
                    blocked_until,
                )
                assert next_allowed_at is not None
                timestamp = _timestamp(anchor)
                connection.execute(
                    """
                    INSERT INTO host_throttle (
                        rate_limit_group, next_allowed_at, blocked_until,
                        last_http_status, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(rate_limit_group) DO UPDATE SET
                        next_allowed_at = excluded.next_allowed_at,
                        blocked_until = excluded.blocked_until,
                        last_http_status = excluded.last_http_status,
                        updated_at = excluded.updated_at
                    """,
                    (
                        group,
                        _timestamp(next_allowed_at),
                        _timestamp(blocked_until) if blocked_until is not None else None,
                        status,
                        timestamp,
                    ),
                )
        finally:
            connection.close()
        return HostThrottleState(
            group,
            next_allowed_at,
            blocked_until,
            status,
            anchor,
        )

    @_single_writer
    def sync_registry(self, *, now: datetime | None = None) -> dict[str, int]:
        """Persist immutable registry snapshots and schedule rules idempotently."""

        self.migrate()
        timestamp = _timestamp(now)
        inserted_sources = 0
        inserted_definitions = 0
        inserted_schedules = 0
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                for source in self.registry.sources:
                    if self._insert_source(connection, source, timestamp):
                        inserted_sources += 1
                for definition in self.registry.definitions:
                    if self._insert_definition(connection, definition, timestamp):
                        inserted_definitions += 1
                    for binding in definition.source_bindings:
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO datasource_source (
                                datasource_id, definition_version, source_id,
                                source_version, role, required
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                definition.datasource_id,
                                definition.definition_version,
                                binding.source_id,
                                binding.source_version,
                                binding.role,
                                int(binding.required),
                            ),
                        )
                    for schedule in definition.schedules:
                        schedule_id = _schedule_id(definition, schedule.name)
                        schedule_json = schedule.as_json()
                        changed = connection.execute(
                            """
                            INSERT OR IGNORE INTO workflow_schedule (
                                schedule_id, task_kind, datasource_id, definition_version,
                                name, lane, rule_json, rule_hash, timezone, catchup_policy,
                                max_catchup_jobs, max_catchup_horizon_seconds, enabled,
                                created_at, updated_at
                            ) VALUES (?, 'ingest', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                schedule_id,
                                definition.datasource_id,
                                definition.definition_version,
                                schedule.name,
                                definition.default_lane,
                                _json(schedule_json["rule"]),
                                request_hash(schedule_json["rule"]),
                                schedule.timezone,
                                schedule.catchup_policy,
                                schedule.max_catchup_jobs,
                                schedule.max_catchup_horizon_seconds,
                                int(schedule.enabled),
                                timestamp,
                                timestamp,
                            ),
                        ).rowcount
                        inserted_schedules += changed
        finally:
            connection.close()
        return {
            "sources_inserted": inserted_sources,
            "definitions_inserted": inserted_definitions,
            "schedules_inserted": inserted_schedules,
        }

    def registry_status(self) -> tuple[dict[str, object], ...]:
        """Return persisted status, or the packaged read-only bootstrap view."""

        runtime = self.registry.runtime_status(self.runtime_bindings)
        catalog = datasource_workflow_catalog()

        def enrich(item: dict[str, object]) -> dict[str, object]:
            datasource_id = item["datasource_id"]
            definition_version = item["definition_version"]
            assert isinstance(datasource_id, str)
            assert isinstance(definition_version, int)
            validation = runtime[f"{datasource_id}@{definition_version}"]
            catalog_item = catalog.get(datasource_id)
            return {
                **item,
                "runtime_ready": validation.ready,
                "missing_runtime_bindings": [
                    f"{binding.kind}:{binding.name}@{binding.version}"
                    for binding in validation.missing
                ],
                "workflow_state": catalog_item.state if catalog_item else "custom_unbound",
                "legacy_adapter": catalog_item.legacy_adapter if catalog_item else None,
                "degraded_behavior": (
                    catalog_item.degraded_behavior
                    if catalog_item
                    else "custom_policy_required"
                ),
            }

        if not self._is_initialized():
            return tuple(
                enrich(
                    {
                        "datasource_id": definition.datasource_id,
                        "definition_version": definition.definition_version,
                        "definition_hash": definition.definition_hash,
                        "status": definition.status,
                        "default_lane": definition.default_lane,
                        "access_class": definition.access_class,
                        "promotion_policy": definition.promotion_policy,
                        "created_at": None,
                    }
                )
                for definition in sorted(
                    self.registry.definitions,
                    key=lambda item: (item.datasource_id, item.definition_version),
                )
            )
        connection = connect_database(self.database_path, read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT datasource_id, definition_version, definition_hash, status,
                       default_lane, access_class, promotion_policy, created_at
                FROM datasource_definition
                ORDER BY datasource_id, definition_version
                """
            ).fetchall()
            return tuple(enrich(dict(row)) for row in rows)
        finally:
            connection.close()

    def registry_diff(self) -> dict[str, object]:
        """Compare persisted immutable snapshots against the packaged registry.

        This is intentionally read-only: approving a changed definition means
        adding a new descriptor version in source control, never mutating a
        previously persisted semantic contract.
        """

        if not self._is_initialized():
            rows: tuple[object, ...] = ()
        else:
            connection = connect_database(self.database_path, read_only=True)
            try:
                rows = tuple(
                    connection.execute(
                        "SELECT datasource_id, definition_version, definition_hash FROM datasource_definition"
                    ).fetchall()
                )
            finally:
                connection.close()
        persisted = {
            (row["datasource_id"], row["definition_version"]): row["definition_hash"]
            for row in rows
        }
        packaged = {
            (definition.datasource_id, definition.definition_version): definition.definition_hash
            for definition in self.registry.definitions
        }
        return {
            "schema_version": "registry_diff.v1",
            "missing_in_store": [
                {"datasource_id": item[0], "definition_version": item[1]}
                for item in sorted(packaged.keys() - persisted.keys())
            ],
            "unexpected_in_store": [
                {"datasource_id": item[0], "definition_version": item[1]}
                for item in sorted(persisted.keys() - packaged.keys())
            ],
            "hash_mismatches": [
                {"datasource_id": item[0], "definition_version": item[1]}
                for item in sorted(packaged.keys() & persisted.keys())
                if packaged[item] != persisted[item]
            ],
        }

    @_single_writer
    def enqueue(
        self,
        datasource_id: str,
        *,
        definition_version: int | None = None,
        request: Mapping[str, Any] | None = None,
        trigger: str = "manual",
        lane: str | None = None,
        priority: int = 100,
        scheduled_for: datetime | None = None,
        request_instance_id: str | None = None,
        schedule_id: str | None = None,
        parent_job_id: str | None = None,
        generation: int = 0,
        job_kind: str | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> EnqueueResult:
        """Create or return a durable, policy-selected job without executing it."""

        self.sync_registry()
        try:
            definition = self.registry.lookup(datasource_id, definition_version)
        except RegistryError as error:
            raise OperationalError(str(error)) from error
        effective_lane = lane or definition.default_lane
        if effective_lane not in {
            "production_ingestion",
            "source_discovery",
            "ad_hoc_research",
        }:
            raise OperationalError("unsupported ingestion lane")
        if effective_lane == "production_ingestion" and definition.status != "production":
            raise OperationalError("unapproved datasource cannot be enqueued for production")
        if trigger not in {
            "schedule",
            "agent_request",
            "manual",
            "backfill",
            "fanout",
            "reparse",
            "promotion",
            "recovery",
        }:
            raise OperationalError("unsupported job trigger")
        # Requests are audit metadata as well as worker input.  Never retain a
        # credential in either the persisted request or its dedupe hash.
        request_payload = redact_secrets(
            dict(request or _thaw(definition.default_request))
        )
        effective_job_kind = job_kind or (
            "backfill"
            if trigger == "backfill"
            else "on_demand_refresh"
            if trigger == "agent_request"
            else "scheduled_ingest"
        )
        if effective_job_kind not in {
            "scheduled_ingest",
            "on_demand_refresh",
            "backfill",
            "offline_reparse",
            "manual_submission",
        }:
            raise OperationalError("unsupported datasource job kind")
        if (window_start is None) != (window_end is None):
            raise OperationalError("job window_start and window_end must be supplied together")
        window_start_at = _timestamp(window_start) if window_start else None
        window_end_at = _timestamp(window_end) if window_end else None
        if (
            window_start_at is not None
            and window_end_at is not None
            and window_start_at > window_end_at
        ):
            raise OperationalError("job window_start must be before window_end")
        if effective_job_kind == "backfill" and window_start_at is None:
            raise OperationalError("backfill jobs require a bounded window")
        if effective_job_kind != "backfill" and window_start_at is not None:
            raise OperationalError("only backfill jobs support durable windows")
        if effective_job_kind == "backfill":
            request_start = request_payload.get("window_start")
            request_end = request_payload.get("window_end")
            if request_start != window_start_at or request_end != window_end_at:
                raise OperationalError("request window must match the durable job window")
        now = _timestamp(scheduled_for)
        payload_hash = request_hash(request_payload)
        def dedupe_key(instance_id: str | None) -> str:
            return request_hash({
                "datasource_id": datasource_id,
                "definition_version": definition.definition_version,
                "definition_hash": definition.definition_hash,
                "lane": effective_lane,
                "trigger": trigger,
                "request_hash": payload_hash,
                "scheduled_for": now if trigger == "schedule" else None,
                "schedule_id": schedule_id,
                "parent_job_id": parent_job_id,
                "generation": generation,
                "request_instance_id": instance_id,
                "job_kind": effective_job_kind,
                "window_start": window_start_at,
                "window_end": window_end_at,
            })

        dedupe = dedupe_key(request_instance_id)
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                existing = connection.execute(
                    "SELECT job_id, state FROM workflow_job WHERE dedupe_key = ?", (dedupe,)
                ).fetchone()
                if existing is not None:
                    # Default manual requests dedupe while one is active, but
                    # a later operator retry must not resolve forever to a
                    # terminal historical job.  An explicit request id keeps
                    # its normal idempotent semantics even after completion.
                    if not (
                        trigger == "manual"
                        and request_instance_id is None
                        and existing["state"]
                        in {"succeeded", "empty", "failed", "dead_letter", "cancelled"}
                    ):
                        return EnqueueResult(existing["job_id"], "deduplicated", existing["state"])
                    request_instance_id = new_id("manual_request")
                    dedupe = dedupe_key(request_instance_id)
                job_id = new_id("job")
                changed = connection.execute(
                    """
                    INSERT INTO workflow_job (
                        job_id, dedupe_key, job_kind, datasource_id,
                        definition_version, definition_hash, lane, trigger,
                        schedule_id, parent_job_id, generation, scheduled_for, available_at,
                        window_start, window_end, priority, request_json,
                        request_hash, state, max_attempts, request_instance_id,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                    """,
                    (
                        job_id,
                        dedupe,
                        effective_job_kind,
                        datasource_id,
                        definition.definition_version,
                        definition.definition_hash,
                        effective_lane,
                        trigger,
                        schedule_id,
                        parent_job_id,
                        generation,
                        now,
                        now,
                        window_start_at,
                        window_end_at,
                        priority,
                        _json(request_payload),
                        payload_hash,
                        _max_attempts(definition),
                        request_instance_id,
                        now,
                    ),
                )
                self._audit(
                    connection,
                    actor_type="operator" if trigger != "agent_request" else "agent",
                    actor_id="local",
                    action="job_enqueued",
                    target_type="workflow_job",
                    target_id=job_id,
                    details={"datasource_id": datasource_id, "lane": effective_lane},
                    at=now,
                )
                return EnqueueResult(job_id, "accepted", "queued")
        finally:
            connection.close()

    @_single_writer
    def enqueue_backfill(
        self,
        datasource_id: str,
        *,
        window_start: datetime,
        window_end: datetime,
        lane: str | None = None,
    ) -> EnqueueResult:
        """Enqueue an auditable, bounded historical window without executing it."""

        start = _as_utc(window_start)
        end = _as_utc(window_end)
        if start > end:
            raise OperationalError("backfill window_start must be before window_end")
        return self.enqueue(
            datasource_id,
            request={
                "window_start": _timestamp(start),
                "window_end": _timestamp(end),
            },
            trigger="backfill",
            lane=lane,
            scheduled_for=start,
            request_instance_id=new_id("backfill"),
            job_kind="backfill",
            window_start=start,
            window_end=end,
        )

    @_single_writer
    def enqueue_projection_delivery(
        self,
        output_directory: str | Path,
        *,
        as_of_at: datetime,
        scheduled_for: datetime | None = None,
        trigger: str = "manual",
    ) -> EnqueueResult:
        """Queue one bounded canonical projection publication.

        This system job intentionally has no datasource ID or ingestion run.
        Its request contains only an operator-selected output root and a fixed
        as-of anchor; it cannot acquire data, alter promotion, or select SQL.
        """

        if trigger not in {"manual", "schedule", "recovery"}:
            raise OperationalError("unsupported projection delivery trigger")
        self.migrate()
        anchor = _as_utc(as_of_at)
        queued_for = _as_utc(scheduled_for or anchor)
        root = Path(output_directory).expanduser().resolve()
        request = {
            "output_directory": str(root),
            "as_of_at": _timestamp(anchor),
        }
        payload_hash = request_hash(request)
        dedupe = request_hash(
            {
                "job_kind": "projection_delivery",
                "trigger": trigger,
                "request_hash": payload_hash,
                "scheduled_for": _timestamp(queued_for) if trigger == "schedule" else None,
            }
        )
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                existing = connection.execute(
                    "SELECT job_id, state FROM workflow_job WHERE dedupe_key = ?", (dedupe,)
                ).fetchone()
                if existing is not None:
                    return EnqueueResult(existing["job_id"], "deduplicated", existing["state"])
                job_id = new_id("job")
                timestamp = _timestamp(queued_for)
                connection.execute(
                    """
                    INSERT INTO workflow_job (
                        job_id, dedupe_key, job_kind, datasource_id,
                        definition_version, definition_hash, lane, trigger,
                        schedule_id, parent_job_id, generation, scheduled_for, available_at,
                        window_start, window_end, priority, request_json,
                        request_hash, state, max_attempts, request_instance_id,
                        created_at
                    ) VALUES (?, ?, 'projection_delivery', NULL, NULL, NULL, NULL, ?,
                              NULL, NULL, 0, ?, ?, NULL, NULL, 50, ?, ?, 'queued', 3, NULL, ?)
                    """,
                    (
                        job_id,
                        dedupe,
                        trigger,
                        timestamp,
                        timestamp,
                        _json(request),
                        payload_hash,
                        timestamp,
                    ),
                )
                self._audit(
                    connection,
                    actor_type="operator",
                    actor_id="local",
                    action="projection_delivery_enqueued",
                    target_type="workflow_job",
                    target_id=job_id,
                    details=request,
                    at=timestamp,
                )
                return EnqueueResult(job_id, "accepted", "queued")
        finally:
            connection.close()

    @_single_writer
    def claim_next(
        self, worker_id: str, *, now: datetime | None = None, lease_seconds: int = 180
    ) -> ClaimedJob | None:
        """Atomically lease one due job; stale workers cannot later commit it."""

        anchor = _as_utc(now)
        self.recover_expired(now=anchor)
        timestamp = _timestamp(anchor)
        expires = _timestamp(anchor + timedelta(seconds=_lease_seconds(lease_seconds)))
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                row = connection.execute(
                    """
                    SELECT job_id, datasource_id, definition_version, lane, job_kind,
                           window_start, window_end, request_json
                    FROM workflow_job
                    WHERE state IN ('queued', 'retry_wait') AND available_at <= ?
                    ORDER BY priority ASC, available_at ASC, scheduled_for ASC, job_id ASC
                    LIMIT 1
                    """,
                    (timestamp,),
                ).fetchone()
                if row is None:
                    return None
                return self._claim_row(connection, row, worker_id, timestamp, expires)
        finally:
            connection.close()

    @_single_writer
    def claim_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = 180,
    ) -> ClaimedJob | None:
        """Lease one known job without allowing a worker to claim another job."""

        anchor = _as_utc(now)
        self.recover_expired(now=anchor)
        timestamp = _timestamp(anchor)
        expires = _timestamp(anchor + timedelta(seconds=_lease_seconds(lease_seconds)))
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                row = connection.execute(
                    """
                    SELECT job_id, datasource_id, definition_version, lane, job_kind,
                           window_start, window_end, request_json
                    FROM workflow_job
                    WHERE job_id = ? AND state IN ('queued', 'retry_wait')
                      AND available_at <= ?
                    """,
                    (job_id, timestamp),
                ).fetchone()
                if row is None:
                    return None
                return self._claim_row(connection, row, worker_id, timestamp, expires)
        finally:
            connection.close()

    @staticmethod
    def _claim_row(
        connection: object,
        row: object,
        worker_id: str,
        timestamp: str,
        expires: str,
    ) -> ClaimedJob | None:
        if not worker_id:
            raise OperationalError("worker_id is required")
        token = new_id("claim")
        changed = connection.execute(  # type: ignore[attr-defined]
            """
            UPDATE workflow_job
            SET state = 'claimed', claim_token = ?, claimed_by = ?,
                claimed_at = ?, lease_expires_at = ?, heartbeat_at = ?
            WHERE job_id = ? AND state IN ('queued', 'retry_wait')
            """,
            (token, worker_id, timestamp, expires, timestamp, row["job_id"]),
        ).rowcount
        if changed != 1:
            return None
        return ClaimedJob(
            job_id=row["job_id"],
            claim_token=token,
            datasource_id=row["datasource_id"],
            definition_version=row["definition_version"],
            lane=row["lane"],
            job_kind=row["job_kind"],
            window_start=_optional_timestamp(row["window_start"]),
            window_end=_optional_timestamp(row["window_end"]),
            request=json.loads(row["request_json"]),
        )

    @_single_writer
    def start_run(
        self, claim: ClaimedJob, worker_id: str, *, now: datetime | None = None
    ) -> RunHandle:
        """Turn a lease into an attempt and one-to-one ingestion run."""

        if claim.datasource_id is None or claim.definition_version is None or claim.lane is None:
            raise OperationalError("system jobs do not create ingestion runs")
        now = _timestamp(now)
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                row = connection.execute(
                    """
                    SELECT attempt_count, max_attempts, definition_hash, trigger
                    FROM workflow_job
                    WHERE job_id = ? AND claim_token = ? AND state = 'claimed'
                      AND lease_expires_at > ?
                    """,
                    (claim.job_id, claim.claim_token, now),
                ).fetchone()
                if row is None:
                    raise OperationalError("job lease is no longer valid")
                attempt_no = row["attempt_count"] + 1
                if attempt_no > row["max_attempts"]:
                    raise OperationalError("job has exhausted its attempt budget")
                attempt_id = new_id("attempt")
                run_id = new_id("run")
                changed = connection.execute(
                    """
                    UPDATE workflow_job
                    SET state = 'running', attempt_count = ?, started_at = COALESCE(started_at, ?),
                        heartbeat_at = ?
                    WHERE job_id = ? AND claim_token = ? AND state = 'claimed'
                      AND lease_expires_at > ?
                    """,
                    (attempt_no, now, now, claim.job_id, claim.claim_token, now),
                ).rowcount
                if changed != 1:
                    raise OperationalError("job lease expired before run start")
                connection.execute(
                    """
                    INSERT INTO workflow_attempt (
                        attempt_id, job_id, attempt_no, status, worker_id,
                        warnings_json, started_at, heartbeat_at
                    ) VALUES (?, ?, ?, 'running', ?, '[]', ?, ?)
                    """,
                    (attempt_id, claim.job_id, attempt_no, worker_id, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO ingestion_run (
                        run_id, attempt_id, job_id, datasource_id, definition_version,
                        definition_hash, lane, trigger, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
                    """,
                    (
                        run_id,
                        attempt_id,
                        claim.job_id,
                        claim.datasource_id,
                        claim.definition_version,
                        row["definition_hash"],
                        claim.lane,
                        row["trigger"],
                        now,
                    ),
                )
                return RunHandle(
                    run_id,
                    attempt_id,
                    claim.job_id,
                    claim.claim_token,
                    claim.datasource_id,
                    claim.definition_version,
                    claim.lane,
                )
        finally:
            connection.close()

    @_single_writer
    def start_system_job(
        self, claim: ClaimedJob, worker_id: str, *, now: datetime | None = None
    ) -> SystemJobHandle:
        """Start a claimed system task without creating an ingestion run."""

        if claim.datasource_id is not None or claim.job_kind != "projection_delivery":
            raise OperationalError("unsupported system job")
        if not worker_id:
            raise OperationalError("worker_id is required")
        current_at = _timestamp(now)
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                row = connection.execute(
                    """
                    SELECT attempt_count, max_attempts
                    FROM workflow_job
                    WHERE job_id = ? AND claim_token = ? AND state = 'claimed'
                      AND lease_expires_at > ?
                    """,
                    (claim.job_id, claim.claim_token, current_at),
                ).fetchone()
                if row is None:
                    raise OperationalError("system job lease is no longer valid")
                attempt_no = row["attempt_count"] + 1
                if attempt_no > row["max_attempts"]:
                    raise OperationalError("system job exhausted its attempt budget")
                attempt_id = new_id("attempt")
                changed = connection.execute(
                    """
                    UPDATE workflow_job
                    SET state = 'running', attempt_count = ?, started_at = COALESCE(started_at, ?),
                        heartbeat_at = ?
                    WHERE job_id = ? AND claim_token = ? AND state = 'claimed'
                      AND lease_expires_at > ?
                    """,
                    (
                        attempt_no,
                        current_at,
                        current_at,
                        claim.job_id,
                        claim.claim_token,
                        current_at,
                    ),
                ).rowcount
                if changed != 1:
                    raise OperationalError("system job lease expired before start")
                connection.execute(
                    """
                    INSERT INTO workflow_attempt (
                        attempt_id, job_id, attempt_no, status, worker_id,
                        warnings_json, started_at, heartbeat_at
                    ) VALUES (?, ?, ?, 'running', ?, '[]', ?, ?)
                    """,
                    (attempt_id, claim.job_id, attempt_no, worker_id, current_at, current_at),
                )
                return SystemJobHandle(attempt_id, claim.job_id, claim.claim_token, claim.job_kind)
        finally:
            connection.close()

    @_single_writer
    def finish_system_job(
        self,
        handle: SystemJobHandle,
        *,
        status: str,
        error: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        """Close a system-task attempt with the same lease CAS as ingestion."""

        if status not in {"succeeded", "failed", "cancelled"}:
            raise OperationalError("invalid system job terminal status")
        current_at = _timestamp(now)
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                job = connection.execute(
                    """
                    SELECT attempt_count, max_attempts FROM workflow_job
                    WHERE job_id = ? AND claim_token = ? AND state = 'running'
                      AND lease_expires_at > ?
                    """,
                    (handle.job_id, handle.claim_token, current_at),
                ).fetchone()
                if job is None:
                    raise OperationalError("system job is not owned by an active claim")
                if status == "succeeded":
                    job_state = "succeeded"
                elif status == "cancelled":
                    job_state = "cancelled"
                elif job["attempt_count"] < job["max_attempts"]:
                    job_state = "retry_wait"
                else:
                    job_state = "dead_letter"
                available_at = _retry_timestamp(current_at, "{}", job["attempt_count"])
                connection.execute(
                    """
                    UPDATE workflow_attempt
                    SET status = ?, completed_at = ?, heartbeat_at = ?, error_json = ?
                    WHERE attempt_id = ? AND status = 'running'
                    """,
                    (status, current_at, current_at, _json(error) if error else None, handle.attempt_id),
                )
                connection.execute(
                    """
                    UPDATE workflow_job
                    SET state = ?, completed_at = CASE WHEN ? IN ('succeeded', 'cancelled', 'dead_letter') THEN ? ELSE NULL END,
                        available_at = CASE WHEN ? = 'retry_wait' THEN ? ELSE available_at END,
                        claim_token = NULL, claimed_by = NULL, claimed_at = NULL,
                        lease_expires_at = NULL, heartbeat_at = ?, last_error_json = ?
                    WHERE job_id = ? AND claim_token = ? AND state = 'running'
                    """,
                    (
                        job_state,
                        job_state,
                        current_at,
                        job_state,
                        available_at,
                        current_at,
                        _json(error) if error else None,
                        handle.job_id,
                        handle.claim_token,
                    ),
                )
        finally:
            connection.close()

    @_single_writer
    def heartbeat(
        self,
        run: RunHandle,
        *,
        now: datetime | None = None,
        lease_seconds: int = 180,
    ) -> None:
        """Extend a running lease with the run's opaque compare-and-set token."""

        anchor = _as_utc(now)
        timestamp = _timestamp(anchor)
        expires = _timestamp(anchor + timedelta(seconds=_lease_seconds(lease_seconds)))
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                changed = connection.execute(
                    """
                    UPDATE workflow_job
                    SET heartbeat_at = ?, lease_expires_at = ?
                    WHERE job_id = ? AND claim_token = ? AND state = 'running'
                      AND lease_expires_at > ?
                    """,
                    (timestamp, expires, run.job_id, run.claim_token, timestamp),
                ).rowcount
                if changed != 1:
                    raise OperationalError("run is not owned by an active claim")
                connection.execute(
                    """
                    UPDATE workflow_attempt SET heartbeat_at = ?
                    WHERE attempt_id = ? AND status = 'running'
                    """,
                    (timestamp, run.attempt_id),
                )
        finally:
            connection.close()

    @_single_writer
    def preflight_evidence(
        self,
        run: RunHandle,
        *,
        request: Mapping[str, Any] | None = None,
        response: Mapping[str, Any] | None = None,
        source_id: str | None = None,
        retrieved_at: datetime | None = None,
        retention_until: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        """Reject deterministic evidence failures before a stream publishes CAS.

        The persisted path repeats this immediately before creating immutable
        database references.  This preflight exists for live streaming, where
        the body has not yet been atomically published.
        """

        _definition, _source_binding, current_at, _retrieved, _retention = (
            self._prepare_evidence_metadata(
                run,
                request=request,
                response=response,
                source_id=source_id,
                retrieved_at=retrieved_at,
                retention_until=retention_until,
                now=now,
            )
        )
        connection = connect_database(self.database_path, read_only=True)
        try:
            self._require_running_run(connection, run, at=current_at)
        finally:
            connection.close()

    @_single_writer
    def persist_evidence(
        self,
        run: RunHandle,
        body: bytes | None = None,
        *,
        artifact: StoredArtifact | None = None,
        role: str = "primary",
        media_type: str | None = None,
        request: Mapping[str, Any] | None = None,
        response: Mapping[str, Any] | None = None,
        source_id: str | None = None,
        required: bool = True,
        retrieved_at: datetime | None = None,
        retention_until: datetime | None = None,
        now: datetime | None = None,
    ) -> PersistedEvidence:
        """Save bytes first, then record the immutable evidence metadata."""

        if (body is None) == (artifact is None):
            raise OperationalError("provide exactly one evidence body or stored artifact")
        definition, source_binding, current_at, retrieved, retention = (
            self._prepare_evidence_metadata(
                run,
                request=request,
                response=response,
                source_id=source_id,
                retrieved_at=retrieved_at,
                retention_until=retention_until,
                now=now,
            )
        )
        effective_media_type = media_type
        if artifact is not None:
            if (
                media_type is not None
                and artifact.media_type is not None
                and media_type != artifact.media_type
            ):
                raise OperationalError("stored artifact media type does not match evidence metadata")
            effective_media_type = artifact.media_type or media_type
            if not self.artifacts.verify(artifact):
                raise OperationalError("stored artifact failed verification")
            try:
                validate_artifact_file(
                    self.artifacts.object_path(artifact.content_sha256),
                    byte_size=artifact.byte_size,
                    media_type=effective_media_type,
                    policy=_artifact_policy_for(definition),
                )
            except PolicyError as error:
                raise OperationalError(str(error)) from error
        else:
            assert body is not None
            try:
                validate_artifact_bytes(
                    body,
                    media_type=effective_media_type,
                    policy=_artifact_policy_for(definition),
                )
            except PolicyError as error:
                raise OperationalError(str(error)) from error
        # Fail known-invalid runs before publishing an otherwise unreferenced
        # CAS object.  The transaction below repeats this compare-and-set
        # check immediately before its immutable DB reference is inserted.
        connection = connect_database(self.database_path, read_only=True)
        try:
            self._require_running_run(connection, run, at=current_at)
        finally:
            connection.close()
        if artifact is None:
            assert body is not None
            artifact = self.artifacts.put_bytes(body, media_type=effective_media_type)
        evidence_id = new_id("ev")
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                self._require_running_run(connection, run, at=current_at)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO content_object (
                        content_sha256, byte_size, artifact_uri, created_at, verified_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.content_sha256,
                        artifact.byte_size,
                        artifact.artifact_uri,
                        current_at,
                        current_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO evidence_artifact (
                        evidence_id, content_sha256, media_type, access_class,
                        retention_until, retrieved_at, request_json, response_json, source_id,
                        source_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence_id,
                        artifact.content_sha256,
                        effective_media_type,
                        definition.access_class,
                        _timestamp(retention) if retention is not None else None,
                        retrieved,
                        _json(redact_secrets(request or {})),
                        _json(redact_secrets(response or {})),
                        source_binding.source_id,
                        source_binding.source_version,
                        current_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO run_evidence (run_id, evidence_id, role, required)
                    VALUES (?, ?, ?, ?)
                    """,
                    (run.run_id, evidence_id, role, int(required)),
                )
        finally:
            connection.close()
        return PersistedEvidence(evidence_id, artifact)

    def _prepare_evidence_metadata(
        self,
        run: RunHandle,
        *,
        request: Mapping[str, Any] | None,
        response: Mapping[str, Any] | None,
        source_id: str | None,
        retrieved_at: datetime | None,
        retention_until: datetime | None,
        now: datetime | None,
    ) -> tuple[DatasourceDefinitionDescriptor, SourceBinding, str, str, datetime | None]:
        """Validate immutable evidence metadata before bytes are published."""

        definition = self.registry.lookup(run.datasource_id, run.definition_version)
        matching_bindings = tuple(
            binding
            for binding in definition.source_bindings
            if binding.source_id == (source_id or definition.source_bindings[0].source_id)
        )
        if len(matching_bindings) != 1:
            raise OperationalError("evidence source must be exactly one bound definition source")
        source_binding = matching_bindings[0]
        self._validate_evidence_provenance(
            source_binding,
            request=request,
            response=response,
        )
        current_at = _timestamp(now)
        retrieved = _timestamp(retrieved_at) if retrieved_at is not None else current_at
        retention = _retention_deadline(
            definition.retention_policy,
            retrieved_at=_parse_timestamp(retrieved),
            requested_until=retention_until,
        )
        return definition, source_binding, current_at, retrieved, retention

    def _validate_evidence_provenance(
        self,
        source_binding: SourceBinding,
        *,
        request: Mapping[str, Any] | None,
        response: Mapping[str, Any] | None,
    ) -> None:
        """Reject forged acquisition metadata before bytes enter the CAS.

        The acquisition boundary validates public addresses before transport.
        This persistence boundary validates the original request and final URL
        again against the immutable source binding, without resolving DNS, so
        an arbitrary caller cannot relabel an external artifact as canonical
        evidence.  Manual imports are deliberately distinct: they may omit a
        URL, but a supplied URL must still match the bound source.
        """

        request_data = dict(request or {})
        response_data = dict(response or {})
        method = request_data.get("method")
        source = self.registry.lookup_source(
            source_binding.source_id, source_binding.source_version
        )

        if method == "MANUAL_IMPORT":
            source_url = request_data.get("source_url")
            if source_url is None:
                return
            if not isinstance(source_url, str):
                raise OperationalError("manual evidence source URL must be a string")
            self._validate_bound_source_url(source.allowed_hosts, source_url, "GET")
            return

        if not isinstance(method, str) or method.upper() != "GET":
            raise OperationalError("evidence acquisition method must be GET")
        request_url = request_data.get("url")
        final_url = response_data.get("final_url")
        status = response_data.get("status")
        if not isinstance(request_url, str) or not isinstance(final_url, str):
            raise OperationalError("evidence requires request and final response URLs")
        if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status < 300:
            raise OperationalError("canonical evidence requires a successful HTTP response")
        self._validate_bound_source_url(source.allowed_hosts, request_url, method)
        self._validate_bound_source_url(source.allowed_hosts, final_url, method)

    @staticmethod
    def _validate_bound_source_url(
        allowed_hosts: tuple[str, ...], url: str, method: str
    ) -> None:
        if not allowed_hosts:
            raise OperationalError("bound source has no approved network host")
        try:
            validate_source_url(
                url,
                SourcePolicy(allowed_hosts=allowed_hosts, allowed_methods=(method,)),
                method=method,
                resolver=None,
            )
        except PolicyError as error:
            raise OperationalError("evidence has unapproved source provenance") from error

    def read_evidence(self, evidence: PersistedEvidence | str | object) -> bytes:
        """Return verified bytes for a known evidence handle, never an input path."""

        evidence_id = (
            evidence
            if isinstance(evidence, str)
            else getattr(evidence, "evidence_id", None)
        )
        if not isinstance(evidence_id, str) or not evidence_id:
            raise OperationalError("evidence handle is required")
        connection = connect_database(self.database_path, read_only=True)
        try:
            row = connection.execute(
                "SELECT content_sha256 FROM evidence_artifact WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise OperationalError("unknown evidence handle")
        with self.artifacts.open(row["content_sha256"]) as artifact:
            return artifact.read()

    @_single_writer
    def import_manual_evidence(
        self,
        datasource_id: str,
        body: bytes,
        *,
        media_type: str | None,
        source_url: str | None = None,
        attestation: str | None = None,
        retention_until: datetime | None = None,
        actor_id: str = "operator",
    ) -> ManualEvidenceResult:
        """Store operator-supplied evidence and open review when policy requires it.

        The caller supplies bytes already opened with a no-follow regular-file
        check.  This method accepts no source path, so a daemon adapter never
        receives authority to read arbitrary local files.
        """

        is_submarket_mapping = datasource_id == SUBMARKET_MAPPING_DATASOURCE_ID
        if is_submarket_mapping:
            try:
                attestation = validate_submarket_mapping_submission(
                    media_type=media_type, attestation=attestation
                )
            except SubmarketMappingError as error:
                raise OperationalError(str(error)) from error
        definition = self.registry.lookup(datasource_id)
        queued = self.enqueue(
            datasource_id,
            request={"manual_import": True, "source_url": source_url, "attestation": attestation},
            trigger="manual",
            lane=definition.default_lane,
            request_instance_id=new_id("manual"),
            job_kind="manual_submission",
        )
        claim = self.claim_job(queued.job_id, actor_id)
        if claim is None:
            raise OperationalError("manual evidence job could not be claimed")
        run = self.start_run(claim, actor_id)
        try:
            evidence = self.persist_evidence(
                run,
                body,
                media_type=media_type,
                request={"method": "MANUAL_IMPORT", "source_url": source_url},
                response={"attestation": attestation},
                retention_until=retention_until,
            )
        except Exception as error:
            try:
                self.finish_run(
                    run,
                    status="failed",
                    error={
                        "schema_version": "error.v1",
                        "code": "MANUAL_EVIDENCE_REJECTED",
                        "stage": "evidence",
                        "retryable": False,
                        "details": {"exception": type(error).__name__},
                    },
                )
            except OperationalError:
                # A lease recovered during the failed capture owns its terminal
                # state; never revive it only to record a second failure.
                pass
            raise
        if is_submarket_mapping:
            try:
                persist_submarket_mapping_observation(self, run, evidence)
            except SubmarketMappingError as error:
                code = mapping_import_error_code(error)
                self.finish_run(
                    run,
                    status="failed",
                    error={"code": code, "retryable": False},
                )
                raise OperationalError(code) from error
        review_id: str | None = None
        if definition.promotion_policy == "manual_review":
            review_id = new_id("review")
            now = _timestamp()
            connection = connect_database(self.database_path)
            try:
                with transaction(connection):
                    self._require_running_run(connection, run, at=now)
                    connection.execute(
                        """
                        INSERT INTO review_task (
                            review_id, datasource_id, definition_version, run_id,
                            task_type, state, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, 'manual_evidence_review', 'open', ?, ?)
                        """,
                        (
                            review_id,
                            definition.datasource_id,
                            definition.definition_version,
                            run.run_id,
                            _json({"evidence_id": evidence.evidence_id, "attestation": attestation}),
                            now,
                        ),
                    )
            finally:
                connection.close()
        self.finish_run(run, status="succeeded")
        return ManualEvidenceResult(run.run_id, evidence.evidence_id, review_id, "succeeded")

    def review_tasks(self, *, state: str | None = None) -> tuple[dict[str, object], ...]:
        """Return bounded review metadata; raw report bytes remain in the CAS."""

        if state is not None and state not in {"open", "approved", "rejected", "cancelled"}:
            raise OperationalError("invalid review state")
        if not self._is_initialized():
            return ()
        connection = connect_database(self.database_path, read_only=True)
        try:
            sql = (
                "SELECT review_id, datasource_id, definition_version, run_id, task_type, "
                "state, payload_json, created_at, completed_at FROM review_task"
            )
            parameters: tuple[object, ...] = ()
            if state is not None:
                sql += " WHERE state = ?"
                parameters = (state,)
            sql += " ORDER BY created_at, review_id"
            rows = connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()
        return tuple(
            {
                **{key: value for key, value in dict(row).items() if key != "payload_json"},
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        )

    @_single_writer
    def decide_review(
        self,
        review_id: str,
        *,
        decision: str,
        actor_id: str,
        reason: str | None = None,
    ) -> bool:
        """Record a human review decision without auto-promoting report facts."""

        if decision not in {"approved", "rejected"}:
            raise OperationalError("review decision must be approved or rejected")
        if not actor_id:
            raise OperationalError("review actor is required")
        now = _timestamp()
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                review = connection.execute(
                    "SELECT state FROM review_task WHERE review_id = ?", (review_id,)
                ).fetchone()
                if review is None:
                    raise OperationalError("unknown review task")
                if review["state"] != "open":
                    return False
                changed = connection.execute(
                    """
                    UPDATE review_task SET state = ?, completed_at = ?
                    WHERE review_id = ? AND state = 'open'
                    """,
                    (decision, now, review_id),
                ).rowcount
                if changed:
                    connection.execute(
                        """
                        INSERT INTO review_decision (
                            decision_id, review_id, decision, actor_id, reason, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (new_id("review_decision"), review_id, decision, actor_id, reason, now),
                    )
                    self._audit(
                        connection,
                        actor_type="operator",
                        actor_id=actor_id,
                        action="review_decided",
                        target_type="review_task",
                        target_id=review_id,
                        details={"decision": decision},
                        at=now,
                    )
                return bool(changed)
        finally:
            connection.close()

    @_single_writer
    def promote_review(
        self,
        review_id: str,
        *,
        actor_id: str,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> ManualPromotionResult:
        """Explicitly promote the successful production run behind an approved review.

        A review decision intentionally does not change canonical state on its
        own.  This method is the separately auditable operator action and is
        idempotent only for the same review-to-promotion link.
        """

        if not review_id:
            raise OperationalError("review ID is required")
        if not actor_id:
            raise OperationalError("promotion actor is required")
        self.migrate()
        current_at = _timestamp(now)
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                existing = connection.execute(
                    """
                    SELECT review_id, run_id, promotion_id
                    FROM manual_review_promotion WHERE review_id = ?
                    """,
                    (review_id,),
                ).fetchone()
                if existing is not None:
                    return ManualPromotionResult(
                        existing["review_id"],
                        existing["run_id"],
                        existing["promotion_id"],
                        False,
                    )

                review = connection.execute(
                    """
                    SELECT r.review_id, r.datasource_id, r.definition_version,
                           r.run_id, r.state, d.status AS definition_status,
                           d.promotion_policy, run.status AS run_status,
                           run.lane AS run_lane, attempt.status AS attempt_status
                    FROM review_task AS r
                    JOIN datasource_definition AS d
                      ON d.datasource_id = r.datasource_id
                     AND d.definition_version = r.definition_version
                    LEFT JOIN ingestion_run AS run ON run.run_id = r.run_id
                    LEFT JOIN workflow_attempt AS attempt ON attempt.attempt_id = run.attempt_id
                    WHERE r.review_id = ?
                    """,
                    (review_id,),
                ).fetchone()
                if review is None:
                    raise OperationalError("unknown review task")
                if review["state"] != "approved":
                    raise OperationalError("review must be approved before promotion")
                if review["promotion_policy"] != "manual_review":
                    raise OperationalError("review datasource does not permit manual promotion")
                if review["definition_status"] != "production":
                    raise OperationalError("manual promotion requires a production datasource")
                if not review["run_id"]:
                    raise OperationalError("review is not attached to an ingestion run")
                if (
                    review["run_lane"] != "production_ingestion"
                    or review["run_status"] != "succeeded"
                    or review["attempt_status"] != "succeeded"
                ):
                    raise OperationalError(
                        "manual promotion requires a successful production ingestion run"
                    )
                decision = connection.execute(
                    """
                    SELECT decision_id FROM review_decision
                    WHERE review_id = ? AND decision = 'approved'
                    ORDER BY created_at DESC, decision_id DESC LIMIT 1
                    """,
                    (review_id,),
                ).fetchone()
                if decision is None:
                    raise OperationalError("approved review has no approval decision")
                prior_run_link = connection.execute(
                    """
                    SELECT review_id FROM manual_review_promotion WHERE run_id = ?
                    """,
                    (review["run_id"],),
                ).fetchone()
                if prior_run_link is not None:
                    raise OperationalError("run was already manually promoted by another review")
                prior_approval = connection.execute(
                    """
                    SELECT promotion_id FROM run_promotion
                    WHERE run_id = ? AND decision = 'approved'
                    LIMIT 1
                    """,
                    (review["run_id"],),
                ).fetchone()
                if prior_approval is not None:
                    raise OperationalError("run already has an approved promotion")

                promotion_id = new_id("promotion")
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(promotion_seq), 0) + 1 FROM run_promotion"
                ).fetchone()[0]
                details = {
                    "review_id": review_id,
                    "datasource_id": review["datasource_id"],
                    "definition_version": review["definition_version"],
                }
                connection.execute(
                    """
                    INSERT INTO run_promotion (
                        promotion_id, promotion_seq, run_id, decision, approval_mode,
                        decision_at, actor_type, actor_id, policy_version, reason, details_json
                    ) VALUES (?, ?, ?, 'approved', 'manual', ?, 'operator', ?, 'v1', ?, ?)
                    """,
                    (
                        promotion_id,
                        sequence,
                        review["run_id"],
                        current_at,
                        actor_id,
                        reason,
                        _json(details),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO manual_review_promotion (
                        review_id, decision_id, run_id, promotion_id, actor_id, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_id,
                        decision["decision_id"],
                        review["run_id"],
                        promotion_id,
                        actor_id,
                        reason,
                        current_at,
                    ),
                )
                self._audit(
                    connection,
                    actor_type="operator",
                    actor_id=actor_id,
                    action="manual_promotion_approved",
                    target_type="run_promotion",
                    target_id=promotion_id,
                    details=details,
                    at=current_at,
                )
                return ManualPromotionResult(
                    review_id, review["run_id"], promotion_id, True
                )
        finally:
            connection.close()

    @_single_writer
    def revoke_promotion(
        self,
        run_id: str,
        *,
        actor_id: str,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> PromotionRevocationResult:
        """Append a revocation without rewriting the promotion history.

        The canonical/as-of views select the latest decision for a run, so a
        later revocation withdraws the run from current results while readers
        anchored before this action keep seeing its original approval.
        """

        if not run_id:
            raise OperationalError("run ID is required")
        if not actor_id:
            raise OperationalError("revocation actor is required")
        self.migrate()
        current_at = _timestamp(now)
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                latest = connection.execute(
                    """
                    SELECT p.promotion_id, p.decision, p.approval_mode,
                           r.datasource_id, r.definition_version,
                           r.lane, r.status AS run_status,
                           a.status AS attempt_status
                    FROM run_promotion AS p
                    JOIN ingestion_run AS r ON r.run_id = p.run_id
                    JOIN workflow_attempt AS a ON a.attempt_id = r.attempt_id
                    WHERE p.run_id = ?
                    ORDER BY p.decision_at DESC, p.promotion_seq DESC
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if latest is None:
                    raise OperationalError("run has no promotion to revoke")
                if latest["decision"] == "revoked":
                    return PromotionRevocationResult(run_id, latest["promotion_id"], False)
                if latest["decision"] != "approved":
                    raise OperationalError("only an approved promotion can be revoked")
                if (
                    latest["lane"] != "production_ingestion"
                    or latest["run_status"] != "succeeded"
                    or latest["attempt_status"] != "succeeded"
                ):
                    raise OperationalError("revocation target must be a successful production run")
                promotion_id = new_id("promotion")
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(promotion_seq), 0) + 1 FROM run_promotion"
                ).fetchone()[0]
                details = {
                    "revokes_promotion_id": latest["promotion_id"],
                    "datasource_id": latest["datasource_id"],
                    "definition_version": latest["definition_version"],
                }
                connection.execute(
                    """
                    INSERT INTO run_promotion (
                        promotion_id, promotion_seq, run_id, decision, approval_mode,
                        decision_at, actor_type, actor_id, policy_version, reason, details_json
                    ) VALUES (?, ?, ?, 'revoked', ?, ?, 'operator', ?, 'v1', ?, ?)
                    """,
                    (
                        promotion_id,
                        sequence,
                        run_id,
                        latest["approval_mode"],
                        current_at,
                        actor_id,
                        reason,
                        _json(details),
                    ),
                )
                self._audit(
                    connection,
                    actor_type="operator",
                    actor_id=actor_id,
                    action="promotion_revoked",
                    target_type="run_promotion",
                    target_id=promotion_id,
                    details=details,
                    at=current_at,
                )
                return PromotionRevocationResult(run_id, promotion_id, True)
        finally:
            connection.close()

    @_single_writer
    def persist_observation(
        self,
        run: RunHandle,
        *,
        record_key: tuple[str, ...],
        payload: Mapping[str, Any],
        record_type: str,
        category: str,
        evidence: Iterable[PersistedEvidence],
        source_date: str | None = None,
        period_label: str | None = None,
        unit: str | None = None,
        definition_text: str | None = None,
        limitations: Iterable[str] = (),
        locator: Mapping[str, Any] | None = None,
        reason: str = "first_seen",
        now: datetime | None = None,
    ) -> str:
        """Create/reuse an immutable revision and attach current run evidence."""

        definition = self.registry.lookup(run.datasource_id, run.definition_version)
        evidence_items = tuple(evidence)
        if not evidence_items:
            raise OperationalError("an observation requires persisted evidence")
        key_hash = record_key_hash(run.datasource_id, definition.record_key_version, record_key)
        record_digest = observation_hash(
            datasource_id=run.datasource_id,
            definition_version=run.definition_version,
            record_type=record_type,
            schema_version=definition.schema_version,
            revision_action="upsert",
            record_key=record_key,
            payload=payload,
            source_date=source_date,
            period_label=period_label,
            unit=unit,
            data_kind=definition.data_kind,
            confidence=definition.default_confidence,
            definition=definition_text,
            limitations=tuple(limitations),
        )
        current_at = _timestamp(now)
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                self._require_running_run(connection, run, at=current_at)
                for item in evidence_items:
                    bound = connection.execute(
                        """
                        SELECT 1 FROM run_evidence
                        WHERE run_id = ? AND evidence_id = ?
                        """,
                        (run.run_id, item.evidence_id),
                    ).fetchone()
                    if bound is None:
                        raise OperationalError(
                            "observation evidence must be attached to its ingestion run"
                        )
                run_metadata = connection.execute(
                    "SELECT snapshot_scope_hash FROM ingestion_run WHERE run_id = ?",
                    (run.run_id,),
                ).fetchone()
                snapshot_scope_hash = run_metadata["snapshot_scope_hash"] if run_metadata else None
                if definition.snapshot_mode == "full_snapshot" and not snapshot_scope_hash:
                    raise OperationalError("full snapshot observations require a declared scope")
                head = connection.execute(
                    """
                    SELECT observation_id, record_hash, revision_no, revision_action
                    FROM record_stream_head
                    WHERE datasource_id = ? AND lane = ? AND record_key_version = ?
                      AND record_key_hash = ?
                    """,
                    (run.datasource_id, run.lane, definition.record_key_version, key_hash),
                ).fetchone()
                if head is not None and head["record_hash"] == record_digest and head["revision_action"] == "upsert":
                    observation_id = head["observation_id"]
                else:
                    observation_id = new_id("obs")
                    revision_no = 1 if head is None else head["revision_no"] + 1
                    revision_reason = reason if head is None else ("reappearance" if head["revision_action"] == "tombstone" else "changed")
                    connection.execute(
                        """
                        INSERT INTO observation_revision (
                            observation_id, datasource_id, definition_version, lane,
                            record_key_version, record_key_json, record_key_hash,
                            snapshot_scope_hash, revision_no, revision_action, revision_reason, record_hash,
                            category, record_type, payload_json, source_date, period_label,
                            unit, data_kind, confidence, definition, limitations_json,
                            parser_version, schema_version, supersedes_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'upsert', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            observation_id,
                            run.datasource_id,
                            run.definition_version,
                            run.lane,
                            definition.record_key_version,
                            _json(list(record_key)),
                            key_hash,
                            snapshot_scope_hash,
                            revision_no,
                            revision_reason,
                            record_digest,
                            category,
                            record_type,
                            _json(payload),
                            source_date,
                            period_label,
                            unit,
                            definition.data_kind,
                            definition.default_confidence,
                            definition_text,
                            _json(list(limitations)),
                            definition.parser_version,
                            definition.schema_version,
                            head["observation_id"] if head is not None else None,
                            current_at,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO record_stream_head (
                            datasource_id, lane, record_key_version, record_key_hash,
                            observation_id, record_hash, revision_no, revision_action, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'upsert', ?)
                        ON CONFLICT(datasource_id, lane, record_key_version, record_key_hash)
                        DO UPDATE SET observation_id = excluded.observation_id,
                                      record_hash = excluded.record_hash,
                                      revision_no = excluded.revision_no,
                                      revision_action = excluded.revision_action,
                                      updated_at = excluded.updated_at
                        """,
                        (
                            run.datasource_id,
                            run.lane,
                            definition.record_key_version,
                            key_hash,
                            observation_id,
                            record_digest,
                            revision_no,
                            current_at,
                        ),
                    )
                linked = connection.execute(
                    "INSERT OR IGNORE INTO run_observation (run_id, observation_id) VALUES (?, ?)",
                    (run.run_id, observation_id),
                ).rowcount
                if linked:
                    connection.execute(
                        """
                        UPDATE ingestion_run
                        SET record_count = record_count + 1,
                            accepted_record_count = accepted_record_count + 1
                        WHERE run_id = ? AND status = 'running'
                        """,
                        (run.run_id,),
                    )
                for item in evidence_items:
                    evidence_locator: dict[str, Any] = {
                        "evidence_id": item.evidence_id,
                        "role": "primary",
                    }
                    if locator is not None:
                        evidence_locator["record_locator"] = dict(locator)
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO observation_evidence (
                            run_id, observation_id, evidence_id, locator_json, locator_hash
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            run.run_id,
                            observation_id,
                            item.evidence_id,
                            _json(evidence_locator),
                            locator_hash(evidence_locator),
                        ),
                    )
                return observation_id
        finally:
            connection.close()

    @_single_writer
    def begin_full_snapshot(
        self, run: RunHandle, *, scope: Mapping[str, Any], now: datetime | None = None
    ) -> str:
        """Declare one stable deletion scope before writing full-snapshot rows."""

        definition = self.registry.lookup(run.datasource_id, run.definition_version)
        if definition.snapshot_mode != "full_snapshot":
            raise OperationalError("only full_snapshot definitions may declare a deletion scope")
        scope_hash = request_hash({"snapshot_scope": scope})
        current_at = _timestamp(now)
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                self._require_running_run(connection, run, at=current_at)
                changed = connection.execute(
                    """
                    UPDATE ingestion_run
                    SET snapshot_scope_json = ?, snapshot_scope_hash = ?, snapshot_complete = 0
                    WHERE run_id = ? AND status = 'running'
                      AND snapshot_scope_hash IS NULL
                    """,
                    (_json({"scope": scope}), scope_hash, run.run_id),
                ).rowcount
                if changed != 1:
                    raise OperationalError("full snapshot scope is already set or run is inactive")
        finally:
            connection.close()
        return scope_hash

    @_single_writer
    def finalize_full_snapshot(
        self,
        run: RunHandle,
        *,
        completeness_proof: Mapping[str, Any],
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        """Create tombstones only for a proven-complete, stable snapshot scope.

        This method deliberately derives the seen keys from rows linked to this
        running attempt.  A caller cannot provide a free-form missing-key list
        to delete observations it did not actually produce evidence for.
        """

        definition = self.registry.lookup(run.datasource_id, run.definition_version)
        if definition.snapshot_mode != "full_snapshot":
            raise OperationalError("only full_snapshot definitions may infer tombstones")
        if not completeness_proof:
            raise OperationalError("full snapshot requires a non-empty completeness proof")
        current_at = _timestamp(now)
        tombstones: list[str] = []
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                self._require_running_run(connection, run, at=current_at)
                run_row = connection.execute(
                    """
                    SELECT snapshot_scope_json, snapshot_scope_hash FROM ingestion_run
                    WHERE run_id = ? AND status = 'running'
                    """,
                    (run.run_id,),
                ).fetchone()
                if run_row is None or not run_row["snapshot_scope_hash"]:
                    raise OperationalError("full snapshot scope must be declared before finalization")
                scope_hash = run_row["snapshot_scope_hash"]
                if (
                    completeness_proof.get("complete") is not True
                    or completeness_proof.get("snapshot_scope_hash") != scope_hash
                    or not isinstance(completeness_proof.get("schema_version"), str)
                ):
                    raise OperationalError(
                        "full snapshot completeness proof must be validated for its exact scope"
                    )
                evidence_row = connection.execute(
                    """
                    SELECT evidence_id FROM run_evidence
                    WHERE run_id = ? AND required = 1 ORDER BY evidence_id LIMIT 1
                    """,
                    (run.run_id,),
                ).fetchone()
                if evidence_row is None:
                    raise OperationalError("full snapshot tombstones require current-run evidence")
                seen_rows = connection.execute(
                    """
                    SELECT o.record_key_hash
                    FROM run_observation AS ro
                    JOIN observation_revision AS o ON o.observation_id = ro.observation_id
                    WHERE ro.run_id = ? AND o.revision_action = 'upsert'
                    """,
                    (run.run_id,),
                ).fetchall()
                seen = {row["record_key_hash"] for row in seen_rows}
                heads = connection.execute(
                    """
                    SELECT h.observation_id AS previous_id, h.record_key_hash,
                           h.revision_no, o.record_key_json, o.category, o.record_type,
                           o.source_date, o.period_label, o.unit, o.data_kind,
                           o.confidence, o.definition, o.limitations_json
                    FROM record_stream_head AS h
                    JOIN observation_revision AS o ON o.observation_id = h.observation_id
                    WHERE h.datasource_id = ? AND h.lane = ?
                      AND h.record_key_version = ? AND h.revision_action = 'upsert'
                      AND o.snapshot_scope_hash = ?
                    ORDER BY h.record_key_hash
                    """,
                    (
                        run.datasource_id,
                        run.lane,
                        definition.record_key_version,
                        scope_hash,
                    ),
                ).fetchall()
                for head in heads:
                    if head["record_key_hash"] in seen:
                        continue
                    record_key = json.loads(head["record_key_json"])
                    digest = observation_hash(
                        datasource_id=run.datasource_id,
                        definition_version=run.definition_version,
                        record_type=head["record_type"],
                        schema_version=definition.schema_version,
                        revision_action="tombstone",
                        record_key=record_key,
                        payload={},
                        source_date=head["source_date"],
                        period_label=head["period_label"],
                        unit=head["unit"],
                        data_kind=head["data_kind"],
                        confidence=head["confidence"],
                        snapshot_scope_hash=scope_hash,
                        definition=head["definition"],
                        limitations=json.loads(head["limitations_json"]),
                    )
                    observation_id = new_id("obs")
                    connection.execute(
                        """
                        INSERT INTO observation_revision (
                            observation_id, datasource_id, definition_version, lane,
                            record_key_version, record_key_json, record_key_hash,
                            snapshot_scope_hash, revision_no, revision_action,
                            revision_reason, record_hash, category, record_type,
                            payload_json, source_date, period_label, unit, data_kind,
                            confidence, definition, limitations_json, parser_version,
                            schema_version, supersedes_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'tombstone', 'tombstone',
                                  ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            observation_id,
                            run.datasource_id,
                            run.definition_version,
                            run.lane,
                            definition.record_key_version,
                            head["record_key_json"],
                            head["record_key_hash"],
                            scope_hash,
                            head["revision_no"] + 1,
                            digest,
                            head["category"],
                            head["record_type"],
                            head["source_date"],
                            head["period_label"],
                            head["unit"],
                            head["data_kind"],
                            head["confidence"],
                            head["definition"],
                            head["limitations_json"],
                            definition.parser_version,
                            definition.schema_version,
                            head["previous_id"],
                            current_at,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE record_stream_head
                        SET observation_id = ?, record_hash = ?, revision_no = ?,
                            revision_action = 'tombstone', updated_at = ?
                        WHERE datasource_id = ? AND lane = ? AND record_key_version = ?
                          AND record_key_hash = ? AND observation_id = ?
                        """,
                        (
                            observation_id,
                            digest,
                            head["revision_no"] + 1,
                            current_at,
                            run.datasource_id,
                            run.lane,
                            definition.record_key_version,
                            head["record_key_hash"],
                            head["previous_id"],
                        ),
                    )
                    connection.execute(
                        "INSERT INTO run_observation (run_id, observation_id) VALUES (?, ?)",
                        (run.run_id, observation_id),
                    )
                    locator = {"evidence_id": evidence_row["evidence_id"], "role": "snapshot_tombstone"}
                    connection.execute(
                        """
                        INSERT INTO observation_evidence (
                            run_id, observation_id, evidence_id, locator_json, locator_hash
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            run.run_id,
                            observation_id,
                            evidence_row["evidence_id"],
                            _json(locator),
                            locator_hash(locator),
                        ),
                    )
                    tombstones.append(observation_id)
                scope_payload = json.loads(run_row["snapshot_scope_json"])
                scope_payload["completeness_proof"] = completeness_proof
                connection.execute(
                    """
                    UPDATE ingestion_run
                    SET snapshot_complete = 1, snapshot_scope_json = ?,
                        record_count = record_count + ?,
                        accepted_record_count = accepted_record_count + ?
                    WHERE run_id = ? AND status = 'running'
                    """,
                    (
                        _json(scope_payload),
                        len(tombstones),
                        len(tombstones),
                        run.run_id,
                    ),
                )
        finally:
            connection.close()
        return tuple(tombstones)

    @_single_writer
    def finish_run(
        self,
        run: RunHandle,
        *,
        status: str,
        retryable: bool = False,
        retry_at: datetime | None = None,
        error: Mapping[str, Any] | None = None,
        promote: bool = False,
        now: datetime | None = None,
    ) -> None:
        """Terminally close an attempt while preserving earlier valid values."""

        if status not in {"succeeded", "empty", "partial", "failed", "cancelled"}:
            raise OperationalError("invalid terminal attempt status")
        if retry_at is not None and not retryable:
            raise OperationalError("retry_at requires a retryable run")
        definition = self.registry.lookup(run.datasource_id, run.definition_version)
        if promote:
            if status != "succeeded" or run.lane != "production_ingestion":
                raise OperationalError("only successful production runs can be promoted")
            if definition.promotion_policy != "automatic":
                raise OperationalError("definition requires an approved manual review")
        current_at = _timestamp(now)
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                job = self._require_running_run(connection, run, at=current_at)
                if status in {"succeeded", "empty"}:
                    job_state = status
                elif retryable and job["attempt_count"] < job["max_attempts"]:
                    job_state = "retry_wait"
                elif retryable:
                    job_state = "dead_letter"
                else:
                    job_state = "failed" if status != "cancelled" else "cancelled"
                normal_retry_at = _parse_timestamp(
                    _retry_timestamp(current_at, job["retry_policy_json"], job["attempt_count"])
                )
                available_at = _timestamp(
                    _latest_timestamp(normal_retry_at, _as_utc(retry_at))
                    if retry_at is not None
                    else normal_retry_at
                )
                connection.execute(
                    """
                    UPDATE workflow_attempt
                    SET status = ?, completed_at = ?, heartbeat_at = ?, error_json = ?
                    WHERE attempt_id = ? AND status = 'running'
                    """,
                    (status, current_at, current_at, _json(error) if error else None, run.attempt_id),
                )
                connection.execute(
                    """
                    UPDATE ingestion_run
                    SET status = ?, completed_at = ?, retrieved_at = ?
                    WHERE run_id = ?
                    """,
                    (status, current_at, current_at, run.run_id),
                )
                connection.execute(
                    """
                    UPDATE workflow_job
                    SET state = ?, completed_at = CASE WHEN ? IN ('succeeded', 'empty', 'failed', 'dead_letter', 'cancelled') THEN ? ELSE NULL END,
                        available_at = CASE WHEN ? = 'retry_wait' THEN ? ELSE available_at END,
                        claim_token = NULL, claimed_by = NULL, claimed_at = NULL,
                        lease_expires_at = NULL, heartbeat_at = ?, last_error_json = ?
                    WHERE job_id = ? AND claim_token = ? AND state = 'running'
                    """,
                    (
                        job_state,
                        job_state,
                        current_at,
                        job_state,
                        available_at,
                        current_at,
                        _json(error) if error else None,
                        run.job_id,
                        run.claim_token,
                    ),
                )
                if promote:
                    if definition.snapshot_mode == "full_snapshot":
                        complete = connection.execute(
                            "SELECT snapshot_complete FROM ingestion_run WHERE run_id = ?",
                            (run.run_id,),
                        ).fetchone()
                        if complete is None or complete["snapshot_complete"] != 1:
                            raise OperationalError("full snapshot must be finalized before promotion")
                    self._insert_promotion(connection, run.run_id, current_at)
        finally:
            connection.close()

    def jobs(self) -> tuple[dict[str, object], ...]:
        if not self._is_initialized():
            return ()
        connection = connect_database(self.database_path, read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT job_id, datasource_id, definition_version, lane, trigger,
                       state, attempt_count, max_attempts, created_at, completed_at
                FROM workflow_job ORDER BY created_at DESC, job_id DESC
                """
            ).fetchall()
            return tuple(dict(row) for row in rows)
        finally:
            connection.close()

    def get_job(self, job_id: str) -> dict[str, object] | None:
        """Return bounded status/lineage metadata without raw evidence bytes."""

        if not self._is_initialized():
            return None
        connection = connect_database(self.database_path, read_only=True)
        try:
            job = connection.execute(
                """
                SELECT job_id, job_kind, datasource_id, definition_version, lane, trigger,
                       state, attempt_count, max_attempts, request_instance_id,
                       parent_job_id, generation, created_at, started_at, completed_at,
                       available_at, window_start, window_end, last_error_json
                FROM workflow_job WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if job is None:
                return None
            attempts = connection.execute(
                """
                SELECT attempt_id, attempt_no, status, started_at, completed_at, error_json
                FROM workflow_attempt WHERE job_id = ? ORDER BY attempt_no
                """,
                (job_id,),
            ).fetchall()
            run = connection.execute(
                """
                SELECT run_id, status, record_count, accepted_record_count,
                       rejected_record_count, completed_at
                FROM ingestion_run WHERE job_id = ? ORDER BY created_at DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            promotions = ()
            if run is not None:
                promotions = connection.execute(
                    """
                    SELECT decision, decision_at, promotion_seq
                    FROM run_promotion WHERE run_id = ? ORDER BY promotion_seq
                    """,
                    (run["run_id"],),
                ).fetchall()
        finally:
            connection.close()
        result = dict(job)
        last_error = result.pop("last_error_json")
        result["last_error"] = json.loads(last_error) if last_error else None
        attempt_rows = []
        for attempt in attempts:
            attempt_row = dict(attempt)
            error = attempt_row.pop("error_json")
            attempt_row["error"] = json.loads(error) if error else None
            attempt_rows.append(attempt_row)
        result["attempts"] = attempt_rows
        result["run"] = dict(run) if run is not None else None
        result["promotions"] = [dict(promotion) for promotion in promotions]
        return result

    @_single_writer
    def submit_agent_refresh(
        self,
        *,
        request_id: str,
        principal: str,
        request_fingerprint: str,
        dedupe_key: str,
        datasource_id: str,
        definition_version: int,
        request_profile: str,
        lane: str,
        bounded_scope: Mapping[str, tuple[str, ...]],
        intent: str,
        submitted_at: datetime,
        cooldown_until: datetime,
        confirmation_token: str | None = None,
    ) -> DurableRefreshResult:
        """Durably bind one bounded agent refresh to its workflow job.

        The request ledger, rather than an in-process broker cache, is the
        authority for idempotency, cooldown sharing, and later status access.
        A crash after job creation but before ledger insertion is recovered
        from the immutable refresh-control metadata persisted with that job.
        """

        _require_refresh_identifier("request_id", request_id)
        _require_refresh_identifier("principal", principal)
        _require_refresh_digest("request_fingerprint", request_fingerprint)
        _require_refresh_digest("dedupe_key", dedupe_key)
        if not request_profile or not intent:
            raise OperationalError("refresh profile and intent are required")
        _require_optional_refresh_confirmation_token(confirmation_token)
        if confirmation_token is not None and datasource_id != _ONSPD_REFRESH_DATASOURCE_ID:
            raise RefreshConfirmationError(
                "confirmation is only supported for the ONSPD refresh tool"
            )
        anchor = _as_utc(submitted_at)
        cooldown = _as_utc(cooldown_until)
        if cooldown < anchor:
            raise OperationalError("refresh cooldown cannot precede submission")
        self.sync_registry(now=anchor)
        try:
            definition = self.registry.lookup(datasource_id, definition_version)
        except RegistryError as error:
            raise OperationalError(str(error)) from error

        existing = self._refresh_request_by_id(request_id)
        if existing is not None:
            return self._validate_existing_refresh_request(
                existing,
                principal=principal,
                request_fingerprint=request_fingerprint,
            )

        pending_confirmation = self._refresh_confirmation_by_request_id(request_id)
        if pending_confirmation is not None:
            self._validate_refresh_confirmation_identity(
                pending_confirmation,
                principal=principal,
                request_fingerprint=request_fingerprint,
                datasource_id=datasource_id,
                definition_version=definition_version,
            )

        recovered = self._refresh_job_by_request_id(request_id)
        if recovered is not None:
            control = _refresh_control(recovered["request_json"])
            if control["principal"] != principal:
                raise RefreshRequestAccessError(
                    "request_instance_id belongs to another principal"
                )
            if control["request_fingerprint"] != request_fingerprint:
                raise RefreshRequestReplayError(
                    "request_instance_id was reused for a different request"
                )
            return self._record_refresh_request(
                request_id=request_id,
                principal=principal,
                request_fingerprint=request_fingerprint,
                dedupe_key=control["dedupe_key"],
                datasource_id=datasource_id,
                definition_version=definition_version,
                request_profile=request_profile,
                job_id=recovered["job_id"],
                disposition="accepted",
                initial_state=control["initial_state"],
                submitted_at=_parse_timestamp(control["submitted_at"]),
                cooldown_until=_parse_timestamp(control["cooldown_until"]),
            )

        prior = self._active_refresh_dedupe(dedupe_key, anchor)
        if prior is not None:
            return self._record_refresh_request(
                request_id=request_id,
                principal=principal,
                request_fingerprint=request_fingerprint,
                dedupe_key=dedupe_key,
                datasource_id=datasource_id,
                definition_version=definition_version,
                request_profile=request_profile,
                job_id=prior["job_id"],
                disposition="deduplicated",
                initial_state=prior["initial_state"],
                submitted_at=anchor,
                cooldown_until=_parse_timestamp(prior["cooldown_until"]),
            )

        confirmation = self._onspd_refresh_confirmation(
            request_id=request_id,
            principal=principal,
            request_fingerprint=request_fingerprint,
            datasource_id=datasource_id,
            definition_version=definition_version,
            submitted_at=anchor,
            confirmation_token=confirmation_token,
            pending=pending_confirmation,
        )
        if confirmation is not None:
            return confirmation

        control = {
            "principal": principal,
            "request_fingerprint": request_fingerprint,
            "dedupe_key": dedupe_key,
            "submitted_at": _timestamp(anchor),
            "cooldown_until": _timestamp(cooldown),
            "initial_state": "queued",
        }
        request = {
            **_thaw(definition.default_request),
            "refresh_profile": request_profile,
            "scope": {key: list(values) for key, values in bounded_scope.items()},
            "intent": intent,
            "_refresh_control": control,
        }
        queued = self.enqueue(
            datasource_id,
            definition_version=definition_version,
            request=request,
            trigger="agent_request",
            lane=lane,
            scheduled_for=anchor,
            request_instance_id=request_id,
        )
        if queued.disposition != "accepted":
            # The only expected route is a process crash between durable job
            # creation and ledger insertion; resolve it through the explicit
            # recovery branch above instead of binding an unknown job here.
            raise OperationalError("refresh job was not accepted for ledger binding")
        return self._record_refresh_request(
            request_id=request_id,
            principal=principal,
            request_fingerprint=request_fingerprint,
            dedupe_key=dedupe_key,
            datasource_id=datasource_id,
            definition_version=definition_version,
            request_profile=request_profile,
            job_id=queued.job_id,
            disposition="accepted",
            initial_state=queued.state,
            submitted_at=anchor,
            cooldown_until=cooldown,
        )

    def get_agent_refresh_job(
        self, job_id: str, *, principal: str
    ) -> dict[str, object] | None:
        """Return a job only when the durable refresh ledger grants its principal."""

        _require_refresh_identifier("job_id", job_id)
        _require_refresh_identifier("principal", principal)
        if not self._is_initialized():
            raise RefreshRequestAccessError("job is not visible to this context")
        connection = connect_database(self.database_path, read_only=True)
        try:
            permitted = connection.execute(
                """
                SELECT 1 FROM refresh_request
                WHERE job_id = ? AND principal = ?
                LIMIT 1
                """,
                (job_id, principal),
            ).fetchone()
        finally:
            connection.close()
        if permitted is None:
            raise RefreshRequestAccessError("job is not visible to this context")
        return self.get_job(job_id)

    @_single_writer
    def create_agent_refresh_approval(
        self,
        *,
        refresh_request_id: str,
        principal: str,
        capability_scope_id: str,
        capability_id: str,
        manifest_version: str,
        profile_version: str,
        request_fingerprint: str,
        datasource_id: str,
        request_profile: str,
        bounded_scope: Mapping[str, object],
        intent: str,
        now: datetime | None = None,
    ) -> AgentRefreshApproval:
        """Bind a confirmation-required request to one opaque host approval.

        This is deliberately called only after a trusted broker has returned
        ``confirmation_required``.  It reads the existing confirmation row but
        never copies its token into the immutable approval mapping.
        """

        _require_refresh_identifier("refresh_request_id", refresh_request_id)
        _require_refresh_identifier("principal", principal)
        _require_agent_approval_identifier("capability_scope_id", capability_scope_id)
        _require_agent_approval_identifier("capability_id", capability_id)
        _require_agent_approval_identifier("manifest_version", manifest_version)
        _require_agent_approval_identifier("profile_version", profile_version)
        _require_refresh_digest("request_fingerprint", request_fingerprint)
        snapshot = _agent_refresh_snapshot(
            datasource_id=datasource_id,
            request_profile=request_profile,
            bounded_scope=bounded_scope,
            intent=intent,
        )
        if _agent_refresh_snapshot_fingerprint(snapshot) != request_fingerprint:
            raise RefreshApprovalReplayError(
                "approval snapshot does not match the durable refresh request"
            )
        anchor = _as_utc(now)
        if not self._is_initialized():
            raise RefreshApprovalError("refresh confirmation is not available")
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                confirmation = connection.execute(
                    """
                    SELECT request_id, principal, request_fingerprint, datasource_id,
                           issued_at, expires_at
                    FROM refresh_confirmation WHERE request_id = ?
                    """,
                    (refresh_request_id,),
                ).fetchone()
                if confirmation is None:
                    raise RefreshApprovalError("refresh confirmation is not available")
                _validate_approval_confirmation(
                    confirmation,
                    principal=principal,
                    request_fingerprint=request_fingerprint,
                    datasource_id=datasource_id,
                    at=anchor,
                )
                existing = connection.execute(
                    """
                    SELECT approval_id, refresh_request_id, principal, capability_scope_id,
                           capability_id, manifest_version, profile_version,
                           request_fingerprint, request_snapshot_json, issued_at, expires_at
                    FROM agent_refresh_approval WHERE refresh_request_id = ?
                    """,
                    (refresh_request_id,),
                ).fetchone()
                if existing is not None:
                    approval = _agent_refresh_approval_from_row(existing)
                    _validate_agent_refresh_approval(
                        approval,
                        principal=principal,
                        capability_scope_id=capability_scope_id,
                        capability_id=capability_id,
                        manifest_version=manifest_version,
                        profile_version=profile_version,
                        request_fingerprint=request_fingerprint,
                        at=anchor,
                    )
                    return approval

                approval = AgentRefreshApproval(
                    approval_id=new_id("approval"),
                    refresh_request_id=refresh_request_id,
                    principal=principal,
                    capability_scope_id=capability_scope_id,
                    capability_id=capability_id,
                    manifest_version=manifest_version,
                    profile_version=profile_version,
                    request_fingerprint=request_fingerprint,
                    snapshot=_freeze_agent_refresh_snapshot(snapshot),
                    issued_at=_parse_timestamp(confirmation["issued_at"]),
                    expires_at=_parse_timestamp(confirmation["expires_at"]),
                )
                connection.execute(
                    """
                    INSERT INTO agent_refresh_approval (
                        approval_id, refresh_request_id, principal, capability_scope_id,
                        capability_id, manifest_version, profile_version,
                        request_fingerprint, request_snapshot_json, issued_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval.approval_id,
                        approval.refresh_request_id,
                        approval.principal,
                        approval.capability_scope_id,
                        approval.capability_id,
                        approval.manifest_version,
                        approval.profile_version,
                        approval.request_fingerprint,
                        _json(snapshot),
                        _timestamp(approval.issued_at),
                        _timestamp(approval.expires_at),
                    ),
                )
                return approval
        finally:
            connection.close()

    def recover_agent_refresh_approval(
        self,
        approval_id: str,
        *,
        principal: str,
        capability_scope_id: str,
        capability_id: str,
        manifest_version: str,
        profile_version: str,
        request_fingerprint: str,
        now: datetime | None = None,
    ) -> RecoveredAgentRefreshApproval:
        """Recover the trusted token and original snapshot for host-only replay.

        The token is retrieved only after validating the opaque approval's
        principal, capability scope, frozen policy versions, exact request
        fingerprint, and expiry.  This method is intentionally not imported by
        the model-facing agent tool package.
        """

        approval = self._validated_agent_refresh_approval(
            approval_id,
            principal=principal,
            capability_scope_id=capability_scope_id,
            capability_id=capability_id,
            manifest_version=manifest_version,
            profile_version=profile_version,
            request_fingerprint=request_fingerprint,
            now=now,
            include_token=True,
        )
        assert isinstance(approval, RecoveredAgentRefreshApproval)
        return approval

    def lookup_agent_refresh_approval(
        self,
        approval_id: str,
        *,
        now: datetime | None = None,
    ) -> AgentRefreshApproval:
        """Load a host-only approval binding without recovering its token.

        This narrow helper exists so the facade can obtain the immutable
        identity values it must pass back into the context-checking recovery
        and decision methods after a one-shot child restart.  It never returns
        the confirmation token and is not exported through a model-facing
        surface.
        """

        _require_agent_approval_identifier("approval_id", approval_id)
        anchor = _as_utc(now)
        if not self._is_initialized():
            raise RefreshApprovalAccessError("approval is not available to this host context")
        connection = connect_database(self.database_path, read_only=True)
        try:
            row = connection.execute(
                """
                SELECT approval_id, refresh_request_id, principal, capability_scope_id,
                       capability_id, manifest_version, profile_version,
                       request_fingerprint, request_snapshot_json, issued_at, expires_at
                FROM agent_refresh_approval WHERE approval_id = ?
                """,
                (approval_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RefreshApprovalAccessError("approval is not available to this host context")
        approval = _agent_refresh_approval_from_row(row)
        if approval.expires_at <= anchor:
            raise RefreshApprovalExpiredError("approval has expired")
        return approval

    @_single_writer
    def decide_agent_refresh_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        principal: str,
        capability_scope_id: str,
        capability_id: str,
        manifest_version: str,
        profile_version: str,
        request_fingerprint: str,
        actor_id: str,
        now: datetime | None = None,
    ) -> AgentRefreshApprovalDecision:
        """Append an approve/deny event, replaying only an identical decision.

        The host calls this before a broker resend.  A later retry creates a
        ``replay`` event and can safely re-run the durable refresh submission;
        a conflicting decision is rejected without changing earlier history.
        """

        _require_agent_approval_identifier("approval_id", approval_id)
        if decision not in {"approve", "deny"}:
            raise RefreshApprovalError("approval decision must be approve or deny")
        _require_refresh_identifier("principal", principal)
        _require_agent_approval_identifier("capability_scope_id", capability_scope_id)
        _require_agent_approval_identifier("capability_id", capability_id)
        _require_agent_approval_identifier("manifest_version", manifest_version)
        _require_agent_approval_identifier("profile_version", profile_version)
        _require_refresh_digest("request_fingerprint", request_fingerprint)
        _require_agent_approval_identifier("actor_id", actor_id)
        anchor = _as_utc(now)
        if not self._is_initialized():
            raise RefreshApprovalAccessError("approval is not available to this host context")
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                row = connection.execute(
                    """
                    SELECT approval_id, refresh_request_id, principal, capability_scope_id,
                           capability_id, manifest_version, profile_version,
                           request_fingerprint, request_snapshot_json, issued_at, expires_at
                    FROM agent_refresh_approval WHERE approval_id = ?
                    """,
                    (approval_id,),
                ).fetchone()
                if row is None:
                    raise RefreshApprovalAccessError(
                        "approval is not available to this host context"
                    )
                approval = _agent_refresh_approval_from_row(row)
                _validate_agent_refresh_approval(
                    approval,
                    principal=principal,
                    capability_scope_id=capability_scope_id,
                    capability_id=capability_id,
                    manifest_version=manifest_version,
                    profile_version=profile_version,
                    request_fingerprint=request_fingerprint,
                    at=anchor,
                )
                prior = connection.execute(
                    """
                    SELECT decision FROM agent_refresh_approval_event
                    WHERE approval_id = ? AND event_type = 'decision'
                    """,
                    (approval_id,),
                ).fetchone()
                if prior is not None and prior["decision"] != decision:
                    raise ApprovalDecisionConflictError(
                        "approval already has a conflicting immutable decision"
                    )
                outcome = "replay" if prior is not None else "decision"
                sequence = connection.execute(
                    """
                    SELECT COALESCE(MAX(event_seq), 0) + 1 AS next_event_seq
                    FROM agent_refresh_approval_event WHERE approval_id = ?
                    """,
                    (approval_id,),
                ).fetchone()["next_event_seq"]
                event_id = new_id("approval_event")
                connection.execute(
                    """
                    INSERT INTO agent_refresh_approval_event (
                        event_id, approval_id, event_seq, event_type, decision,
                        actor_type, actor_id, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'host', ?, ?, ?)
                    """,
                    (
                        event_id,
                        approval_id,
                        sequence,
                        outcome,
                        decision,
                        actor_id,
                        _json({"refresh_request_id": approval.refresh_request_id}),
                        _timestamp(anchor),
                    ),
                )
                return AgentRefreshApprovalDecision(
                    approval_id=approval_id,
                    event_id=event_id,
                    decision=decision,
                    outcome="replayed" if outcome == "replay" else "recorded",
                    decided_at=anchor,
                )
        finally:
            connection.close()

    def _validated_agent_refresh_approval(
        self,
        approval_id: str,
        *,
        principal: str,
        capability_scope_id: str,
        capability_id: str,
        manifest_version: str,
        profile_version: str,
        request_fingerprint: str,
        now: datetime | None,
        include_token: bool,
    ) -> AgentRefreshApproval | RecoveredAgentRefreshApproval:
        _require_agent_approval_identifier("approval_id", approval_id)
        _require_refresh_identifier("principal", principal)
        _require_agent_approval_identifier("capability_scope_id", capability_scope_id)
        _require_agent_approval_identifier("capability_id", capability_id)
        _require_agent_approval_identifier("manifest_version", manifest_version)
        _require_agent_approval_identifier("profile_version", profile_version)
        _require_refresh_digest("request_fingerprint", request_fingerprint)
        anchor = _as_utc(now)
        if not self._is_initialized():
            raise RefreshApprovalAccessError("approval is not available to this host context")
        connection = connect_database(self.database_path, read_only=True)
        try:
            row = connection.execute(
                """
                SELECT approval.approval_id, approval.refresh_request_id,
                       approval.principal, approval.capability_scope_id,
                       approval.capability_id, approval.manifest_version,
                       approval.profile_version, approval.request_fingerprint,
                       approval.request_snapshot_json, approval.issued_at,
                       approval.expires_at, confirmation.confirmation_token,
                       confirmation.principal AS confirmation_principal,
                       confirmation.request_fingerprint AS confirmation_fingerprint,
                       confirmation.datasource_id AS confirmation_datasource_id,
                       confirmation.issued_at AS confirmation_issued_at,
                       confirmation.expires_at AS confirmation_expires_at
                FROM agent_refresh_approval AS approval
                JOIN refresh_confirmation AS confirmation
                  ON confirmation.request_id = approval.refresh_request_id
                WHERE approval.approval_id = ?
                """,
                (approval_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RefreshApprovalAccessError("approval is not available to this host context")
        approval = _agent_refresh_approval_from_row(row)
        _validate_agent_refresh_approval(
            approval,
            principal=principal,
            capability_scope_id=capability_scope_id,
            capability_id=capability_id,
            manifest_version=manifest_version,
            profile_version=profile_version,
            request_fingerprint=request_fingerprint,
            at=anchor,
        )
        snapshot = approval.snapshot
        _validate_approval_confirmation(
            row,
            principal=approval.principal,
            request_fingerprint=approval.request_fingerprint,
            datasource_id=snapshot["datasource_id"],
            at=anchor,
            principal_key="confirmation_principal",
            fingerprint_key="confirmation_fingerprint",
            datasource_key="confirmation_datasource_id",
            expires_key="confirmation_expires_at",
        )
        if (
            row["confirmation_issued_at"] != _timestamp(approval.issued_at)
            or row["confirmation_expires_at"] != _timestamp(approval.expires_at)
        ):
            raise RefreshApprovalReplayError("approval confirmation metadata changed")
        if not include_token:
            return approval
        token = row["confirmation_token"]
        _require_optional_refresh_confirmation_token(token)
        assert isinstance(token, str)
        return RecoveredAgentRefreshApproval(approval=approval, confirmation_token=token)

    def _refresh_request_by_id(self, request_id: str) -> object | None:
        connection = connect_database(self.database_path, read_only=True)
        try:
            return connection.execute(
                """
                SELECT request_id, principal, request_fingerprint, job_id,
                       disposition, initial_state, submitted_at, cooldown_until
                FROM refresh_request WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        finally:
            connection.close()

    def _refresh_confirmation_by_request_id(self, request_id: str) -> object | None:
        connection = connect_database(self.database_path, read_only=True)
        try:
            return connection.execute(
                """
                SELECT confirmation_token, request_id, principal, request_fingerprint,
                       datasource_id, definition_version, issued_at, expires_at
                FROM refresh_confirmation WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        finally:
            connection.close()

    def _onspd_refresh_confirmation(
        self,
        *,
        request_id: str,
        principal: str,
        request_fingerprint: str,
        datasource_id: str,
        definition_version: int,
        submitted_at: datetime,
        confirmation_token: str | None,
        pending: object | None,
    ) -> DurableRefreshResult | None:
        """Enforce the competition ONSPD daily cap before creating a job.

        Only a new agent refresh job consumes quota.  Idempotent replays and
        cooldown deduplications have already returned above, so they cannot
        turn a harmless retry into an extra network-bound operation.
        """

        if datasource_id != _ONSPD_REFRESH_DATASOURCE_ID:
            return None
        if self._refresh_jobs_in_london_day(submitted_at) < _ONSPD_REFRESH_DAILY_LIMIT:
            if confirmation_token is not None:
                raise RefreshConfirmationError(
                    "confirmation is not required while the ONSPD daily quota remains"
                )
            return None
        if confirmation_token is None:
            return self._issue_refresh_confirmation(
                request_id=request_id,
                principal=principal,
                request_fingerprint=request_fingerprint,
                datasource_id=datasource_id,
                definition_version=definition_version,
                submitted_at=submitted_at,
                pending=pending,
            )
        if pending is None:
            raise RefreshConfirmationError("refresh confirmation is unknown")
        self._validate_refresh_confirmation(
            pending,
            confirmation_token=confirmation_token,
            submitted_at=submitted_at,
        )
        return None

    def _refresh_jobs_in_london_day(self, submitted_at: datetime) -> int:
        day_start, day_end = _london_day_window(submitted_at)
        connection = connect_database(self.database_path, read_only=True)
        try:
            row = connection.execute(
                """
                SELECT COUNT(*) AS job_count
                FROM workflow_job
                WHERE datasource_id = ?
                  AND trigger = 'agent_request'
                  AND created_at >= ?
                  AND created_at < ?
                """,
                (
                    _ONSPD_REFRESH_DATASOURCE_ID,
                    _timestamp(day_start),
                    _timestamp(day_end),
                ),
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        return int(row["job_count"])

    def _issue_refresh_confirmation(
        self,
        *,
        request_id: str,
        principal: str,
        request_fingerprint: str,
        datasource_id: str,
        definition_version: int,
        submitted_at: datetime,
        pending: object | None,
    ) -> DurableRefreshResult:
        if pending is not None:
            expires_at = _parse_timestamp(pending["expires_at"])  # type: ignore[index]
            if expires_at <= submitted_at:
                raise RefreshConfirmationError(
                    "refresh confirmation expired; create a new request"
                )
            return DurableRefreshResult(
                None,
                "confirmation_required",
                "confirmation_required",
                _parse_timestamp(pending["issued_at"]),  # type: ignore[index]
                pending["confirmation_token"],  # type: ignore[index]
                expires_at,
            )
        issued_at = submitted_at
        expires_at = issued_at + _REFRESH_CONFIRMATION_TTL
        token = new_id("refresh_confirmation")
        day_start, _ = _london_day_window(issued_at)
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
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
                        request_fingerprint,
                        datasource_id,
                        definition_version,
                        _timestamp(day_start),
                        _timestamp(issued_at),
                        _timestamp(expires_at),
                    ),
                )
                self._audit(
                    connection,
                    actor_type="agent",
                    actor_id=principal,
                    action="refresh_confirmation_issued",
                    target_type="refresh_confirmation",
                    target_id=request_id,
                    details={
                        "datasource_id": datasource_id,
                        "request_id": request_id,
                        "daily_limit": _ONSPD_REFRESH_DAILY_LIMIT,
                    },
                    at=_timestamp(issued_at),
                )
        finally:
            connection.close()
        return DurableRefreshResult(
            None,
            "confirmation_required",
            "confirmation_required",
            issued_at,
            token,
            expires_at,
        )

    @staticmethod
    def _validate_refresh_confirmation_identity(
        row: object,
        *,
        principal: str,
        request_fingerprint: str,
        datasource_id: str,
        definition_version: int,
    ) -> None:
        if row["principal"] != principal:  # type: ignore[index]
            raise RefreshRequestAccessError(
                "request_instance_id belongs to another principal"
            )
        if row["request_fingerprint"] != request_fingerprint:  # type: ignore[index]
            raise RefreshRequestReplayError(
                "request_instance_id was reused for a different request"
            )
        if (
            row["datasource_id"] != datasource_id  # type: ignore[index]
            or row["definition_version"] != definition_version  # type: ignore[index]
        ):
            raise RefreshRequestReplayError(
                "request_instance_id was reused for a different datasource"
            )

    @staticmethod
    def _validate_refresh_confirmation(
        row: object,
        *,
        confirmation_token: str,
        submitted_at: datetime,
    ) -> None:
        if row["confirmation_token"] != confirmation_token:  # type: ignore[index]
            raise RefreshConfirmationError("refresh confirmation is invalid")
        if _parse_timestamp(row["expires_at"]) <= submitted_at:  # type: ignore[index]
            raise RefreshConfirmationError(
                "refresh confirmation expired; create a new request"
            )

    def _refresh_job_by_request_id(self, request_id: str) -> object | None:
        connection = connect_database(self.database_path, read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT job_id, request_json FROM workflow_job
                WHERE request_instance_id = ? AND trigger = 'agent_request'
                ORDER BY created_at DESC, job_id DESC LIMIT 2
                """,
                (request_id,),
            ).fetchall()
        finally:
            connection.close()
        if len(rows) > 1:
            raise OperationalError("ambiguous durable refresh request ID")
        return rows[0] if rows else None

    def _active_refresh_dedupe(
        self, dedupe_key: str, at: datetime
    ) -> Mapping[str, str] | None:
        """Find an active refresh cohort, including a pre-ledger crash orphan.

        ``enqueue`` commits the workflow job before the append-only ledger row.
        A process can therefore die in that narrow interval.  The job's
        refresh-control metadata is the recovery authority until its original
        request is replayed, and must participate in cooldown deduplication.
        """

        connection = connect_database(self.database_path, read_only=True)
        try:
            ledger_rows = connection.execute(
                """
                SELECT job_id, initial_state, cooldown_until
                       , submitted_at
                FROM refresh_request
                WHERE dedupe_key = ? AND cooldown_until > ?
                ORDER BY submitted_at DESC, request_id DESC LIMIT 1
                """,
                (dedupe_key, _timestamp(at)),
            ).fetchall()
            orphan_rows = connection.execute(
                """
                SELECT job_id, request_json
                FROM workflow_job AS job
                WHERE trigger = 'agent_request'
                  AND NOT EXISTS (
                      SELECT 1 FROM refresh_request AS ledger
                      WHERE ledger.job_id = job.job_id
                  )
                """
            ).fetchall()
        finally:
            connection.close()

        candidates = [dict(row) for row in ledger_rows]
        for row in orphan_rows:
            try:
                request = json.loads(row["request_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise OperationalError("durable refresh request metadata is invalid") from error
            if not isinstance(request, Mapping) or "_refresh_control" not in request:
                continue
            control = _refresh_control(row["request_json"])
            if (
                control["dedupe_key"] == dedupe_key
                and _parse_timestamp(control["cooldown_until"]) > at
            ):
                candidates.append(
                    {
                        "job_id": row["job_id"],
                        "initial_state": control["initial_state"],
                        "cooldown_until": control["cooldown_until"],
                        "submitted_at": control["submitted_at"],
                    }
                )
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda candidate: (
                _parse_timestamp(candidate["submitted_at"]),
                candidate["job_id"],
            ),
        )

    @staticmethod
    def _validate_existing_refresh_request(
        row: object,
        *,
        principal: str,
        request_fingerprint: str,
    ) -> DurableRefreshResult:
        if row["principal"] != principal:  # type: ignore[index]
            raise RefreshRequestAccessError(
                "request_instance_id belongs to another principal"
            )
        if row["request_fingerprint"] != request_fingerprint:  # type: ignore[index]
            raise RefreshRequestReplayError(
                "request_instance_id was reused for a different request"
            )
        return DurableRefreshResult(
            row["job_id"],  # type: ignore[index]
            row["disposition"],  # type: ignore[index]
            row["initial_state"],  # type: ignore[index]
            _parse_timestamp(row["submitted_at"]),  # type: ignore[index]
        )

    def _record_refresh_request(
        self,
        *,
        request_id: str,
        principal: str,
        request_fingerprint: str,
        dedupe_key: str,
        datasource_id: str,
        definition_version: int,
        request_profile: str,
        job_id: str,
        disposition: str,
        initial_state: str,
        submitted_at: datetime,
        cooldown_until: datetime,
    ) -> DurableRefreshResult:
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                connection.execute(
                    """
                    INSERT INTO refresh_request (
                        request_id, principal, request_fingerprint, dedupe_key,
                        datasource_id, definition_version, request_profile, job_id,
                        disposition, initial_state, submitted_at, cooldown_until
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        principal,
                        request_fingerprint,
                        dedupe_key,
                        datasource_id,
                        definition_version,
                        request_profile,
                        job_id,
                        disposition,
                        initial_state,
                        _timestamp(submitted_at),
                        _timestamp(cooldown_until),
                    ),
                )
        finally:
            connection.close()
        return DurableRefreshResult(job_id, disposition, initial_state, submitted_at)

    @_single_writer
    def retry(self, job_id: str, *, actor_id: str = "local") -> EnqueueResult:
        """Create a new, explicitly linked retry job; attempts remain immutable."""

        connection = connect_database(self.database_path, read_only=True)
        try:
            row = connection.execute(
                """
                SELECT datasource_id, definition_version, lane, request_json, state,
                       generation, job_kind, window_start, window_end
                FROM workflow_job WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise OperationalError("unknown job")
        if row["state"] not in {"failed", "dead_letter", "cancelled"}:
            raise OperationalError("only terminal failed, dead-letter, or cancelled jobs can retry")
        if not row["datasource_id"] or not row["lane"]:
            raise OperationalError("system job retry is not supported by this API")
        result = self.enqueue(
            row["datasource_id"],
            definition_version=row["definition_version"],
            request=json.loads(row["request_json"]),
            trigger="manual",
            lane=row["lane"],
            request_instance_id=new_id("retry"),
            parent_job_id=job_id,
            generation=row["generation"] + 1,
            job_kind=row["job_kind"],
            window_start=_optional_timestamp(row["window_start"]),
            window_end=_optional_timestamp(row["window_end"]),
        )
        return result

    @_single_writer
    def cancel(self, job_id: str, actor_id: str = "local") -> bool:
        now = _timestamp()
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                changed = connection.execute(
                    """
                    UPDATE workflow_job
                    SET state = 'cancelled', completed_at = ?, cancel_requested_at = ?,
                        cancel_requested_by = ?
                    WHERE job_id = ? AND state IN ('queued', 'retry_wait', 'claimed')
                    """,
                    (now, now, actor_id, job_id),
                ).rowcount
                if changed:
                    self._audit(
                        connection,
                        actor_type="operator",
                        actor_id=actor_id,
                        action="job_cancelled",
                        target_type="workflow_job",
                        target_id=job_id,
                        details={},
                        at=now,
                    )
                return bool(changed)
        finally:
            connection.close()

    @_single_writer
    def recover_expired(self, *, now: datetime | None = None) -> tuple[str, ...]:
        """Reconcile abandoned claims/attempts before another worker can run.

        A recovered running attempt is terminally failed first; its logical job
        then moves to a bounded retry or dead letter state.  The original claim
        token is cleared, so a late child cannot subsequently commit evidence.
        """

        timestamp = _timestamp(now)
        connection = connect_database(self.database_path)
        recovered: list[str] = []
        try:
            with transaction(connection):
                rows = connection.execute(
                    """
                    SELECT j.job_id, j.state, j.attempt_count, j.max_attempts,
                           j.datasource_id, j.definition_version,
                           COALESCE(d.retry_policy_json, '{}') AS retry_policy_json,
                           j.claim_token
                    FROM workflow_job AS j
                    LEFT JOIN datasource_definition AS d
                      ON d.datasource_id = j.datasource_id
                     AND d.definition_version = j.definition_version
                    WHERE j.state IN ('claimed', 'running')
                      AND j.lease_expires_at IS NOT NULL
                      AND j.lease_expires_at <= ?
                    """,
                    (timestamp,),
                ).fetchall()
                for row in rows:
                    error = {
                        "schema_version": "error.v1",
                        "code": "WORKER_LEASE_EXPIRED",
                        "stage": "worker",
                        "retryable": row["state"] == "running",
                    }
                    if row["state"] == "claimed":
                        next_state = "queued"
                        available_at = timestamp
                    else:
                        attempt = connection.execute(
                            """
                            SELECT attempt_id FROM workflow_attempt
                            WHERE job_id = ? AND status = 'running'
                            ORDER BY attempt_no DESC LIMIT 1
                            """,
                            (row["job_id"],),
                        ).fetchone()
                        if attempt is not None:
                            connection.execute(
                                """
                                UPDATE workflow_attempt
                                SET status = 'failed', completed_at = ?, heartbeat_at = ?, error_json = ?
                                WHERE attempt_id = ? AND status = 'running'
                                """,
                                (timestamp, timestamp, _json(error), attempt["attempt_id"]),
                            )
                            connection.execute(
                                """
                                UPDATE ingestion_run SET status = 'failed', completed_at = ?, retrieved_at = ?
                                WHERE attempt_id = ? AND status = 'running'
                                """,
                                (timestamp, timestamp, attempt["attempt_id"]),
                            )
                        if row["attempt_count"] < row["max_attempts"]:
                            next_state = "retry_wait"
                            available_at = _retry_timestamp(
                                timestamp, row["retry_policy_json"], row["attempt_count"]
                            )
                        else:
                            next_state = "dead_letter"
                            available_at = timestamp
                    connection.execute(
                        """
                        UPDATE workflow_job
                        SET state = ?, available_at = ?,
                            completed_at = CASE WHEN ? = 'dead_letter' THEN ? ELSE NULL END,
                            claim_token = NULL, claimed_by = NULL, claimed_at = NULL,
                            lease_expires_at = NULL, heartbeat_at = NULL, last_error_json = ?
                        WHERE job_id = ? AND claim_token = ?
                        """,
                        (
                            next_state,
                            available_at,
                            next_state,
                            timestamp,
                            _json(error),
                            row["job_id"],
                            row["claim_token"],
                        ),
                    )
                    self._audit(
                        connection,
                        actor_type="service",
                        actor_id="reconciler",
                        action="lease_recovered",
                        target_type="workflow_job",
                        target_id=row["job_id"],
                        details={"state": row["state"], "next_state": next_state},
                        at=timestamp,
                    )
                    recovered.append(row["job_id"])
        finally:
            connection.close()
        return tuple(recovered)

    @_single_writer
    def scheduler_tick(self, *, now: datetime | None = None) -> dict[str, object]:
        """Materialize due registry schedules into idempotent durable jobs.

        The tick is deliberately finite and accepts an injected clock.  It is
        safe for tests, recovery runs, and a daemon loop without real sleeps.
        Definitions without executable bindings remain visible as blocked work
        instead of creating jobs that could masquerade as production coverage.
        """

        anchor = _as_utc(now)
        self.sync_registry(now=anchor)
        recovered = self.recover_expired(now=anchor)
        connection = connect_database(self.database_path, read_only=True)
        try:
            schedules = connection.execute(
                """
                SELECT schedule_id, datasource_id, definition_version, lane,
                       rule_json, timezone, catchup_policy, max_catchup_jobs,
                       max_catchup_horizon_seconds, cursor_at, created_at
                FROM workflow_schedule
                WHERE enabled = 1 AND paused_reason IS NULL
                ORDER BY schedule_id
                """
            ).fetchall()
        finally:
            connection.close()

        materialized: list[dict[str, object]] = []
        blocked: list[dict[str, object]] = []
        errors: list[dict[str, object]] = []
        for schedule in schedules:
            definition = self.registry.lookup(
                schedule["datasource_id"], schedule["definition_version"]
            )
            validation = self.runtime_bindings.validate(definition, operation="ingest")
            if not validation.ready:
                blocked.append(
                    {
                        "schedule_id": schedule["schedule_id"],
                        "datasource_id": definition.datasource_id,
                        "reason": "runtime_unbound",
                    }
                )
                continue
            try:
                recurrence = recurrence_from_rule(
                    json.loads(schedule["rule_json"]),
                    timezone=schedule["timezone"],
                    anchor_at=_parse_timestamp(schedule["created_at"]),
                )
                cursor = (
                    _parse_timestamp(schedule["cursor_at"])
                    if schedule["cursor_at"]
                    else _parse_timestamp(schedule["created_at"])
                )
                horizon_seconds = schedule["max_catchup_horizon_seconds"]
                horizon = (
                    timedelta(seconds=horizon_seconds) if horizon_seconds else None
                )
                due = materialize_due_slots(
                    recurrence,
                    cursor_at=cursor,
                    now=anchor,
                    catchup_policy=CatchupPolicy(schedule["catchup_policy"]),
                    max_catchup_jobs=schedule["max_catchup_jobs"],
                    max_catchup_horizon=horizon,
                )
            except (JobError, ValueError, TypeError) as error:
                errors.append(
                    {
                        "schedule_id": schedule["schedule_id"],
                        "code": "SCHEDULE_INVALID",
                        "message": type(error).__name__,
                    }
                )
                self._pause_schedule(schedule["schedule_id"], "SCHEDULE_INVALID", anchor)
                continue

            accepted = 0
            deduplicated = 0
            try:
                for slot in due.slots:
                    request = _thaw(definition.default_request)
                    request["scheduled_slot"] = _timestamp(slot)
                    result = self.enqueue(
                        definition.datasource_id,
                        request=request,
                        trigger="schedule",
                        lane=schedule["lane"],
                        scheduled_for=slot,
                        schedule_id=schedule["schedule_id"],
                    )
                    if result.disposition == "accepted":
                        accepted += 1
                    else:
                        deduplicated += 1
            except OperationalError as error:
                errors.append(
                    {
                        "schedule_id": schedule["schedule_id"],
                        "code": "SCHEDULE_ENQUEUE_FAILED",
                        "message": type(error).__name__,
                    }
                )
                continue
            next_cursor = due.next_cursor_at or cursor
            self._update_schedule_cursor(
                schedule["schedule_id"],
                cursor_at=next_cursor,
                next_due_at=recurrence.next_after(next_cursor),
                now=anchor,
            )
            materialized.append(
                {
                    "schedule_id": schedule["schedule_id"],
                    "datasource_id": definition.datasource_id,
                    "accepted": accepted,
                    "deduplicated": deduplicated,
                    "skipped_slots": due.skipped_slots,
                }
            )
        return {
            "schema_version": "scheduler_tick.v1",
            "at": _timestamp(anchor),
            "recovered_job_ids": list(recovered),
            "schedules": materialized,
            "blocked": blocked,
            "errors": errors,
        }

    def _pause_schedule(self, schedule_id: str, reason: str, now: datetime) -> None:
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                connection.execute(
                    """
                    UPDATE workflow_schedule
                    SET paused_reason = ?, updated_at = ? WHERE schedule_id = ?
                    """,
                    (reason, _timestamp(now), schedule_id),
                )
        finally:
            connection.close()

    def _update_schedule_cursor(
        self,
        schedule_id: str,
        *,
        cursor_at: datetime,
        next_due_at: datetime,
        now: datetime,
    ) -> None:
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                connection.execute(
                    """
                    UPDATE workflow_schedule
                    SET cursor_at = ?, next_due_at = ?, paused_reason = NULL, updated_at = ?
                    WHERE schedule_id = ?
                    """,
                    (
                        _timestamp(cursor_at),
                        _timestamp(next_due_at),
                        _timestamp(now),
                        schedule_id,
                    ),
                )
        finally:
            connection.close()

    def health(self) -> dict[str, object]:
        runtime = self.registry.runtime_status(self.runtime_bindings)
        unbound = sorted(
            identifier for identifier, validation in runtime.items() if not validation.ready
        )
        production_unbound = sorted(
            identifier
            for identifier, validation in runtime.items()
            if not validation.ready
            and validation.descriptor.status == "production"
            and validation.descriptor.automation_mode in {"automatic", "on_demand"}
        )
        parser_isolation = parser_isolation_status()
        operational_ingestion_ready = bool(parser_isolation["available"]) and not production_unbound
        operational_ingestion_reason = (
            parser_isolation["reason"]
            if not parser_isolation["available"]
            else "RUNTIME_BINDINGS_UNBOUND"
            if production_unbound
            else None
        )
        blocked = sorted(
            f"{definition.datasource_id}@{definition.definition_version}"
            for definition in self.registry.definitions
            if definition.status != "production"
        )
        if not self._is_initialized():
            evidence = self.verify_evidence()
            return {
                "schema_version": "health.v1",
                "state": "uninitialized",
                "integrity_ok": False,
                "evidence_ok": False,
                "jobs": {},
                "definitions": {},
                "runtime_unbound": unbound,
                "production_runtime_unbound": production_unbound,
                "parser_isolation": parser_isolation,
                "operational_ingestion": {
                    "ready": operational_ingestion_ready,
                    "reason": operational_ingestion_reason,
                },
                "policy_blocked": blocked,
                "unreferenced_artifacts": evidence["unreferenced"],
            }
        report = integrity_check(self.database_path)
        connection = connect_database(self.database_path, read_only=True)
        try:
            jobs = connection.execute(
                "SELECT state, count(*) AS count FROM workflow_job GROUP BY state"
            ).fetchall()
            definitions = connection.execute(
                "SELECT status, count(*) AS count FROM datasource_definition GROUP BY status"
            ).fetchall()
        finally:
            connection.close()
        evidence = self.verify_evidence()
        return {
            "schema_version": "health.v1",
            "state": "ready",
            "integrity_ok": report.ok,
            "evidence_ok": evidence["ok"],
            "jobs": {row["state"]: row["count"] for row in jobs},
            "definitions": {row["status"]: row["count"] for row in definitions},
            "runtime_unbound": unbound,
            "production_runtime_unbound": production_unbound,
            "parser_isolation": parser_isolation,
            "operational_ingestion": {
                "ready": operational_ingestion_ready,
                "reason": operational_ingestion_reason,
            },
            "policy_blocked": blocked,
            "unreferenced_artifacts": evidence["unreferenced"],
        }

    def metrics(self) -> dict[str, object]:
        """Return bounded local operational counters without exposing raw data."""

        if not self._is_initialized():
            return {
                "schema_version": "metrics.v1",
                "state": "uninitialized",
                "database_bytes": 0,
                "evidence_bytes": 0,
                "evidence_count": 0,
                "jobs": {},
                "open_alerts": {},
                "last_verified_backup_at": None,
            }
        connection = connect_database(self.database_path, read_only=True)
        try:
            job_rows = connection.execute(
                "SELECT state, count(*) AS count FROM workflow_job GROUP BY state"
            ).fetchall()
            evidence = connection.execute(
                "SELECT count(*) AS count, COALESCE(sum(byte_size), 0) AS bytes FROM content_object"
            ).fetchone()
            backup = connection.execute(
                "SELECT max(verified_at) AS last_verified_at FROM backup_set"
            ).fetchone()
            alerts = connection.execute(
                "SELECT severity, count(*) AS count FROM operational_alert WHERE state = 'open' GROUP BY severity"
            ).fetchall()
        finally:
            connection.close()
        database_bytes = self.database_path.stat().st_size if self.database_path.exists() else 0
        return {
            "schema_version": "metrics.v1",
            "state": "ready",
            "database_bytes": database_bytes,
            "evidence_bytes": evidence["bytes"],
            "evidence_count": evidence["count"],
            "jobs": {row["state"]: row["count"] for row in job_rows},
            "open_alerts": {row["severity"]: row["count"] for row in alerts},
            "last_verified_backup_at": backup["last_verified_at"],
        }

    @_single_writer
    def update_service_heartbeat(
        self,
        *,
        instance_id: str,
        role: str,
        state: str,
        lease_seconds: int = 180,
        now: datetime | None = None,
    ) -> None:
        """Persist one daemon/worker heartbeat used by health and recovery checks."""

        if role not in {"daemon", "worker"} or state not in {"starting", "running", "stopping", "failed"}:
            raise OperationalError("invalid service heartbeat role or state")
        if not instance_id:
            raise OperationalError("service instance_id is required")
        anchor = _as_utc(now)
        timestamp = _timestamp(anchor)
        expires = _timestamp(
            anchor + timedelta(seconds=_lease_seconds(lease_seconds))
        )
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                connection.execute(
                    """
                    INSERT INTO service_heartbeat (
                        instance_id, role, app_version, state, started_at, heartbeat_at,
                        lease_expires_at, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}')
                    ON CONFLICT(instance_id) DO UPDATE SET
                        role = excluded.role, app_version = excluded.app_version,
                        state = excluded.state, heartbeat_at = excluded.heartbeat_at,
                        lease_expires_at = excluded.lease_expires_at
                    """,
                    (
                        instance_id,
                        role,
                        self.app_version,
                        state,
                        timestamp,
                        timestamp,
                        expires,
                    ),
                )
        finally:
            connection.close()

    @_single_writer
    def backup(self, target: str | Path) -> Path:
        self.migrate()
        return backup_database(self.database_path, target)

    @_single_writer
    def rebuild_projections(self) -> object:
        """Rebuild derived SQLite projections under the store writer lease."""

        from nan_fung.projections.rebuild import rebuild_sqlite_projections

        return rebuild_sqlite_projections(self.database_path, _writer_locked=True)

    def publish_projections(
        self,
        output_directory: str | Path,
        *,
        as_of_at: datetime,
        alert_rules: Iterable[object] = (),
        actor_id: str = "operator",
    ) -> object:
        """Publish fixed canonical projections, then append their durable lineage.

        Filesystem publication is atomic per file and its report carries the
        canonical-input hash. A separate short SQLite transaction records that
        immutable report and its threshold alerts; a crash between them is
        safely replayable because both identifiers are content-derived.
        """

        from nan_fung.projections.delivery import deliver_canonical_projections

        with self.writer_session():
            self.migrate()
            report = deliver_canonical_projections(
                self.database_path,
                output_directory,
                as_of_at=_as_utc(as_of_at),
                alert_rules=tuple(alert_rules),
                _writer_locked=True,
            )
            self.record_projection_delivery(report, actor_id=actor_id)
            return report

    @_single_writer
    def record_projection_delivery(self, report: object, *, actor_id: str) -> dict[str, int]:
        """Persist content-addressed output and alert lineage from a trusted report."""

        if not actor_id:
            raise OperationalError("projection delivery actor is required")
        output_root = Path(getattr(report, "output_directory", "")).expanduser().resolve()
        artifacts = tuple(getattr(report, "artifacts", ()))
        delivery_id = getattr(report, "delivery_id", None)
        as_of_at = getattr(report, "as_of_at", None)
        audit_path = Path(getattr(report, "audit_path", "")).expanduser().resolve()
        audit_hash = getattr(report, "audit_content_sha256", None)
        audit_source_hash = getattr(report, "audit_source_hash", None)
        if (
            not isinstance(delivery_id, str)
            or not isinstance(as_of_at, datetime)
            or not isinstance(audit_hash, str)
            or not isinstance(audit_source_hash, str)
            or not output_root.is_dir()
        ):
            raise OperationalError("invalid projection delivery report")
        validated: list[tuple[str, Path, str, str, Mapping[str, object]]] = []
        for artifact in artifacts:
            artifact_type = getattr(artifact, "artifact_type", None)
            path = Path(getattr(artifact, "path", "")).expanduser().resolve()
            content_hash = getattr(artifact, "content_sha256", None)
            source_hash = getattr(artifact, "source_hash", None)
            details = getattr(artifact, "details", None)
            if (
                not isinstance(artifact_type, str)
                or not isinstance(content_hash, str)
                or not isinstance(source_hash, str)
                or not isinstance(details, Mapping)
            ):
                raise OperationalError("invalid projection delivery artifact")
            _validate_output_file(path, output_root, content_hash)
            validated.append((artifact_type, path, content_hash, source_hash, details))
        _validate_output_file(audit_path, output_root, audit_hash)
        now = _timestamp()
        outputs_written = 0
        alerts_written = 0
        connection = connect_database(self.database_path)
        try:
            with transaction(connection):
                for artifact_type, path, content_hash, source_hash, details in validated:
                    output_id = "out_" + request_hash(
                        {
                            "delivery_id": delivery_id,
                            "type": artifact_type,
                            "path": str(path),
                            "content_sha256": content_hash,
                        }
                    )
                    changed = connection.execute(
                        """
                        INSERT OR IGNORE INTO output_artifact (
                            output_id, output_type, path, source_hash, details_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            output_id,
                            f"projection_{artifact_type}",
                            str(path),
                            source_hash,
                            _json(
                                {
                                    "schema_version": "projection_output.v1",
                                    "delivery_id": delivery_id,
                                    "as_of_at": _timestamp(_as_utc(as_of_at)),
                                    "content_sha256": content_hash,
                                    **dict(details),
                                }
                            ),
                            now,
                        ),
                    ).rowcount
                    outputs_written += changed
                    if artifact_type == "alerts":
                        alerts_written += _record_delivered_alerts(connection, path, now)
                audit_id = "out_" + request_hash(
                    {
                        "delivery_id": delivery_id,
                        "type": "audit",
                        "path": str(audit_path),
                        "content_sha256": audit_hash,
                    }
                )
                changed = connection.execute(
                    """
                    INSERT OR IGNORE INTO output_artifact (
                        output_id, output_type, path, source_hash, details_json, created_at
                    ) VALUES (?, 'projection_audit', ?, ?, ?, ?)
                    """,
                    (
                        audit_id,
                        str(audit_path),
                        audit_source_hash,
                        _json(
                            {
                                "schema_version": "projection_output.v1",
                                "delivery_id": delivery_id,
                                "as_of_at": _timestamp(_as_utc(as_of_at)),
                                "content_sha256": audit_hash,
                            }
                        ),
                        now,
                    ),
                ).rowcount
                outputs_written += changed
                if outputs_written or alerts_written:
                    self._audit(
                        connection,
                        actor_type="operator",
                        actor_id=actor_id,
                        action="projection_delivery_recorded",
                        target_type="projection_delivery",
                        target_id=delivery_id,
                        details={
                            "output_count": outputs_written,
                            "alert_count": alerts_written,
                            "audit_source_hash": audit_source_hash,
                        },
                        at=now,
                    )
        finally:
            connection.close()
        return {"outputs_written": outputs_written, "alerts_written": alerts_written}

    def verify_evidence(self) -> dict[str, object]:
        if not self._is_initialized():
            return {
                "checked": 0,
                "missing_or_corrupt": [],
                "unreferenced": list(self.artifacts.published_digests()),
                "ok": True,
            }
        connection = connect_database(self.database_path, read_only=True)
        try:
            rows = connection.execute(
                "SELECT content_sha256 FROM content_object ORDER BY content_sha256"
            ).fetchall()
        finally:
            connection.close()
        missing = [
            row["content_sha256"]
            for row in rows
            if not self.artifacts.verify(row["content_sha256"])
        ]
        referenced = {row["content_sha256"] for row in rows}
        unreferenced = sorted(set(self.artifacts.published_digests()) - referenced)
        return {
            "checked": len(rows),
            "missing_or_corrupt": missing,
            "unreferenced": unreferenced,
            "ok": not missing,
        }

    def retention_dry_run(self, *, as_of: datetime | None = None) -> dict[str, object]:
        """List eligible evidence without deleting data or mutating the CAS."""

        anchor = _timestamp(as_of)
        if not self._is_initialized():
            return {
                "schema_version": "retention_dry_run.v1",
                "as_of": anchor,
                "eligible": [],
                "count": 0,
            }
        connection = connect_database(self.database_path, read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT evidence_id, content_sha256, access_class, retention_until
                FROM evidence_artifact
                WHERE retention_until IS NOT NULL AND retention_until <= ?
                ORDER BY retention_until, evidence_id
                """,
                (anchor,),
            ).fetchall()
        finally:
            connection.close()
        return {
            "schema_version": "retention_dry_run.v1",
            "as_of": anchor,
            "eligible": [dict(row) for row in rows],
            "count": len(rows),
        }

    def _insert_source(self, connection: object, source: object, now: str) -> bool:
        source_json = source.as_json()  # type: ignore[attr-defined]
        existing = connection.execute(  # type: ignore[attr-defined]
            "SELECT source_hash FROM source_definition WHERE source_id = ? AND source_version = ?",
            (source.source_id, source.source_version),  # type: ignore[attr-defined]
        ).fetchone()
        if existing is not None:
            if existing["source_hash"] != source.source_hash:  # type: ignore[attr-defined]
                raise OperationalError("immutable source definition conflicts with database")
            return False
        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO source_definition (
                source_id, source_version, source_hash, display_name, publisher,
                surface_kind, base_origin_redacted, allowed_hosts_json, licence,
                access_class, retention_profile, source_json, status, approved_by,
                approved_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.source_id, source.source_version, source.source_hash,  # type: ignore[attr-defined]
                source.display_name, source.publisher, source.surface_kind,  # type: ignore[attr-defined]
                source.base_origin_redacted, _json(list(source.allowed_hosts)),  # type: ignore[attr-defined]
                source.licence, source.access_class, source.retention_profile,  # type: ignore[attr-defined]
                _json(source_json), source.status, source.approved_by,  # type: ignore[attr-defined]
                source.approved_at, now,  # type: ignore[attr-defined]
            ),
        )
        return True

    def _insert_definition(
        self, connection: object, definition: DatasourceDefinitionDescriptor, now: str
    ) -> bool:
        definition_json = definition.as_json()
        existing = connection.execute(  # type: ignore[attr-defined]
            "SELECT definition_hash FROM datasource_definition WHERE datasource_id = ? AND definition_version = ?",
            (definition.datasource_id, definition.definition_version),
        ).fetchone()
        if existing is not None:
            if existing["definition_hash"] != definition.definition_hash:
                raise OperationalError("immutable datasource definition conflicts with database")
            return False
        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO datasource_definition (
                datasource_id, definition_version, definition_hash, display_name,
                publisher, category, source_kind, automation_mode, snapshot_mode,
                default_lane, promotion_policy, data_kind, default_confidence,
                collector_name, collector_version, parser_name, parser_version,
                schema_version, record_key_builder_name, record_key_version,
                locator_version, allowed_hosts_json, validation_policy_json,
                retry_policy_json, timeout_policy_json, artifact_policy_json,
                freshness_policy_json, capabilities_json, licence, access_class,
                retention_policy, definition_json, status, approved_by, approved_at,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                definition.datasource_id,
                definition.definition_version,
                definition.definition_hash,
                definition.display_name,
                definition.publisher,
                definition.category,
                definition.source_kind,
                definition.automation_mode,
                definition.snapshot_mode,
                definition.default_lane,
                definition.promotion_policy,
                definition.data_kind,
                definition.default_confidence,
                definition.collector_name,
                definition.collector_version,
                definition.parser_name,
                definition.parser_version,
                definition.schema_version,
                definition.record_key_builder_name,
                definition.record_key_version,
                definition.locator_version,
                _json(list(definition.allowed_hosts)),
                _json(_thaw(definition.validation_policy)),
                _json(_thaw(definition.retry_policy)),
                _json(_thaw(definition.timeout_policy)),
                _json(_thaw(definition.artifact_policy)),
                _json(_thaw(definition.freshness_policy)),
                _json(_thaw(definition.capabilities)),
                definition.licence,
                definition.access_class,
                definition.retention_policy,
                _json(definition_json),
                definition.status,
                definition.approved_by,
                definition.approved_at,
                now,
            ),
        )
        return True

    @staticmethod
    def _audit(
        connection: object,
        *,
        actor_type: str,
        actor_id: str,
        action: str,
        target_type: str,
        target_id: str,
        details: Mapping[str, Any],
        at: str,
    ) -> None:
        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO audit_event (
                audit_id, actor_type, actor_id, action, target_type, target_id,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("audit"), actor_type, actor_id, action, target_type, target_id, _json(details), at),
        )

    @staticmethod
    def _require_running_run(
        connection: object, run: RunHandle, *, at: str
    ) -> object:
        row = connection.execute(  # type: ignore[attr-defined]
            """
            SELECT j.job_id, j.attempt_count, j.max_attempts, d.retry_policy_json
            FROM workflow_job AS j
            JOIN datasource_definition AS d
              ON d.datasource_id = j.datasource_id
             AND d.definition_version = j.definition_version
            JOIN workflow_attempt AS a
              ON a.attempt_id = ? AND a.job_id = j.job_id AND a.status = 'running'
            WHERE j.job_id = ? AND j.claim_token = ? AND j.state = 'running'
              AND j.lease_expires_at > ?
            """,
            (run.attempt_id, run.job_id, run.claim_token, at),
        ).fetchone()
        if row is None:
            raise OperationalError("run is not owned by an active claim")
        return row

    @staticmethod
    def _insert_promotion(connection: object, run_id: str, at: str) -> None:
        sequence = connection.execute(  # type: ignore[attr-defined]
            "SELECT COALESCE(MAX(promotion_seq), 0) + 1 FROM run_promotion"
        ).fetchone()[0]
        connection.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO run_promotion (
                promotion_id, promotion_seq, run_id, decision, approval_mode,
                decision_at, actor_type, actor_id, policy_version, details_json
            ) VALUES (?, ?, ?, 'approved', 'automatic', ?, 'service', 'daemon', 'v1', '{}')
            """,
            (new_id("promotion"), sequence, run_id, at),
        )

class HostThrottleGate:
    """Adapt :class:`OperationalStore` state to the HTTP acquisition seam."""

    def __init__(
        self,
        store: OperationalStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def permit(self, host: str, *, continuation: bool = False) -> None:
        self._store.permit_host(
            host, continuation=continuation, now=self._now()
        )

    def record_response(
        self, host: str, *, status: int, retry_after: str | None
    ) -> datetime | None:
        state = self._store.record_host_response(
            host,
            status=status,
            retry_after=retry_after,
            now=self._now(),
        )
        return state.blocked_until

    def _now(self) -> datetime:
        return _as_utc(self._clock())


def _timestamp(value: datetime | None = None) -> str:
    return _as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _as_utc(value: datetime | None = None) -> datetime:
    candidate = value or datetime.now(UTC)
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise OperationalError("timestamps must be timezone-aware")
    return candidate.astimezone(UTC)


def _london_day_window(value: datetime) -> tuple[datetime, datetime]:
    """Return the UTC bounds for the London calendar day containing ``value``."""

    local = _as_utc(value).astimezone(_ONSPD_REFRESH_TIMEZONE)
    day_start = datetime.combine(local.date(), time.min, tzinfo=_ONSPD_REFRESH_TIMEZONE)
    return day_start.astimezone(UTC), (day_start + timedelta(days=1)).astimezone(UTC)


def _rate_limit_group(host: str) -> str:
    group = host.strip().rstrip(".").lower() if isinstance(host, str) else ""
    if not group or any(character.isspace() or character in "/:@?#[\\]" for character in group):
        raise OperationalError("host throttle requires a bare hostname")
    return group


def _optional_timestamp(value: str | None) -> datetime | None:
    return _parse_timestamp(value) if value is not None else None


def _latest_timestamp(*values: datetime | None) -> datetime | None:
    candidates = tuple(value for value in values if value is not None)
    return max(candidates) if candidates else None


def _retry_after_deadline(value: str | None, *, now: datetime) -> datetime | None:
    if not isinstance(value, str) or not (candidate := value.strip()):
        return None
    if candidate.isdecimal():
        try:
            return now + timedelta(seconds=int(candidate))
        except OverflowError:
            return None
    try:
        parsed = parsedate_to_datetime(candidate)
    except (IndexError, TypeError, ValueError):
        return None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return max(_as_utc(parsed), now)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise OperationalError("invalid persisted timestamp") from error
    return _as_utc(parsed)


def _retention_deadline(
    policy: str,
    *,
    retrieved_at: datetime,
    requested_until: datetime | None,
) -> datetime | None:
    """Apply the versioned retention basis without inventing a duration.

    Open official and internal configuration evidence have the explicit
    project-lifetime basis.  Restricted, reference-only, per-artifact, and
    unapproved sources require a caller-supplied deadline that represents a
    separately approved retention decision; a generic fallback would falsely
    claim rights that are not in the registry.
    """

    if policy in {"open_official", "internal_config"}:
        if requested_until is not None:
            raise OperationalError("project-lifetime evidence cannot set a retention deadline")
        return None
    if requested_until is None:
        raise OperationalError("source retention policy requires an approved retention deadline")
    deadline = _as_utc(requested_until)
    if deadline <= retrieved_at:
        raise OperationalError("retention deadline must be after evidence retrieval")
    return deadline


def _artifact_policy_for(definition: DatasourceDefinitionDescriptor) -> ArtifactPolicy:
    """Construct the executable artifact bounds frozen into a definition."""

    raw = _thaw(definition.artifact_policy)
    allowed_keys = {
        "max_bytes",
        "allowed_media_types",
        "max_archive_members",
        "max_expanded_bytes",
        "max_compression_ratio",
    }
    values = {key: value for key, value in raw.items() if key in allowed_keys}
    if "allowed_media_types" in values:
        values["allowed_media_types"] = tuple(values["allowed_media_types"])
    try:
        return ArtifactPolicy(**values)
    except (TypeError, ValueError) as error:
        raise OperationalError("invalid frozen artifact policy") from error


def _lease_seconds(value: int) -> int:
    if not isinstance(value, int) or not 1 <= value <= 3_600:
        raise OperationalError("lease_seconds must be an integer in [1, 3600]")
    return value


def _retry_timestamp(now: str, policy_json: str, attempt_no: int) -> str:
    """Return deterministic bounded exponential retry time from persisted policy."""

    try:
        policy = json.loads(policy_json)
    except (TypeError, json.JSONDecodeError):
        policy = {}
    base = policy.get("base_delay_seconds", 60) if isinstance(policy, dict) else 60
    maximum = policy.get("max_delay_seconds", 3_600) if isinstance(policy, dict) else 3_600
    if not isinstance(base, int) or base < 0:
        base = 60
    if not isinstance(maximum, int) or maximum < base:
        maximum = max(base, 3_600)
    exponent = max(0, min(int(attempt_no) - 1, 16))
    delay = min(maximum, base * (2**exponent))
    return _timestamp(_parse_timestamp(now) + timedelta(seconds=delay))


def _json(value: Any) -> str:
    return canonical_json(value)


def _thaw(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) if isinstance(item, Mapping) else item for key, item in value.items()}
    return dict(value) if value else {}


def _require_agent_approval_identifier(name: str, value: object) -> None:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise RefreshApprovalError(
            f"{name} must be a non-empty string of at most 256 characters"
        )


def _agent_refresh_snapshot(
    *,
    datasource_id: object,
    request_profile: object,
    bounded_scope: Mapping[str, object],
    intent: object,
) -> dict[str, object]:
    """Build the canonical JSON snapshot shared by approval and broker replay."""

    _require_agent_approval_identifier("datasource_id", datasource_id)
    _require_agent_approval_identifier("request_profile", request_profile)
    if not isinstance(intent, str) or not intent or len(intent) > 240:
        raise RefreshApprovalError("approval intent must be between 1 and 240 characters")
    if not isinstance(bounded_scope, Mapping) or len(bounded_scope) > 100:
        raise RefreshApprovalError("approval scope must be a bounded mapping")
    normalized_scope: dict[str, list[str]] = {}
    total_values = 0
    for key, raw_values in bounded_scope.items():
        _require_agent_approval_identifier("approval scope key", key)
        if isinstance(raw_values, str):
            values = (raw_values,)
        elif isinstance(raw_values, (tuple, list)) and all(
            isinstance(value, str) for value in raw_values
        ):
            values = tuple(raw_values)
        else:
            raise RefreshApprovalError("approval scope values must contain strings")
        if not values or len(values) > 100:
            raise RefreshApprovalError("approval scope values are invalid")
        if any(not value or len(value) > 256 for value in values):
            raise RefreshApprovalError("approval scope values are invalid")
        total_values += len(values)
        if total_values > 100:
            raise RefreshApprovalError("approval scope has too many values")
        normalized_scope[key] = list(values)
    return {
        "datasource_id": datasource_id,
        "request_profile": request_profile,
        "bounded_scope": {
            key: normalized_scope[key] for key in sorted(normalized_scope)
        },
        "intent": intent,
    }


def _agent_refresh_snapshot_fingerprint(snapshot: Mapping[str, object]) -> str:
    return sha256(_json(snapshot).encode("utf-8")).hexdigest()


def _freeze_agent_refresh_snapshot(snapshot: Mapping[str, object]) -> Mapping[str, object]:
    scope = snapshot["bounded_scope"]
    if not isinstance(scope, Mapping):
        raise RefreshApprovalError("approval snapshot scope is invalid")
    return MappingProxyType(
        {
            "datasource_id": snapshot["datasource_id"],
            "request_profile": snapshot["request_profile"],
            "bounded_scope": MappingProxyType(
                {
                    key: tuple(values)
                    for key, values in scope.items()
                    if isinstance(key, str) and isinstance(values, list)
                }
            ),
            "intent": snapshot["intent"],
        }
    )


def _agent_refresh_approval_from_row(row: object) -> AgentRefreshApproval:
    try:
        raw_snapshot = json.loads(row["request_snapshot_json"])  # type: ignore[index]
        if not isinstance(raw_snapshot, Mapping) or set(raw_snapshot) != {
            "datasource_id",
            "request_profile",
            "bounded_scope",
            "intent",
        }:
            raise RefreshApprovalError("durable approval snapshot is invalid")
        snapshot = _agent_refresh_snapshot(
            datasource_id=raw_snapshot["datasource_id"],
            request_profile=raw_snapshot["request_profile"],
            bounded_scope=raw_snapshot["bounded_scope"],
            intent=raw_snapshot["intent"],
        )
        approval = AgentRefreshApproval(
            approval_id=row["approval_id"],  # type: ignore[index]
            refresh_request_id=row["refresh_request_id"],  # type: ignore[index]
            principal=row["principal"],  # type: ignore[index]
            capability_scope_id=row["capability_scope_id"],  # type: ignore[index]
            capability_id=row["capability_id"],  # type: ignore[index]
            manifest_version=row["manifest_version"],  # type: ignore[index]
            profile_version=row["profile_version"],  # type: ignore[index]
            request_fingerprint=row["request_fingerprint"],  # type: ignore[index]
            snapshot=_freeze_agent_refresh_snapshot(snapshot),
            issued_at=_parse_timestamp(row["issued_at"]),  # type: ignore[index]
            expires_at=_parse_timestamp(row["expires_at"]),  # type: ignore[index]
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, RefreshApprovalError):
            raise
        raise RefreshApprovalError("durable approval metadata is invalid") from error
    _require_agent_approval_identifier("approval_id", approval.approval_id)
    _require_refresh_identifier("refresh_request_id", approval.refresh_request_id)
    _require_refresh_identifier("principal", approval.principal)
    _require_agent_approval_identifier("capability_scope_id", approval.capability_scope_id)
    _require_agent_approval_identifier("capability_id", approval.capability_id)
    _require_agent_approval_identifier("manifest_version", approval.manifest_version)
    _require_agent_approval_identifier("profile_version", approval.profile_version)
    _require_refresh_digest("request_fingerprint", approval.request_fingerprint)
    if _agent_refresh_snapshot_fingerprint(snapshot) != approval.request_fingerprint:
        raise RefreshApprovalReplayError("durable approval snapshot fingerprint is invalid")
    return approval


def _validate_agent_refresh_approval(
    approval: AgentRefreshApproval,
    *,
    principal: str,
    capability_scope_id: str,
    capability_id: str,
    manifest_version: str,
    profile_version: str,
    request_fingerprint: str,
    at: datetime,
) -> None:
    if (
        approval.principal != principal
        or approval.capability_scope_id != capability_scope_id
    ):
        raise RefreshApprovalAccessError("approval is not available to this host context")
    if (
        approval.capability_id != capability_id
        or approval.manifest_version != manifest_version
        or approval.profile_version != profile_version
        or approval.request_fingerprint != request_fingerprint
    ):
        raise RefreshApprovalReplayError("approval policy or request identity changed")
    if approval.expires_at <= at:
        raise RefreshApprovalExpiredError("approval expired; create a new refresh request")


def _validate_approval_confirmation(
    row: object,
    *,
    principal: str,
    request_fingerprint: str,
    datasource_id: object,
    at: datetime,
    principal_key: str = "principal",
    fingerprint_key: str = "request_fingerprint",
    datasource_key: str = "datasource_id",
    expires_key: str = "expires_at",
) -> None:
    try:
        same_identity = (
            row[principal_key] == principal  # type: ignore[index]
            and row[fingerprint_key] == request_fingerprint  # type: ignore[index]
            and row[datasource_key] == datasource_id  # type: ignore[index]
        )
        expires_at = _parse_timestamp(row[expires_key])  # type: ignore[index]
    except (KeyError, TypeError, ValueError) as error:
        raise RefreshApprovalError("refresh confirmation metadata is invalid") from error
    if not same_identity:
        raise RefreshApprovalReplayError("refresh confirmation identity changed")
    if expires_at <= at:
        raise RefreshApprovalExpiredError("approval expired; create a new refresh request")


def _require_refresh_identifier(name: str, value: object) -> None:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise OperationalError(f"{name} must be a non-empty string of at most 256 characters")


def _require_refresh_digest(name: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OperationalError(f"{name} must be a SHA-256 hex digest")


def _require_optional_refresh_confirmation_token(value: object) -> None:
    if value is not None and (
        not isinstance(value, str) or not value or len(value) > 256
    ):
        raise RefreshConfirmationError(
            "confirmation token must be a non-empty string of at most 256 characters"
        )


def _refresh_control(request_json: object) -> dict[str, str]:
    try:
        request = json.loads(request_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise OperationalError("durable refresh request metadata is invalid") from error
    if not isinstance(request, Mapping):
        raise OperationalError("durable refresh request metadata is invalid")
    control = request.get("_refresh_control")
    if not isinstance(control, Mapping):
        raise OperationalError("durable refresh request metadata is missing")
    values: dict[str, str] = {}
    for key in (
        "principal",
        "request_fingerprint",
        "dedupe_key",
        "submitted_at",
        "cooldown_until",
        "initial_state",
    ):
        value = control.get(key)
        if not isinstance(value, str) or not value:
            raise OperationalError("durable refresh request metadata is invalid")
        values[key] = value
    _require_refresh_identifier("principal", values["principal"])
    _require_refresh_digest("request_fingerprint", values["request_fingerprint"])
    _require_refresh_digest("dedupe_key", values["dedupe_key"])
    if values["initial_state"] != "queued":
        raise OperationalError("durable refresh request initial state is invalid")
    submitted_at = _parse_timestamp(values["submitted_at"])
    cooldown_until = _parse_timestamp(values["cooldown_until"])
    if cooldown_until < submitted_at:
        raise OperationalError("durable refresh request cooldown is invalid")
    return values


def _max_attempts(definition: DatasourceDefinitionDescriptor) -> int:
    policy = _thaw(definition.retry_policy)
    value = policy.get("max_attempts", 4)
    return value if isinstance(value, int) and 1 <= value <= 20 else 4


def _schedule_id(definition: DatasourceDefinitionDescriptor, name: str) -> str:
    digest = request_hash(
        {
            "datasource_id": definition.datasource_id,
            "definition_version": definition.definition_version,
            "name": name,
        }
    )
    return f"schedule_{digest[:24]}"


def _validate_output_file(path: Path, root: Path, expected_sha256: str) -> None:
    """Verify a bounded regular output remains under the approved root."""

    if len(expected_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        raise OperationalError("projection output hash is invalid")
    try:
        path.relative_to(root)
    except ValueError as error:
        raise OperationalError("projection output escaped its configured directory") from error
    if path.is_symlink() or not path.is_file():
        raise OperationalError("projection output must be a regular file")
    if path.stat().st_size > 16 * 1024 * 1024:
        raise OperationalError("projection output exceeds the audit bound")
    digest = sha256()
    with path.open("rb") as output:
        while chunk := output.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise OperationalError("projection output content hash does not match its report")


def _record_delivered_alerts(connection: object, path: Path, now: str) -> int:
    """Insert deterministic threshold alerts without any delivery side effect."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OperationalError("projection alerts output is unreadable") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != "alert_delivery.v1":
        raise OperationalError("projection alerts output schema is unsupported")
    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or len(alerts) > 10_000:
        raise OperationalError("projection alerts output is invalid")
    inserted = 0
    for alert in alerts:
        if not isinstance(alert, dict):
            raise OperationalError("projection alert is invalid")
        alert_id = alert.get("alert_id")
        rule_id = alert.get("rule_id")
        if (
            not isinstance(alert_id, str)
            or not alert_id.startswith("alert_")
            or not isinstance(rule_id, str)
            or not rule_id
        ):
            raise OperationalError("projection alert identity is invalid")
        inserted += connection.execute(  # type: ignore[attr-defined]
            """
            INSERT OR IGNORE INTO operational_alert (
                alert_id, alert_type, severity, state, details_json, created_at, resolved_at
            ) VALUES (?, 'threshold', 'warning', 'open', ?, ?, NULL)
            """,
            (alert_id, _json(alert), now),
        ).rowcount
    return inserted
