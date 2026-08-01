"""Small single-writer daemon supervisor for durable datasource jobs."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from nan_fung.datasources.common import AcquisitionMetadata, HostRequestGate, HostThrottleBlocked
from nan_fung.ingestion.bank_rate import BANK_RATE_DATASOURCE_ID, BankRateArtifact
from nan_fung.ingestion.file_release_lifecycle import (
    FileReleaseCapture,
    acquire_live_file_release,
    ingest_file_release_artifacts,
)
from nan_fung.ingestion.file_release_workflow import (
    FILE_RELEASE_AUTOMATIC_DATASOURCE_IDS,
)
from nan_fung.ingestion.official_macro_lifecycle import (
    OFFICIAL_MACRO_AUTOMATIC_DATASOURCE_IDS,
    OfficialMacroArtifact,
    acquire_live_official_macro,
    ingest_official_macro_artifact,
)
from nan_fung.ingestion.official_macro_workflow import request_for
from nan_fung.ingestion.onspd_lifecycle import (
    ONSPD_DATASOURCE_ID,
    OnspdArtifacts,
    acquire_live_onspd_postcode,
    ingest_onspd_postcode_artifacts,
)
from nan_fung.datasources.geography import normalize_postcode
from nan_fung.operational import OperationalError, OperationalStore, RunHandle, WriterAlreadyRunningError
from nan_fung.workflows import acquire_live_bank_rate, ingest_bank_rate_artifact


_MAX_RESIDENT_POLL_SECONDS = 120.0


@dataclass(frozen=True)
class SupervisorTick:
    """A bounded report from one scheduler/recovery/worker pass."""

    schema_version: str
    at: str
    scheduler: Mapping[str, object]
    job_id: str | None = None
    run_id: str | None = None
    state: str = "idle"
    error_code: str | None = None


@dataclass(frozen=True)
class SupervisorRun:
    """Bounded summary returned when a resident daemon receives shutdown."""

    schema_version: str
    tick_count: int
    last_state: str
    last_job_id: str | None
    shutdown_state: str


class DatasourceSupervisor:
    """Own the process lock and execute at most one job per ``run_once``.

    The deliberately finite loop makes recovery observable and testable.  A
    hosting service can call it repeatedly; it never exposes collector or
    writer capabilities to agent-facing code.
    """

    def __init__(
        self,
        store: OperationalStore,
        *,
        worker_id: str,
        bank_rate_collector: Callable[[Mapping[str, object]], BankRateArtifact]
        | None = None,
        official_macro_collector: Callable[
            [str, Mapping[str, object]], OfficialMacroArtifact
        ]
        | None = None,
        file_release_collector: Callable[
            [str, Mapping[str, object]], FileReleaseCapture
        ]
        | None = None,
        onspd_collector: Callable[[str, Mapping[str, object]], OnspdArtifacts]
        | None = None,
        onspd_retention_until: datetime | None = None,
        allow_network: bool = False,
        clock: Callable[[], datetime] | None = None,
        resolver: Callable[[str], Iterable[str]] | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id is required")
        self._store = store
        self._worker_id = worker_id
        self._bank_rate_collector = bank_rate_collector
        self._official_macro_collector = official_macro_collector
        self._file_release_collector = file_release_collector
        self._onspd_collector = onspd_collector
        if onspd_retention_until is not None and (
            onspd_retention_until.tzinfo is None
            or onspd_retention_until.utcoffset() is None
        ):
            raise ValueError("onspd_retention_until must be timezone-aware")
        self._onspd_retention_until = (
            onspd_retention_until.astimezone(UTC)
            if onspd_retention_until is not None
            else None
        )
        self._allow_network = allow_network
        self._clock = clock or (lambda: datetime.now(UTC))
        self._resolver = resolver

    def run_once(self, *, now: datetime | None = None) -> SupervisorTick:
        anchor = _utc(now if now is not None else self._clock())
        run_clock = (lambda: anchor) if now is not None else self._clock
        with self._store.writer_session():
            self._store.sync_registry(now=anchor)
            self._store.update_service_heartbeat(
                instance_id=self._worker_id,
                role="daemon",
                state="running",
                now=anchor,
            )
            schedule = self._store.scheduler_tick(now=anchor)
            claim = self._store.claim_next(self._worker_id, now=anchor)
            if claim is None:
                return SupervisorTick(
                    "supervisor_tick.v1", _timestamp(anchor), schedule, state="idle"
                )
            if claim.datasource_id is None:
                system_run = None
                try:
                    system_run = self._store.start_system_job(
                        claim, self._worker_id, now=anchor
                    )
                    if claim.job_kind != "projection_delivery":
                        raise OperationalError("unsupported system job")
                    output_directory, as_of_at = _projection_delivery_request(claim.request)
                    self._store.publish_projections(
                        output_directory,
                        as_of_at=as_of_at,
                        actor_id=self._worker_id,
                    )
                    self._store.finish_system_job(
                        system_run, status="succeeded", now=_utc(run_clock())
                    )
                    return SupervisorTick(
                        "supervisor_tick.v1",
                        _timestamp(anchor),
                        schedule,
                        job_id=claim.job_id,
                        state="succeeded",
                    )
                except Exception as error:
                    code = (
                        "UNSUPPORTED_SYSTEM_JOB"
                        if isinstance(error, OperationalError)
                        and str(error) == "unsupported system job"
                        else "PROJECTION_DELIVERY_FAILED"
                    )
                    try:
                        if system_run is None:
                            raise OperationalError("system job did not start")
                        self._store.finish_system_job(
                            system_run,
                            status="failed",
                            error={
                                "schema_version": "error.v1",
                                "code": code,
                                "stage": "projection_delivery",
                                "retryable": True,
                                "details": {"exception": type(error).__name__},
                            },
                            now=_utc(run_clock()),
                        )
                    except OperationalError:
                        pass
                    return SupervisorTick(
                        "supervisor_tick.v1",
                        _timestamp(anchor),
                        schedule,
                        job_id=claim.job_id,
                        state="failed",
                        error_code=code,
                    )
            run = self._store.start_run(claim, self._worker_id, now=anchor)
            if claim.datasource_id not in {
                BANK_RATE_DATASOURCE_ID,
                *OFFICIAL_MACRO_AUTOMATIC_DATASOURCE_IDS,
                *FILE_RELEASE_AUTOMATIC_DATASOURCE_IDS,
                ONSPD_DATASOURCE_ID,
            }:
                self._store.finish_run(
                    run,
                    status="failed",
                    error={
                        "schema_version": "error.v1",
                        "code": "WORKER_UNBOUND",
                        "stage": "worker",
                        "retryable": False,
                    },
                    now=anchor,
                )
                return SupervisorTick(
                    "supervisor_tick.v1",
                    _timestamp(anchor),
                    schedule,
                    job_id=claim.job_id,
                    run_id=run.run_id,
                    state="failed",
                    error_code="WORKER_UNBOUND",
                )
            if claim.datasource_id in {
                *OFFICIAL_MACRO_AUTOMATIC_DATASOURCE_IDS,
                *FILE_RELEASE_AUTOMATIC_DATASOURCE_IDS,
                ONSPD_DATASOURCE_ID,
            } and claim.job_kind == "backfill":
                self._store.finish_run(
                    run,
                    status="failed",
                    error={
                        "schema_version": "error.v1",
                        "code": "BACKFILL_UNSUPPORTED",
                        "stage": "worker",
                        "retryable": False,
                    },
                    now=anchor,
                )
                return SupervisorTick(
                    "supervisor_tick.v1",
                    _timestamp(anchor),
                    schedule,
                    job_id=claim.job_id,
                    run_id=run.run_id,
                    state="failed",
                    error_code="BACKFILL_UNSUPPORTED",
                )
            if (
                claim.datasource_id == ONSPD_DATASOURCE_ID
                and self._onspd_retention_until is None
            ):
                self._store.finish_run(
                    run,
                    status="failed",
                    error={
                        "schema_version": "error.v1",
                        "code": "RETENTION_APPROVAL_REQUIRED",
                        "stage": "policy",
                        "retryable": False,
                    },
                    now=anchor,
                )
                return SupervisorTick(
                    "supervisor_tick.v1",
                    _timestamp(anchor),
                    schedule,
                    job_id=claim.job_id,
                    run_id=run.run_id,
                    state="failed",
                    error_code="RETENTION_APPROVAL_REQUIRED",
                )
            onspd_postcode: str | None = None
            if claim.datasource_id == ONSPD_DATASOURCE_ID:
                try:
                    onspd_postcode = _onspd_postcode_from_request(claim.request)
                except OperationalError:
                    self._store.finish_run(
                        run,
                        status="failed",
                        error={
                            "schema_version": "error.v1",
                            "code": "INVALID_ON_DEMAND_REQUEST",
                            "stage": "request",
                            "retryable": False,
                        },
                        now=anchor,
                    )
                    return SupervisorTick(
                        "supervisor_tick.v1",
                        _timestamp(anchor),
                        schedule,
                        job_id=claim.job_id,
                        run_id=run.run_id,
                        state="failed",
                        error_code="INVALID_ON_DEMAND_REQUEST",
                    )
            try:
                if claim.datasource_id == BANK_RATE_DATASOURCE_ID:
                    backfill_window = _claimed_backfill_window(
                        claim.job_kind, claim.window_start, claim.window_end
                    )
                    artifact = (
                        self._bank_rate_collector(claim.request)
                        if self._bank_rate_collector is not None
                        else self._collect_live_bank_rate(
                            run,
                            claim.request,
                            backfill_window=backfill_window,
                            host_gate=self._store.host_throttle_gate(
                                clock=lambda: _utc(run_clock())
                            ),
                            clock=run_clock,
                        )
                    )
                    execution_at = _utc(run_clock())
                    self._store.heartbeat(run, now=execution_at)
                    result = ingest_bank_rate_artifact(
                        self._store,
                        artifact,
                        lane=run.lane,
                        worker_id=self._worker_id,
                        existing_run=run,
                        execution_at=execution_at,
                        window_start=(backfill_window[0] if backfill_window else None),
                        window_end=(backfill_window[1] if backfill_window else None),
                    )
                elif claim.datasource_id in OFFICIAL_MACRO_AUTOMATIC_DATASOURCE_IDS:
                    datasource_id = claim.datasource_id
                    assert datasource_id is not None
                    artifact = (
                        self._official_macro_collector(datasource_id, claim.request)
                        if self._official_macro_collector is not None
                        else self._collect_live_official_macro(
                            run,
                            datasource_id,
                            host_gate=self._store.host_throttle_gate(
                                clock=lambda: _utc(run_clock())
                            ),
                            clock=run_clock,
                        )
                    )
                    execution_at = _utc(run_clock())
                    self._store.heartbeat(run, now=execution_at)
                    result = ingest_official_macro_artifact(
                        self._store,
                        datasource_id,
                        artifact,
                        lane=run.lane,
                        worker_id=self._worker_id,
                        existing_run=run,
                        execution_at=execution_at,
                    )
                elif claim.datasource_id == ONSPD_DATASOURCE_ID:
                    assert onspd_postcode is not None
                    artifacts = (
                        self._onspd_collector(onspd_postcode, claim.request)
                        if self._onspd_collector is not None
                        else self._collect_live_onspd(
                            run,
                            onspd_postcode,
                            host_gate=self._store.host_throttle_gate(
                                clock=lambda: _utc(run_clock())
                            ),
                            clock=run_clock,
                        )
                    )
                    execution_at = _utc(run_clock())
                    self._store.heartbeat(run, now=execution_at)
                    result = ingest_onspd_postcode_artifacts(
                        self._store,
                        artifacts,
                        lane=run.lane,
                        worker_id=self._worker_id,
                        active_run=run,
                        definition_version=run.definition_version,
                        clock=lambda: execution_at,
                        retention_until=self._onspd_retention_until,
                    )
                else:
                    datasource_id = claim.datasource_id
                    assert datasource_id is not None
                    capture = (
                        self._file_release_collector(datasource_id, claim.request)
                        if self._file_release_collector is not None
                        else self._collect_live_file_release(
                            run,
                            datasource_id,
                            host_gate=self._store.host_throttle_gate(
                                clock=lambda: _utc(run_clock())
                            ),
                            clock=run_clock,
                        )
                    )
                    execution_at = _utc(run_clock())
                    self._store.heartbeat(run, now=execution_at)
                    result = ingest_file_release_artifacts(
                        self._store,
                        datasource_id,
                        discovery=capture.discovery,
                        release=capture.release,
                        lane=run.lane,
                        worker_id=self._worker_id,
                        existing_run=run,
                        execution_at=execution_at,
                    )
            except Exception as error:
                # The lifecycle finalizes Bank Rate runs itself once it starts.
                # A collector failure occurs earlier and needs this close-out.
                throttled_until = (
                    error.blocked_until
                    if isinstance(error, HostThrottleBlocked)
                    else None
                )
                error_code = "HOST_THROTTLED" if throttled_until is not None else "ACQUIRE_FAILED"
                job = self._store.get_job(claim.job_id)
                if job is not None and job["state"] == "running":
                    try:
                        self._store.finish_run(
                            run,
                            status="failed",
                            retryable=True,
                            retry_at=throttled_until,
                            error={
                                "schema_version": "error.v1",
                                "code": error_code,
                                "stage": "acquire",
                                "retryable": True,
                                "details": {"exception": type(error).__name__},
                            },
                            now=_utc(run_clock()),
                        )
                    except OperationalError:
                        # The claim expired while the collector was blocked;
                        # recovery owns terminal state rather than a stale worker.
                        pass
                return SupervisorTick(
                    "supervisor_tick.v1",
                    _timestamp(anchor),
                    schedule,
                    job_id=claim.job_id,
                    run_id=run.run_id,
                    state="failed",
                    error_code=error_code,
                )
            return SupervisorTick(
                "supervisor_tick.v1",
                _timestamp(anchor),
                schedule,
                job_id=claim.job_id,
                run_id=result.run_id,
                state=result.status,
            )

    def run_until(
        self,
        *,
        should_stop: Callable[[], bool],
        wait: Callable[[float], None],
        poll_interval_seconds: float = 30.0,
    ) -> SupervisorRun:
        """Run finite ticks until a signal adapter asks for graceful stop.

        The injected wait/stop hooks keep this loop deterministic in tests and
        let the CLI use a signal-aware event rather than an uninterruptible
        sleep. Each tick owns and completes its own lease before the next stop
        check; the final heartbeat therefore never abandons a running worker.
        """

        if not 0 < poll_interval_seconds <= _MAX_RESIDENT_POLL_SECONDS:
            raise ValueError(
                "poll_interval_seconds must be in (0, 120] to preserve heartbeats"
            )
        tick_count = 0
        last_state = "idle"
        last_job_id: str | None = None
        self._store.sync_registry()
        self._store.update_service_heartbeat(
            instance_id=self._worker_id,
            role="daemon",
            state="starting",
        )
        try:
            while not should_stop():
                tick = self.run_once()
                tick_count += 1
                last_state = tick.state
                last_job_id = tick.job_id
                if should_stop():
                    break
                self._store.update_service_heartbeat(
                    instance_id=self._worker_id,
                    role="daemon",
                    state="running",
                )
                wait(poll_interval_seconds)
        finally:
            self._store.update_service_heartbeat(
                instance_id=self._worker_id,
                role="daemon",
                state="stopping",
            )
        return SupervisorRun(
            "supervisor_run.v1",
            tick_count,
            last_state,
            last_job_id,
            "stopping",
        )

    def _collect_live_bank_rate(
        self,
        run: RunHandle,
        request: Mapping[str, object],
        *,
        backfill_window: tuple[datetime, datetime] | None,
        host_gate: HostRequestGate,
        clock: Callable[[], datetime],
    ) -> BankRateArtifact:
        if not self._allow_network:
            raise OperationalError("live collection requires explicit network opt-in")
        return _collect_bank_rate(
            self._store,
            run,
            request,
            host_gate=host_gate,
            clock=clock,
            resolver=self._resolver,
            backfill_window=backfill_window,
        )

    def _collect_live_official_macro(
        self,
        run: RunHandle,
        datasource_id: str,
        *,
        host_gate: HostRequestGate,
        clock: Callable[[], datetime],
    ) -> OfficialMacroArtifact:
        if not self._allow_network:
            raise OperationalError("live collection requires explicit network opt-in")
        return _collect_official_macro(
            self._store,
            run,
            datasource_id,
            host_gate=host_gate,
            clock=clock,
            resolver=self._resolver,
        )

    def _collect_live_file_release(
        self,
        run: RunHandle,
        datasource_id: str,
        *,
        host_gate: HostRequestGate,
        clock: Callable[[], datetime],
    ) -> FileReleaseCapture:
        if not self._allow_network:
            raise OperationalError("live collection requires explicit network opt-in")
        return _collect_file_release(
            self._store,
            run,
            datasource_id,
            host_gate=host_gate,
            clock=clock,
            resolver=self._resolver,
        )

    def _collect_live_onspd(
        self,
        run: RunHandle,
        postcode: str,
        *,
        host_gate: HostRequestGate,
        clock: Callable[[], datetime],
    ) -> OnspdArtifacts:
        if not self._allow_network:
            raise OperationalError("live collection requires explicit network opt-in")
        assert self._onspd_retention_until is not None
        return acquire_live_onspd_postcode(
            self._store,
            postcode,
            active_run=run,
            host_gate=host_gate,
            resolver=self._resolver,
            clock=clock,
            retention_until=self._onspd_retention_until,
        )


def _collect_bank_rate(
    store: OperationalStore,
    run: RunHandle,
    request: Mapping[str, object],
    *,
    host_gate: HostRequestGate,
    clock: Callable[[], datetime],
    resolver: Callable[[str], Iterable[str]] | None,
    backfill_window: tuple[datetime, datetime] | None,
) -> BankRateArtifact:
    date_from = request.get("date_from")
    date_to = request.get("date_to")
    if backfill_window is not None:
        date_from = backfill_window[0].strftime("%d/%b/%Y")
        date_to = backfill_window[1].strftime("%d/%b/%Y")

    def preflight(metadata: AcquisitionMetadata) -> None:
        execution_at = _utc(clock())
        # Renew only if this worker still owns the claim.  A stale or expired
        # run fails before the fsynced temporary body is linked into the CAS.
        store.heartbeat(run, now=execution_at)
        store.preflight_evidence(
            run,
            request={"method": metadata.method, "url": metadata.request_url, "series": "IUDBEDR"},
            response={
                "status": metadata.status,
                "final_url": metadata.final_url,
                "headers": dict(metadata.headers),
            },
            retrieved_at=datetime.fromisoformat(metadata.retrieved_at),
            now=execution_at,
        )

    return acquire_live_bank_rate(
        date_from=str(date_from) if date_from else "01/Jan/2025",
        date_to=str(date_to) if date_to else "now",
        host_gate=host_gate,
        artifact_store=store.artifacts,
        before_publish=preflight,
        resolver=resolver,
    )


def _collect_official_macro(
    store: OperationalStore,
    run: RunHandle,
    datasource_id: str,
    *,
    host_gate: HostRequestGate,
    clock: Callable[[], datetime],
    resolver: Callable[[str], Iterable[str]] | None,
) -> OfficialMacroArtifact:
    """Collect a source-specific fixed macro request under the active lease."""

    request = request_for(datasource_id)

    def preflight(metadata: AcquisitionMetadata) -> None:
        execution_at = _utc(clock())
        store.heartbeat(run, now=execution_at)
        store.preflight_evidence(
            run,
            request={"method": metadata.method, "url": metadata.request_url},
            response={
                "status": metadata.status,
                "final_url": metadata.final_url,
                "headers": dict(metadata.headers),
            },
            source_id=request.source_id,
            retrieved_at=datetime.fromisoformat(metadata.retrieved_at),
            now=execution_at,
        )

    return acquire_live_official_macro(
        datasource_id,
        artifact_store=store.artifacts,
        host_gate=host_gate,
        before_publish=preflight,
        resolver=resolver,
    )


def _collect_file_release(
    store: OperationalStore,
    run: RunHandle,
    datasource_id: str,
    *,
    host_gate: HostRequestGate,
    clock: Callable[[], datetime],
    resolver: Callable[[str], Iterable[str]] | None,
) -> FileReleaseCapture:
    """Collect the closed discovery/release pair while the lease remains active."""

    def preflight(source_id: str, metadata: AcquisitionMetadata) -> None:
        execution_at = _utc(clock())
        store.heartbeat(run, now=execution_at)
        store.preflight_evidence(
            run,
            request={"method": metadata.method, "url": metadata.request_url},
            response={
                "status": metadata.status,
                "final_url": metadata.final_url,
                "headers": dict(metadata.headers),
            },
            source_id=source_id,
            retrieved_at=datetime.fromisoformat(metadata.retrieved_at),
            now=execution_at,
        )

    return acquire_live_file_release(
        datasource_id,
        artifact_store=store.artifacts,
        host_gate=host_gate,
        before_publish=preflight,
        resolver=resolver,
    )


def _claimed_backfill_window(
    job_kind: str,
    window_start: datetime | None,
    window_end: datetime | None,
) -> tuple[datetime, datetime] | None:
    """Use immutable job columns, never request JSON, for backfill bounds."""

    if job_kind != "backfill":
        return None
    if window_start is None or window_end is None:
        raise OperationalError("Bank Rate backfill job requires durable timestamp bounds")
    start = _utc(window_start)
    end = _utc(window_end)
    if start > end:
        raise OperationalError("Bank Rate backfill request has inverted bounds")
    return start, end


def _onspd_postcode_from_request(request: Mapping[str, object]) -> str:
    """Accept exactly one operator- or broker-selected postcode selector."""

    direct = request.get("postcode")
    scoped = request.get("scope")
    scoped_postcode: object | None = None
    if isinstance(scoped, Mapping):
        values = scoped.get("postcode")
        if isinstance(values, (list, tuple)) and len(values) == 1:
            scoped_postcode = values[0]
    candidates = [value for value in (direct, scoped_postcode) if value is not None]
    if len(candidates) != 1 or not isinstance(candidates[0], str):
        raise OperationalError("ONSPD on-demand job requires exactly one postcode")
    try:
        return normalize_postcode(candidates[0])
    except ValueError as error:
        raise OperationalError("ONSPD on-demand job has an invalid postcode") from error


def _projection_delivery_request(request: Mapping[str, object]) -> tuple[str, datetime]:
    """Accept only the persisted bounded projection delivery request shape."""

    if set(request) != {"output_directory", "as_of_at"}:
        raise OperationalError("projection delivery request is invalid")
    output_directory = request.get("output_directory")
    as_of_value = request.get("as_of_at")
    if (
        not isinstance(output_directory, str)
        or not output_directory
        or len(output_directory) > 4_096
        or not isinstance(as_of_value, str)
    ):
        raise OperationalError("projection delivery request is invalid")
    try:
        return output_directory, _utc(datetime.fromisoformat(as_of_value.replace("Z", "+00:00")))
    except ValueError as error:
        raise OperationalError("projection delivery as_of_at is invalid") from error


def _utc(value: datetime | None) -> datetime:
    candidate = value or datetime.now(UTC)
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise ValueError("supervisor timestamps must be timezone-aware")
    return candidate.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
