"""Concrete, trusted datasource workflow adapters.

The Bank Rate vertical slice, nine fixed ONS series, and two fixed London
Nomis datasets are executable operational workflows.  The Bank Rate adapter
lives here; the fixed ONS/Nomis lifecycle is in ``ingestion.official_macro_lifecycle``.
Other registered sources remain visible through registry/health as unbound or
policy-blocked until their source-specific workflow has the same physical
lifecycle.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from nan_fung.datasources.common import (
    AcquisitionMetadata,
    HostRequestGate,
    acquire_to_artifact,
)
from nan_fung.ingestion.bank_rate import (
    BANK_RATE_DATASOURCE_ID,
    BANK_RATE_PARAMS,
    BANK_RATE_SOURCE_POLICY,
    BOE_BANK_RATE_URL,
    AcquiredArtifact,
    BankRateArtifact,
    BankRateError,
    BankRateLifecycle,
    BankRateLifecycleResult,
    BankRateRecord,
    BankRateRun,
    PersistedEvidence as BankRateEvidence,
    StoredAcquiredArtifact,
    parse_bank_rate_csv_isolated,
)
from nan_fung.ingestion.canonical import thaw_json
from nan_fung.ingestion.registry import BindingDescriptor, SourceBinding
from nan_fung.operational import OperationalError, OperationalStore, PersistedEvidence, RunHandle
from nan_fung.storage.db import connect_database
from nan_fung.storage.artifacts import ArtifactStore


_BANK_RATE_MAX_STREAM_SECONDS = 120


class OperationalBankRatePersistence:
    """Adapt the generic Bank Rate lifecycle to the SQLite/CAS single writer."""

    def __init__(
        self,
        store: OperationalStore,
        *,
        worker_id: str,
        existing_run: RunHandle | None = None,
        execution_at: datetime | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        job_trigger: str = "manual",
        job_kind: str | None = None,
        job_request: Mapping[str, Any] | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id is required")
        self._store = store
        self._worker_id = worker_id
        self._runs: dict[str, RunHandle] = {}
        self._evidence: dict[str, PersistedEvidence] = {}
        self._promotion_requested: set[str] = set()
        self._existing_run = existing_run
        self._execution_at = (execution_at or datetime.now(UTC)).astimezone(UTC)
        self._window = _bank_rate_window(window_start, window_end)
        self._job_trigger = job_trigger
        self._job_kind = job_kind
        self._job_request = dict(job_request) if job_request is not None else None

    def create_run(self, run: BankRateRun) -> str:
        if run.datasource_id != BANK_RATE_DATASOURCE_ID:
            raise BankRateError("OperationalBankRatePersistence only owns Bank Rate")
        if self._existing_run is not None:
            handle = self._existing_run
            if (
                handle.datasource_id != run.datasource_id
                or handle.definition_version != run.definition_version
                or handle.lane != run.lane
            ):
                raise BankRateError("claimed job does not match Bank Rate lifecycle")
            self._runs[handle.run_id] = handle
            self._existing_run = None
            return handle.run_id
        queued = self._store.enqueue(
            run.datasource_id,
            definition_version=run.definition_version,
            request=(
                self._job_request
                if self._job_request is not None
                else thaw_json(run.request)
            ),
            trigger=self._job_trigger,
            lane=run.lane,
            scheduled_for=self._execution_at,
            request_instance_id=run.run_id,
            job_kind=self._job_kind,
        )
        claim = self._store.claim_job(
            queued.job_id, self._worker_id, now=self._execution_at
        )
        if claim is None:
            raise OperationalError("Bank Rate job could not be claimed")
        handle = self._store.start_run(claim, self._worker_id, now=self._execution_at)
        self._runs[handle.run_id] = handle
        return handle.run_id

    def persist_evidence(
        self, run_id: str, artifact: BankRateArtifact
    ) -> BankRateEvidence:
        handle = self._run(run_id)
        request_url = (
            artifact.request_url
            if isinstance(artifact, StoredAcquiredArtifact)
            else artifact.source_url
        )
        evidence_kwargs = {
            "media_type": artifact.media_type,
            "retrieved_at": artifact.retrieved_at,
            "request": {
                "method": "GET",
                "url": request_url,
                "series": "IUDBEDR",
            },
            "response": {
                "status": artifact.status,
                "final_url": artifact.source_url,
                "headers": thaw_json(artifact.headers),
            },
            "now": self._execution_at,
        }
        if isinstance(artifact, StoredAcquiredArtifact):
            persisted = self._store.persist_evidence(
                handle,
                artifact=artifact.artifact,
                **evidence_kwargs,
            )
        else:
            persisted = self._store.persist_evidence(
                handle,
                artifact.body,
                **evidence_kwargs,
            )
        self._evidence[persisted.evidence_id] = persisted
        return BankRateEvidence(persisted.evidence_id, persisted.artifact.content_sha256)

    def read_evidence(self, evidence: BankRateEvidence) -> bytes:
        return self._store.read_evidence(evidence.evidence_id)

    def persist_observation(
        self,
        run_id: str,
        evidence: BankRateEvidence,
        record: BankRateRecord,
        *,
        lane: str,
    ) -> str:
        handle = self._run(run_id)
        if handle.lane != lane:
            raise BankRateError("Bank Rate run lane does not match record lane")
        if self._window is not None:
            start, end = self._window
            effective_date = date.fromisoformat(record.effective_date)
            if not start <= effective_date <= end:
                raise BankRateError(
                    "Bank Rate record is outside the requested backfill window"
                )
        try:
            persisted = self._evidence[evidence.evidence_id]
        except KeyError as error:
            raise BankRateError("unknown Bank Rate evidence") from error
        return self._store.persist_observation(
            handle,
            record_key=record.record_key,
            payload=record.payload,
            record_type="metric",
            category="interest-rates-monetary-policy",
            evidence=(persisted,),
            source_date=record.effective_date,
            unit="percent",
            definition_text="Official Bank of England Bank Rate series IUDBEDR",
            limitations=("Current-vintage official series",),
            now=self._execution_at,
        )

    def promote(
        self, run_id: str, observation_ids: Sequence[str], *, lane: str
    ) -> bool:
        if lane != "production_ingestion":
            return False
        self._run(run_id)
        if not observation_ids:
            return False
        self._promotion_requested.add(run_id)
        # The actual promotion occurs atomically with terminal run completion.
        # A new promotion event is itself a canonical availability change.
        return True

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        error: Mapping[str, str] | None = None,
    ) -> None:
        handle = self._run(run_id)
        self._store.finish_run(
            handle,
            status=status,
            error=error,
            promote=status == "succeeded" and run_id in self._promotion_requested,
            now=self._execution_at,
        )

    def _run(self, run_id: str) -> RunHandle:
        try:
            return self._runs[run_id]
        except KeyError as error:
            raise BankRateError("unknown Bank Rate run") from error


def ingest_bank_rate_artifact(
    store: OperationalStore,
    artifact: BankRateArtifact,
    *,
    lane: str = "production_ingestion",
    worker_id: str = "bank-rate-worker",
    isolate_parser: bool = True,
    existing_run: RunHandle | None = None,
    definition_version: int | None = None,
    execution_at: datetime | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    job_trigger: str = "manual",
    job_kind: str | None = None,
    job_request: Mapping[str, Any] | None = None,
) -> BankRateLifecycleResult:
    """Run the complete Bank Rate lifecycle from an already acquired artifact."""

    if (
        existing_run is not None
        and definition_version is not None
        and existing_run.definition_version != definition_version
    ):
        raise BankRateError("claimed job does not match Bank Rate definition version")
    effective_definition_version = (
        existing_run.definition_version if existing_run is not None else definition_version
    )
    _require_static_bank_rate_contract(store, effective_definition_version)
    if isolate_parser:
        lifecycle = BankRateLifecycle(
            OperationalBankRatePersistence(
                store,
                worker_id=worker_id,
                existing_run=existing_run,
                execution_at=execution_at,
                window_start=window_start,
                window_end=window_end,
                job_trigger=job_trigger,
                job_kind=job_kind,
                job_request=job_request,
            ),
            registry=store.registry,
            bindings=store.runtime_bindings,
            record_parser=parse_bank_rate_csv_isolated,
        )
    else:
        # Keep the direct parser path explicit and test-only; production callers
        # use the default isolated runner above.
        from nan_fung.ingestion.bank_rate import parse_bank_rate_csv

        lifecycle = BankRateLifecycle(
            OperationalBankRatePersistence(
                store,
                worker_id=worker_id,
                existing_run=existing_run,
                execution_at=execution_at,
                window_start=window_start,
                window_end=window_end,
                job_trigger=job_trigger,
                job_kind=job_kind,
                job_request=job_request,
            ),
            registry=store.registry,
            bindings=store.runtime_bindings,
            record_parser=parse_bank_rate_csv,
        )
    return lifecycle.ingest(
        artifact,
        lane=lane,
        requested_at=artifact.retrieved_at,
        definition_version=effective_definition_version,
    )


def _require_static_bank_rate_contract(
    store: OperationalStore, definition_version: int | None
) -> None:
    """Fail closed when a definition needs a parser/collector code upgrade.

    This lifecycle intentionally implements the v1 Bank Rate physical contract
    directly.  A future definition may change policy fields while retaining
    these identities, but a changed executable binding needs a corresponding
    lifecycle implementation instead of silently running the v1 parser.
    """

    definition = store.registry.lookup(BANK_RATE_DATASOURCE_ID, definition_version)
    expected = (
        BindingDescriptor("collector", "bank_rate.collect", "v1"),
        BindingDescriptor("parser", "bank_rate.csv", "v1"),
        BindingDescriptor("record_key", "bank_rate.record_key", "v1"),
    )
    actual = (
        definition.collector_binding,
        definition.parser_binding,
        definition.record_key_binding,
    )
    # Evidence attribution is part of the physical contract too.  Retaining
    # the v1 parser while changing its upstream binding would otherwise make
    # this fixed IADB collector write evidence under the wrong source.
    if actual != expected or definition.source_bindings != (SourceBinding("boe.iadb"),):
        raise BankRateError("Bank Rate definition requires a new bound lifecycle")


def acquire_live_bank_rate(
    *,
    artifact_store: ArtifactStore,
    date_from: str = "01/Jan/2025",
    date_to: str = "now",
    host_gate: HostRequestGate | None = None,
    before_publish: Callable[[AcquisitionMetadata], None] | None = None,
    resolver: Callable[[str], Iterable[str]] | None = None,
) -> StoredAcquiredArtifact:
    """Acquire Bank Rate directly into a verified content-addressed artifact."""

    parameters = {**BANK_RATE_PARAMS, "Datefrom": date_from, "Dateto": date_to}
    response = acquire_to_artifact(
        BOE_BANK_RATE_URL,
        params=parameters,
        policy=BANK_RATE_SOURCE_POLICY,
        host_gate=host_gate,
        artifact_store=artifact_store,
        before_publish=before_publish,
        max_stream_seconds=_BANK_RATE_MAX_STREAM_SECONDS,
        resolver=resolver,
        require_full_response=True,
    )
    return StoredAcquiredArtifact(
        artifact=response.artifact,
        request_url=response.request_url,
        source_url=response.final_url,
        retrieved_at=datetime.fromisoformat(response.retrieved_at),
        status=response.status,
        headers=response.headers,
        media_type=response.artifact.media_type or "text/csv",
    )


def _bank_rate_window(
    window_start: datetime | None, window_end: datetime | None
) -> tuple[date, date] | None:
    if (window_start is None) != (window_end is None):
        raise BankRateError("Bank Rate backfill window requires both bounds")
    if window_start is None:
        return None
    assert window_end is not None
    start = window_start.astimezone(UTC).date()
    end = window_end.astimezone(UTC).date()
    if start > end:
        raise BankRateError("Bank Rate backfill window is invalid")
    return start, end


def reparse_bank_rate_evidence(
    store: OperationalStore,
    evidence_id: str,
    *,
    lane: str | None = None,
    worker_id: str = "bank-rate-reparse",
) -> BankRateLifecycleResult:
    """Replay a saved Bank Rate artifact with no network access whatsoever."""

    connection = connect_database(store.database_path, read_only=True)
    try:
        row = connection.execute(
            """
            SELECT e.media_type, e.retrieved_at, e.request_json, e.response_json,
                   r.lane AS original_lane,
                   r.definition_version AS original_version
            FROM evidence_artifact e
            JOIN run_evidence re ON re.evidence_id = e.evidence_id
            JOIN ingestion_run r ON r.run_id = re.run_id
            WHERE e.evidence_id = ?
              AND e.source_id = 'boe.iadb'
              AND r.datasource_id = ?
            """,
            (evidence_id, BANK_RATE_DATASOURCE_ID),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise OperationalError("evidence is not bound to Bank Rate provenance")
    original_lane = row["original_lane"]
    if not isinstance(original_lane, str):
        raise OperationalError("Bank Rate evidence is missing its source lane")
    if lane is not None and lane != original_lane:
        raise OperationalError("Bank Rate reparse lane must match the source lane")
    original_version = row["original_version"]
    if isinstance(original_version, bool) or not isinstance(original_version, int):
        raise OperationalError("Bank Rate evidence is missing its definition version")
    import json

    request = json.loads(row["request_json"])
    response = json.loads(row["response_json"])
    source_url = response.get("final_url") or request.get("url")
    if not isinstance(source_url, str) or not source_url:
        source_url = "https://www.bankofengland.co.uk/offline-replay.csv"
    headers = response.get("headers") if isinstance(response.get("headers"), Mapping) else {}
    status = response.get("status") if isinstance(response.get("status"), int) else 200
    artifact = AcquiredArtifact(
        body=store.read_evidence(evidence_id),
        source_url=source_url,
        retrieved_at=datetime.fromisoformat(row["retrieved_at"].replace("Z", "+00:00")),
        status=status,
        headers=headers,
        media_type=row["media_type"] or "text/csv",
    )
    return ingest_bank_rate_artifact(
        store,
        artifact,
        lane=original_lane,
        worker_id=worker_id,
        isolate_parser=True,
        definition_version=original_version,
        job_trigger="reparse",
        job_kind="offline_reparse",
        job_request={"reparse_evidence_id": evidence_id},
    )
