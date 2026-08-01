"""Durable, fixed workflows for approved ONS and Nomis macro sources."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Any

from nan_fung.datasources.common import (
    AcquisitionMetadata,
    AcquisitionResponse,
    HostRequestGate,
    StoredAcquisitionResponse,
    acquire_to_artifact,
)
from nan_fung.operational import OperationalError, OperationalStore, PersistedEvidence, RunHandle
from nan_fung.storage.db import connect_database

from .canonical import new_id
from .registry import BindingDescriptor
from .official_macro_workflow import (
    OfficialMacroAcquisition,
    OfficialMacroRequest,
    OfficialMacroWorkflowError,
    adapt_acquisition_response,
    parser_for,
    record_metadata_for,
    request_for,
    validate_saved_records,
)
from .parser_runner import parse_saved_artifact
from .policies import validate_artifact_bytes, validate_artifact_file


OFFICIAL_MACRO_AUTOMATIC_DATASOURCE_IDS = frozenset(
    {
        "ons.gdp.ecyx",
        "ons.gdp.ihyq",
        "ons.inflation.d7g7",
        "ons.inflation.l55o",
        "ons.inflation.czbh",
        "ons.labour.lf24",
        "ons.labour.mgsx",
        "ons.labour.ap2y",
        "ons.labour.kai9",
        "nomis.nm_59_1.london_lfs",
        "nomis.nm_130_1.london_workforce_jobs",
    }
)
_MAX_STREAM_SECONDS = 120


class OfficialMacroLifecycleError(ValueError):
    """A durable ONS/Nomis workflow cannot safely continue."""


@dataclass(frozen=True, slots=True)
class OfficialMacroLifecycleResult:
    """Immutable outcome of one evidence-to-canonical macro run."""

    run_id: str
    evidence_id: str
    observation_ids: tuple[str, ...]
    status: str
    canonical_changed: bool


OfficialMacroArtifact = AcquisitionResponse | StoredAcquisitionResponse


def acquire_live_official_macro(
    datasource_id: str,
    *,
    artifact_store,
    host_gate: HostRequestGate | None = None,
    before_publish: Callable[[AcquisitionMetadata], None] | None = None,
    resolver: Callable[[str], Iterable[str]] | None = None,
) -> StoredAcquisitionResponse:
    """Stream one fixed approved source into CAS before any parser executes."""

    request = _automatic_request(datasource_id)

    def prepublish(metadata: AcquisitionMetadata) -> None:
        _adapt_metadata(request, metadata)
        if before_publish is not None:
            before_publish(metadata)

    return acquire_to_artifact(
        request.url,
        artifact_store=artifact_store,
        policy=request.policy,
        host_gate=host_gate,
        before_publish=prepublish,
        max_stream_seconds=_MAX_STREAM_SECONDS,
        resolver=resolver,
        require_full_response=True,
    )


def ingest_official_macro_artifact(
    store: OperationalStore,
    datasource_id: str,
    artifact: OfficialMacroArtifact,
    *,
    lane: str = "production_ingestion",
    worker_id: str = "official-macro-worker",
    existing_run: RunHandle | None = None,
    definition_version: int | None = None,
    execution_at: datetime | None = None,
    isolate_parser: bool = True,
    job_trigger: str = "manual",
    job_kind: str | None = None,
    job_request: Mapping[str, Any] | None = None,
) -> OfficialMacroLifecycleResult:
    """Persist, isolate-parse, normalize, and promote one fixed macro artifact."""

    if not worker_id:
        raise ValueError("worker_id is required")
    request = _automatic_request(datasource_id)
    acquisition = _adapt_artifact(request, artifact)
    _validate_artifact_policy(store, request, artifact)
    at = _utc(execution_at or datetime.fromisoformat(acquisition.retrieved_at.replace("Z", "+00:00")))
    run = _existing_or_new_run(
        store,
        request,
        lane=lane,
        worker_id=worker_id,
        existing_run=existing_run,
        definition_version=definition_version,
        at=at,
        job_trigger=job_trigger,
        job_kind=job_kind,
        job_request=job_request,
    )
    try:
        evidence = _persist_artifact(
            store, run, request, acquisition, artifact, at=at
        )
        parsed = (
            parse_saved_artifact(store.artifacts, evidence.artifact, parser_for(datasource_id))
            if isolate_parser
            else parser_for(datasource_id)(store.read_evidence(evidence))
        )
        normalized = validate_saved_records(datasource_id, acquisition, parsed)
        observation_ids = tuple(
            _persist_record(store, run, evidence, datasource_id, key, record, at=at)
            for key, record in zip(normalized.record_keys, normalized.records, strict=True)
        )
        status = "succeeded" if observation_ids else "empty"
        promote = bool(
            observation_ids
            and run.lane == "production_ingestion"
            and store.registry.lookup(
                datasource_id, run.definition_version
            ).promotion_policy
            == "automatic"
        )
        store.finish_run(run, status=status, promote=promote, now=at)
        return OfficialMacroLifecycleResult(
            run_id=run.run_id,
            evidence_id=evidence.evidence_id,
            observation_ids=observation_ids,
            status=status,
            canonical_changed=promote,
        )
    except Exception as error:
        _finish_failed(store, run, error, at=at)
        raise


def reparse_official_macro_evidence(
    store: OperationalStore,
    datasource_id: str,
    evidence_id: str,
    *,
    lane: str | None = None,
    worker_id: str = "official-macro-reparse",
) -> OfficialMacroLifecycleResult:
    """Replay saved approved ONS/Nomis evidence without any network access."""

    request = _automatic_request(datasource_id)
    connection = connect_database(store.database_path, read_only=True)
    try:
        row = connection.execute(
            """
            SELECT e.media_type, e.retrieved_at, e.request_json, e.response_json,
                   r.lane AS original_lane,
                   r.definition_version AS original_version
            FROM evidence_artifact AS e
            JOIN run_evidence AS re ON re.evidence_id = e.evidence_id
            JOIN ingestion_run AS r ON r.run_id = re.run_id
            WHERE e.evidence_id = ?
              AND e.source_id = ?
              AND r.datasource_id = ?
            """,
            (evidence_id, request.source_id, datasource_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise OperationalError("evidence is not bound to official-macro provenance")
    original_lane = row["original_lane"]
    if not isinstance(original_lane, str):
        raise OperationalError("official-macro evidence is missing its source lane")
    if lane is not None and lane != original_lane:
        raise OperationalError("official-macro reparse lane must match the source lane")
    original_version = row["original_version"]
    if isinstance(original_version, bool) or not isinstance(original_version, int):
        raise OperationalError("official-macro evidence is missing its definition version")
    request_metadata = json.loads(row["request_json"])
    response_metadata = json.loads(row["response_json"])
    request_url = request_metadata.get("url")
    final_url = response_metadata.get("final_url")
    status = response_metadata.get("status")
    headers = response_metadata.get("headers")
    if not isinstance(request_url, str) or not isinstance(final_url, str):
        raise OperationalError("official-macro evidence is missing URL provenance")
    if not isinstance(status, int) or isinstance(status, bool):
        raise OperationalError("official-macro evidence is missing response status")
    if not isinstance(headers, Mapping):
        headers = {"Content-Type": row["media_type"]} if row["media_type"] else {}
    return ingest_official_macro_artifact(
        store,
        datasource_id,
        AcquisitionResponse(
            request_url=request_url,
            final_url=final_url,
            status=status,
            headers=headers,
            body=store.read_evidence(evidence_id),
            retrieved_at=row["retrieved_at"],
            method="GET",
        ),
        lane=original_lane,
        worker_id=worker_id,
        definition_version=original_version,
        isolate_parser=True,
        job_trigger="reparse",
        job_kind="offline_reparse",
        job_request={"reparse_evidence_id": evidence_id},
    )


def _automatic_request(datasource_id: str) -> OfficialMacroRequest:
    if datasource_id not in OFFICIAL_MACRO_AUTOMATIC_DATASOURCE_IDS:
        raise OfficialMacroLifecycleError(
            "datasource is not an approved automatic official-macro workflow"
        )
    return request_for(datasource_id)


def _adapt_artifact(
    request: OfficialMacroRequest, artifact: OfficialMacroArtifact
) -> OfficialMacroAcquisition:
    if isinstance(artifact, AcquisitionResponse):
        return adapt_acquisition_response(request, artifact)
    return _adapt_metadata(
        request,
        AcquisitionMetadata(
            request_url=artifact.request_url,
            final_url=artifact.final_url,
            status=artifact.status,
            headers=artifact.headers,
            retrieved_at=artifact.retrieved_at,
            method=artifact.method,
        ),
    )


def _adapt_metadata(
    request: OfficialMacroRequest, metadata: AcquisitionMetadata
) -> OfficialMacroAcquisition:
    return adapt_acquisition_response(
        request,
        AcquisitionResponse(
            request_url=metadata.request_url,
            final_url=metadata.final_url,
            status=metadata.status,
            headers=metadata.headers,
            body=b"",
            retrieved_at=metadata.retrieved_at,
            method=metadata.method,
        ),
    )


def _existing_or_new_run(
    store: OperationalStore,
    request: OfficialMacroRequest,
    *,
    lane: str,
    worker_id: str,
    existing_run: RunHandle | None,
    definition_version: int | None,
    at: datetime,
    job_trigger: str,
    job_kind: str | None,
    job_request: Mapping[str, Any] | None,
) -> RunHandle:
    if existing_run is not None:
        if (
            existing_run.datasource_id != request.datasource_id
            or existing_run.lane != lane
            or (
                definition_version is not None
                and existing_run.definition_version != definition_version
            )
        ):
            raise OfficialMacroLifecycleError("claimed job does not match macro lifecycle")
        _require_static_macro_contract(
            store, request, existing_run.definition_version
        )
        store.runtime_bindings.validate(
            store.registry.lookup(request.datasource_id, existing_run.definition_version),
            operation="ingest",
        ).require()
        return existing_run
    definition = store.registry.lookup(request.datasource_id, definition_version)
    _require_static_macro_contract(
        store, request, definition.definition_version
    )
    store.runtime_bindings.validate(definition, operation="ingest").require()
    queued = store.enqueue(
        request.datasource_id,
        definition_version=definition.definition_version,
        request=job_request if job_request is not None else {"url": request.url},
        trigger=job_trigger,
        lane=lane,
        scheduled_for=at,
        request_instance_id=new_id("official_macro"),
        job_kind=job_kind,
    )
    claim = store.claim_job(queued.job_id, worker_id, now=at)
    if claim is None:
        raise OperationalError("official macro job could not be claimed")
    return store.start_run(claim, worker_id, now=at)


def _require_static_macro_contract(
    store: OperationalStore,
    request: OfficialMacroRequest,
    definition_version: int,
) -> None:
    """Refuse a version whose executable contract the static adapter lacks."""

    definition = store.registry.lookup(request.datasource_id, definition_version)
    stem = request.datasource_id.replace(".", "_")
    expected = (
        BindingDescriptor("collector", f"{stem}.collector", "v1"),
        BindingDescriptor("parser", f"{stem}.parser", "v1"),
        BindingDescriptor("record_key", f"{stem}.record_key", "v1"),
    )
    actual = (
        definition.collector_binding,
        definition.parser_binding,
        definition.record_key_binding,
    )
    sources = {(item.source_id, item.source_version) for item in definition.source_bindings}
    if actual != expected or (request.source_id, 1) not in sources:
        raise OfficialMacroLifecycleError(
            "official-macro definition requires a new bound lifecycle"
        )


def _persist_artifact(
    store: OperationalStore,
    run: RunHandle,
    request: OfficialMacroRequest,
    acquisition: OfficialMacroAcquisition,
    artifact: OfficialMacroArtifact,
    *,
    at: datetime,
) -> PersistedEvidence:
    headers = dict(acquisition.headers)
    media_type = _media_type(headers)
    kwargs: dict[str, Any] = {
        "media_type": media_type,
        "request": {"method": "GET", "url": request.url},
        "response": {
            "status": acquisition.status,
            "final_url": acquisition.source_url,
            "headers": headers,
        },
        "source_id": request.source_id,
        "retrieved_at": datetime.fromisoformat(acquisition.retrieved_at.replace("Z", "+00:00")),
        "now": at,
    }
    if isinstance(artifact, StoredAcquisitionResponse):
        return store.persist_evidence(run, artifact=artifact.artifact, **kwargs)
    return store.persist_evidence(run, artifact.body, **kwargs)


def _validate_artifact_policy(
    store: OperationalStore,
    request: OfficialMacroRequest,
    artifact: OfficialMacroArtifact,
) -> None:
    """Keep fixtures and stored live artifacts under the same source policy."""

    media_type = _media_type(artifact.headers)
    if isinstance(artifact, StoredAcquisitionResponse):
        validate_artifact_file(
            store.artifacts.object_path(artifact.artifact.content_sha256),
            byte_size=artifact.artifact.byte_size,
            media_type=media_type,
            policy=request.policy.artifact,
        )
    else:
        validate_artifact_bytes(
            artifact.body,
            media_type=media_type,
            policy=request.policy.artifact,
        )


def _persist_record(
    store: OperationalStore,
    run: RunHandle,
    evidence: PersistedEvidence,
    datasource_id: str,
    key: tuple[str, ...],
    record: Mapping[str, Any],
    *,
    at: datetime,
) -> str:
    metadata = record_metadata_for(datasource_id, record)
    if metadata.get("datasource_id") != datasource_id:
        raise OfficialMacroLifecycleError("normalized record has the wrong datasource")
    locator = metadata.get("locator")
    if not isinstance(locator, Mapping):
        raise OfficialMacroLifecycleError("normalized record requires an artifact locator")
    limitations = metadata.get("limitations")
    if not isinstance(limitations, list) or not all(
        isinstance(item, str) for item in limitations
    ):
        raise OfficialMacroLifecycleError("normalized record limitations are invalid")
    return store.persist_observation(
        run,
        record_key=key,
        payload=record,
        record_type=_text(metadata, "record_type"),
        category=_text(metadata, "category"),
        evidence=(evidence,),
        source_date=_optional_text(metadata, "source_date"),
        period_label=_optional_text(metadata, "period_label"),
        unit=_optional_text(metadata, "unit"),
        definition_text=_optional_text(metadata, "definition"),
        limitations=limitations,
        locator=locator,
        now=at,
    )


def _finish_failed(
    store: OperationalStore, run: RunHandle, error: Exception, *, at: datetime
) -> None:
    try:
        store.finish_run(
            run,
            status="failed",
            error={
                "schema_version": "error.v1",
                "code": "OFFICIAL_MACRO_FAILED",
                "stage": "parse_or_persist",
                "retryable": False,
                "details": {"exception": type(error).__name__},
            },
            now=at,
        )
    except OperationalError:
        pass


def _media_type(headers: Mapping[str, str]) -> str | None:
    for name, value in headers.items():
        if name.lower() == "content-type" and isinstance(value, str):
            return value
    return None


def _text(metadata: Mapping[str, Any], key: str) -> str:
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        raise OfficialMacroLifecycleError(f"record metadata {key} is required")
    return value


def _optional_text(metadata: Mapping[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if value is not None and not isinstance(value, str):
        raise OfficialMacroLifecycleError(f"record metadata {key} must be a string")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OfficialMacroLifecycleError("workflow timestamps must be timezone-aware")
    return value.astimezone(UTC)
