"""Durable evidence-to-canonical lifecycle for three approved file releases."""

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
from .file_release_workflow import (
    FILE_RELEASE_AUTOMATIC_DATASOURCE_IDS,
    FileReleaseAcquisition,
    FileReleaseContract,
    FileReleaseWorkflowError,
    adapt_discovery_metadata,
    adapt_release_metadata,
    contract_for,
    record_metadata_for,
    release_url_from_discovery,
    validate_release_url,
)
from .parser_runner import parse_saved_artifact
from .policies import validate_artifact_bytes, validate_artifact_file


_MAX_STREAM_SECONDS = 120


class FileReleaseLifecycleError(ValueError):
    """An approved file release cannot safely progress to canonical data."""


FileReleaseArtifact = AcquisitionResponse | StoredAcquisitionResponse


@dataclass(frozen=True, slots=True)
class FileReleaseCapture:
    """The two artifacts that make up one discovered official release.

    Live collection supplies streamed CAS handles; tests and offline operator
    fixtures may supply the same metadata with in-memory bytes.
    """

    discovery: FileReleaseArtifact
    release: FileReleaseArtifact


@dataclass(frozen=True, slots=True)
class FileReleaseLifecycleResult:
    """Immutable outcome of one release ingestion or offline replay."""

    run_id: str
    evidence_id: str
    discovery_evidence_id: str | None
    observation_ids: tuple[str, ...]
    status: str
    canonical_changed: bool


def acquire_live_file_release(
    datasource_id: str,
    *,
    artifact_store,
    host_gate: HostRequestGate | None = None,
    before_publish: Callable[[str, AcquisitionMetadata], None] | None = None,
    resolver: Callable[[str], Iterable[str]] | None = None,
) -> FileReleaseCapture:
    """Capture a fixed discovery page and its selected release into the CAS.

    Discovery is parsed only from the first saved artifact and can select only
    the contract's second-stage URL.  No network handle reaches the parser.
    """

    contract = _contract(datasource_id)

    def discovery_preflight(metadata: AcquisitionMetadata) -> None:
        adapt_discovery_metadata(datasource_id, metadata)
        if before_publish is not None:
            before_publish(contract.discovery_source_id, metadata)

    discovery = acquire_to_artifact(
        contract.discovery_url,
        artifact_store=artifact_store,
        policy=contract.discovery_policy,
        host_gate=host_gate,
        before_publish=discovery_preflight,
        max_stream_seconds=_MAX_STREAM_SECONDS,
        resolver=resolver,
        require_full_response=True,
    )
    parsed = parse_saved_artifact(
        artifact_store, discovery.artifact, contract.discovery_parser
    )
    release_url = release_url_from_discovery(datasource_id, parsed)

    def release_preflight(metadata: AcquisitionMetadata) -> None:
        adapt_release_metadata(datasource_id, metadata, release_url=release_url)
        if before_publish is not None:
            before_publish(contract.release_source_id, metadata)

    release = acquire_to_artifact(
        release_url,
        artifact_store=artifact_store,
        policy=contract.release_policy,
        host_gate=host_gate,
        before_publish=release_preflight,
        max_stream_seconds=_MAX_STREAM_SECONDS,
        resolver=resolver,
        require_full_response=True,
    )
    return FileReleaseCapture(discovery, release)


def ingest_file_release_artifacts(
    store: OperationalStore,
    datasource_id: str,
    *,
    release: FileReleaseArtifact,
    discovery: FileReleaseArtifact | None = None,
    lane: str = "production_ingestion",
    worker_id: str = "file-release-worker",
    existing_run: RunHandle | None = None,
    definition_version: int | None = None,
    execution_at: datetime | None = None,
    isolate_parser: bool = True,
    job_trigger: str = "manual",
    job_kind: str | None = None,
    job_request: Mapping[str, Any] | None = None,
) -> FileReleaseLifecycleResult:
    """Persist, isolate-parse, normalize, and promote one saved file release."""

    if not worker_id:
        raise ValueError("worker_id is required")
    contract = _contract(datasource_id)
    at = _utc(
        execution_at
        or datetime.fromisoformat(_retrieved_at(release).replace("Z", "+00:00"))
    )
    run = _existing_or_new_run(
        store,
        contract,
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
        discovery_evidence: PersistedEvidence | None = None
        if discovery is not None:
            discovery_metadata = adapt_discovery_metadata(datasource_id, discovery)
            _validate_artifact_policy(store, contract, "discovery", discovery)
            discovery_evidence = _persist_artifact(
                store,
                run,
                discovery_metadata,
                discovery,
                role="discovery",
                at=at,
            )
            parsed_discovery = _parse_saved(
                store,
                discovery_evidence,
                contract.discovery_parser,
                isolate_parser=isolate_parser,
            )
            release_url = release_url_from_discovery(datasource_id, parsed_discovery)
        else:
            release_url = validate_release_url(datasource_id, _request_url(release))
        release_metadata = adapt_release_metadata(
            datasource_id, release, release_url=release_url
        )
        _validate_artifact_policy(store, contract, "release", release)
        evidence = _persist_artifact(
            store, run, release_metadata, release, role="primary", at=at
        )
        parsed_release = _parse_saved(
            store,
            evidence,
            contract.release_parser,
            isolate_parser=isolate_parser,
        )
        records = _records(parsed_release)
        observation_ids = tuple(
            _persist_record(store, run, evidence, datasource_id, record, at=at)
            for record in records
        )
        status = "succeeded" if observation_ids else "empty"
        definition = store.registry.lookup(datasource_id, run.definition_version)
        promote = bool(
            observation_ids
            and run.lane == "production_ingestion"
            and definition.status == "production"
            and definition.promotion_policy == "automatic"
        )
        store.finish_run(run, status=status, promote=promote, now=at)
        return FileReleaseLifecycleResult(
            run_id=run.run_id,
            evidence_id=evidence.evidence_id,
            discovery_evidence_id=(
                discovery_evidence.evidence_id if discovery_evidence is not None else None
            ),
            observation_ids=observation_ids,
            status=status,
            canonical_changed=promote,
        )
    except Exception as error:
        _finish_failed(store, run, error, at=at)
        raise


def reparse_file_release_evidence(
    store: OperationalStore,
    datasource_id: str,
    evidence_id: str,
    *,
    lane: str | None = None,
    worker_id: str = "file-release-reparse",
) -> FileReleaseLifecycleResult:
    """Replay a saved release artifact without reacquiring its discovery page."""

    contract = _contract(datasource_id)
    connection = connect_database(store.database_path, read_only=True)
    try:
        row = connection.execute(
            """
            SELECT e.media_type, e.retrieved_at, e.request_json, e.response_json,
                   r.lane AS original_lane, r.definition_version AS original_version
            FROM evidence_artifact AS e
            JOIN run_evidence AS re ON re.evidence_id = e.evidence_id
            JOIN ingestion_run AS r ON r.run_id = re.run_id
            WHERE e.evidence_id = ?
              AND e.source_id = ?
              AND r.datasource_id = ?
            """,
            (evidence_id, contract.release_source_id, datasource_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise OperationalError("evidence is not bound to file-release provenance")
    original_lane = row["original_lane"]
    if not isinstance(original_lane, str):
        raise OperationalError("file-release evidence is missing its source lane")
    if lane is not None and lane != original_lane:
        raise OperationalError("file-release reparse lane must match the source lane")
    original_version = row["original_version"]
    if isinstance(original_version, bool) or not isinstance(original_version, int):
        raise OperationalError("file-release evidence is missing its definition version")
    request = json.loads(row["request_json"])
    response = json.loads(row["response_json"])
    request_url = request.get("url")
    final_url = response.get("final_url")
    status = response.get("status")
    headers = response.get("headers")
    if not isinstance(request_url, str) or not isinstance(final_url, str):
        raise OperationalError("file-release evidence is missing URL provenance")
    if not isinstance(status, int) or isinstance(status, bool):
        raise OperationalError("file-release evidence is missing response status")
    if not isinstance(headers, Mapping):
        headers = {"Content-Type": row["media_type"]} if row["media_type"] else {}
    return ingest_file_release_artifacts(
        store,
        datasource_id,
        release=AcquisitionResponse(
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


def _contract(datasource_id: str) -> FileReleaseContract:
    if datasource_id not in FILE_RELEASE_AUTOMATIC_DATASOURCE_IDS:
        raise FileReleaseLifecycleError(
            "datasource is not an approved automatic file-release workflow"
        )
    return contract_for(datasource_id)


def _existing_or_new_run(
    store: OperationalStore,
    contract: FileReleaseContract,
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
            existing_run.datasource_id != contract.datasource_id
            or existing_run.lane != lane
            or (
                definition_version is not None
                and existing_run.definition_version != definition_version
            )
        ):
            raise FileReleaseLifecycleError("claimed job does not match file-release lifecycle")
        _require_static_file_release_contract(
            store, contract, existing_run.definition_version
        )
        store.runtime_bindings.validate(
            store.registry.lookup(contract.datasource_id, existing_run.definition_version),
            operation="ingest",
        ).require()
        return existing_run
    definition = store.registry.lookup(contract.datasource_id, definition_version)
    _require_static_file_release_contract(
        store, contract, definition.definition_version
    )
    store.runtime_bindings.validate(definition, operation="ingest").require()
    queued = store.enqueue(
        contract.datasource_id,
        definition_version=definition.definition_version,
        request=(
            job_request if job_request is not None else {"workflow": "file_release"}
        ),
        trigger=job_trigger,
        lane=lane,
        scheduled_for=at,
        request_instance_id=new_id("file_release"),
        job_kind=job_kind,
    )
    claim = store.claim_job(queued.job_id, worker_id, now=at)
    if claim is None:
        raise OperationalError("file-release job could not be claimed")
    return store.start_run(claim, worker_id, now=at)


def _require_static_file_release_contract(
    store: OperationalStore,
    contract: FileReleaseContract,
    definition_version: int,
) -> None:
    """Do not replay a changed executable release contract with v1 code."""

    definition = store.registry.lookup(contract.datasource_id, definition_version)
    stem = contract.datasource_id.replace(".", "_")
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
    expected_sources = {
        (contract.discovery_source_id, 1),
        (contract.release_source_id, 1),
    }
    if actual != expected or not expected_sources <= sources:
        raise FileReleaseLifecycleError(
            "file-release definition requires a new bound lifecycle"
        )


def _persist_artifact(
    store: OperationalStore,
    run: RunHandle,
    metadata: FileReleaseAcquisition,
    artifact: FileReleaseArtifact,
    *,
    role: str,
    at: datetime,
) -> PersistedEvidence:
    kwargs: dict[str, Any] = {
        "role": role,
        "media_type": _media_type(artifact.headers),
        "request": {"method": "GET", "url": metadata.request_url},
        "response": {
            "status": metadata.status,
            "final_url": metadata.final_url,
            "headers": dict(metadata.headers),
        },
        "source_id": metadata.source_id,
        "retrieved_at": datetime.fromisoformat(metadata.retrieved_at.replace("Z", "+00:00")),
        "now": at,
    }
    if isinstance(artifact, StoredAcquisitionResponse):
        return store.persist_evidence(run, artifact=artifact.artifact, **kwargs)
    return store.persist_evidence(run, artifact.body, **kwargs)


def _validate_artifact_policy(
    store: OperationalStore,
    contract: FileReleaseContract,
    stage: str,
    artifact: FileReleaseArtifact,
) -> None:
    policy = contract.discovery_policy if stage == "discovery" else contract.release_policy
    media_type = _media_type(artifact.headers)
    if isinstance(artifact, StoredAcquisitionResponse):
        validate_artifact_file(
            store.artifacts.object_path(artifact.artifact.content_sha256),
            byte_size=artifact.artifact.byte_size,
            media_type=media_type,
            policy=policy.artifact,
        )
        return
    validate_artifact_bytes(artifact.body, media_type=media_type, policy=policy.artifact)


def _parse_saved(
    store: OperationalStore,
    evidence: PersistedEvidence,
    parser: Callable[[bytes], Any],
    *,
    isolate_parser: bool,
) -> Any:
    if isolate_parser:
        return parse_saved_artifact(store.artifacts, evidence.artifact, parser)
    return parser(store.read_evidence(evidence))


def _records(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or not all(isinstance(record, Mapping) for record in value):
        raise FileReleaseLifecycleError("file-release parser returned invalid records")
    return tuple(_canonical_record(record) for record in value)


def _canonical_record(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """Translate spreadsheet floats into JSON-safe source-value text."""

    def convert(value: Any) -> Any:
        if isinstance(value, float):
            return format(value, ".15g")
        if isinstance(value, Mapping):
            return {str(key): convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        if isinstance(value, tuple):
            return [convert(item) for item in value]
        return value

    return convert(record)


def _persist_record(
    store: OperationalStore,
    run: RunHandle,
    evidence: PersistedEvidence,
    datasource_id: str,
    record: Mapping[str, Any],
    *,
    at: datetime,
) -> str:
    metadata = record_metadata_for(datasource_id, record)
    return store.persist_observation(
        run,
        record_key=metadata["record_key"],
        payload=record,
        record_type=metadata["record_type"],
        category=metadata["category"],
        evidence=(evidence,),
        period_label=metadata["period_label"],
        unit=metadata["unit"],
        definition_text=metadata["definition"],
        limitations=metadata["limitations"],
        locator=metadata["locator"],
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
                "code": "FILE_RELEASE_FAILED",
                "stage": "parse_or_persist",
                "retryable": False,
                "details": {"exception": type(error).__name__},
            },
            now=at,
        )
    except OperationalError:
        pass


def _request_url(artifact: FileReleaseArtifact) -> str:
    if not isinstance(artifact.request_url, str) or not artifact.request_url:
        raise FileReleaseLifecycleError("file-release artifact is missing request URL")
    return artifact.request_url


def _retrieved_at(artifact: FileReleaseArtifact) -> str:
    if not isinstance(artifact.retrieved_at, str) or not artifact.retrieved_at:
        raise FileReleaseLifecycleError("file-release artifact is missing retrieved_at")
    return artifact.retrieved_at


def _media_type(headers: Mapping[str, str]) -> str | None:
    for name, value in headers.items():
        if name.lower() == "content-type" and isinstance(value, str):
            return value
    return None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FileReleaseLifecycleError("workflow timestamps must be timezone-aware")
    return value.astimezone(UTC)
