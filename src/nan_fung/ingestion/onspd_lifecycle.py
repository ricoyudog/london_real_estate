"""Capture-before-parse lifecycle for one ONSPD postcode lookup.

This deliberately owns only an on-demand point lookup.  It has no page,
snapshot, schedule, or caller-supplied URL surface: captured layer metadata
determines the one object-ID field required by the fixed postcode query.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import json
import math
from time import monotonic
from typing import Any
from urllib.parse import parse_qsl, urlparse

from nan_fung.datasources.common import (
    AcquisitionMetadata,
    AcquisitionResponse,
    HostRequestGate,
    StoredAcquisitionResponse,
    acquire_to_artifact,
    build_url,
)
from nan_fung.datasources.geography import (
    ONSPD_LAYER_URL,
    ONSPD_QUERY_URL,
    ONSPD_SOURCE_POLICY,
    arcgis_layer_vintage,
    arcgis_object_id_field,
    normalize_postcode,
    onspd_layer_metadata_params,
    onspd_postcode_query_params,
    parse_arcgis_feature_page_json,
    parse_arcgis_layer_metadata_json,
)
from nan_fung.ingestion.canonical import new_id
from nan_fung.ingestion.parser_runner import parse_saved_artifact
from nan_fung.ingestion.policies import (
    PolicyError,
    validate_artifact_bytes,
    validate_artifact_file,
    validate_source_url,
)
from nan_fung.ingestion.registry import BindingDescriptor
from nan_fung.operational import OperationalError, OperationalStore, PersistedEvidence, RunHandle
from nan_fung.storage.db import connect_database


ONSPD_DATASOURCE_ID = "ons.onspd.postcode"
ONSPD_SOURCE_ID = "ons.onspd"
_MAX_STREAM_SECONDS = 120
_LANES = frozenset({"production_ingestion", "source_discovery", "ad_hoc_research"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


class OnspdLifecycleError(ValueError):
    """An ONSPD point lookup does not meet its fixed evidence contract."""


OnspdArtifact = AcquisitionResponse | StoredAcquisitionResponse


@dataclass(frozen=True, slots=True)
class OnspdArtifacts:
    """The two raw responses required to evidence one postcode lookup."""

    postcode: str
    metadata: OnspdArtifact
    query: OnspdArtifact

    def __post_init__(self) -> None:
        object.__setattr__(self, "postcode", normalize_postcode(self.postcode))
        if not isinstance(self.metadata, (AcquisitionResponse, StoredAcquisitionResponse)):
            raise OnspdLifecycleError("ONSPD metadata must be an acquisition artifact")
        if not isinstance(self.query, (AcquisitionResponse, StoredAcquisitionResponse)):
            raise OnspdLifecycleError("ONSPD query must be an acquisition artifact")


@dataclass(frozen=True, slots=True)
class OnspdLifecycleResult:
    """Immutable outcome of one ONSPD evidence-to-observation run."""

    run_id: str
    metadata_evidence_id: str
    query_evidence_id: str
    observation_ids: tuple[str, ...]
    status: str
    canonical_changed: bool


@dataclass(frozen=True, slots=True)
class _LayerContract:
    object_id_field: str
    vintage: str


def acquire_live_onspd_postcode(
    store: OperationalStore,
    postcode: str,
    *,
    active_run: RunHandle,
    host_gate: HostRequestGate | None = None,
    resolver: Callable[[str], Iterable[str]] | None = None,
    clock: Callable[[], datetime] = _utc_now,
    retention_until: datetime | None = None,
    monotonic_clock: Callable[[], float] = monotonic,
) -> OnspdArtifacts:
    """Capture the fixed metadata and one exact postcode query into CAS.

    Live capture always receives a running claim.  ``before_publish`` validates
    that claim before either object becomes a published CAS artifact; parser
    code only sees saved metadata after that boundary.
    """

    normalized = normalize_postcode(postcode)
    _validate_active_run(store, active_run, lane=active_run.lane)
    retention = _required_retention(
        store,
        retention_until,
        definition_version=active_run.definition_version,
    )
    metadata_url = _metadata_url()
    metadata = acquire_to_artifact(
        ONSPD_LAYER_URL,
        params=onspd_layer_metadata_params(),
        artifact_store=store.artifacts,
        policy=ONSPD_SOURCE_POLICY,
        host_gate=host_gate,
        resolver=resolver,
        max_stream_seconds=_MAX_STREAM_SECONDS,
        monotonic_clock=monotonic_clock,
        require_full_response=True,
        before_publish=_preflight_callback(
            store,
            active_run,
            expected_url=metadata_url,
            clock=clock,
            retention_until=retention,
        ),
    )
    _validate_artifact_contract(store, metadata, expected_url=metadata_url)
    layer = _layer_contract(
        _parse_stored_metadata(store, metadata, isolate_parser=True)
    )
    query_params = onspd_postcode_query_params(
        normalized, object_id_field=layer.object_id_field
    )
    query_url = build_url(ONSPD_QUERY_URL, query_params)
    query = acquire_to_artifact(
        ONSPD_QUERY_URL,
        params=query_params,
        artifact_store=store.artifacts,
        policy=ONSPD_SOURCE_POLICY,
        host_gate=host_gate,
        resolver=resolver,
        max_stream_seconds=_MAX_STREAM_SECONDS,
        monotonic_clock=monotonic_clock,
        require_full_response=True,
        before_publish=_preflight_callback(
            store,
            active_run,
            expected_url=query_url,
            clock=clock,
            retention_until=retention,
        ),
    )
    _validate_artifact_contract(store, query, expected_url=query_url)
    return OnspdArtifacts(normalized, metadata, query)


def ingest_onspd_postcode_artifacts(
    store: OperationalStore,
    artifacts: OnspdArtifacts,
    *,
    lane: str = "production_ingestion",
    worker_id: str = "onspd-worker",
    active_run: RunHandle | None = None,
    definition_version: int | None = None,
    clock: Callable[[], datetime] = _utc_now,
    retention_until: datetime | None = None,
    isolate_parser: bool = True,
) -> OnspdLifecycleResult:
    """Persist two saved responses, sandbox-parse, normalize, and promote.

    A production point lookup cannot manufacture its own run.  Non-production
    fixture/research lanes may create a local manual run, but can never ask the
    store to promote a canonical observation.
    """

    if lane not in _LANES:
        raise OnspdLifecycleError(f"unsupported ONSPD lane: {lane!r}")
    if not worker_id:
        raise OnspdLifecycleError("worker_id is required")
    if lane == "production_ingestion" and active_run is None:
        raise OnspdLifecycleError("production ONSPD ingestion requires an active run")
    metadata_url = _metadata_url()
    _validate_artifact_contract(store, artifacts.metadata, expected_url=metadata_url)
    # Validate the network boundary before a fixture can cause a run/evidence
    # write.  The exact query is checked after saved metadata yields its OID.
    _validate_artifact_contract(store, artifacts.query, expected_endpoint=ONSPD_QUERY_URL)
    effective_definition_version = (
        active_run.definition_version if active_run is not None else definition_version
    )
    retention = _required_retention(
        store, retention_until, definition_version=effective_definition_version
    )
    at = _clock_utc(clock)
    run = _existing_or_new_run(
        store,
        postcode=artifacts.postcode,
        lane=lane,
        worker_id=worker_id,
        active_run=active_run,
        definition_version=definition_version,
        at=at,
    )
    try:
        metadata_evidence = _persist_artifact(
            store,
            run,
            artifacts.metadata,
            role="layer_metadata",
            at=at,
            retention_until=retention,
        )
        metadata = _parse_metadata_evidence(
            store, metadata_evidence, isolate_parser=isolate_parser
        )
        layer = _layer_contract(metadata)
        query_url = build_url(
            ONSPD_QUERY_URL,
            onspd_postcode_query_params(
                artifacts.postcode, object_id_field=layer.object_id_field
            ),
        )
        _validate_artifact_contract(store, artifacts.query, expected_url=query_url)
        query_evidence = _persist_artifact(
            store,
            run,
            artifacts.query,
            role="postcode_query",
            at=at,
            retention_until=retention,
        )
        records = _parse_query_evidence(
            store, query_evidence, isolate_parser=isolate_parser
        )
        if not records:
            store.finish_run(run, status="empty", now=at)
            return OnspdLifecycleResult(
                run.run_id,
                metadata_evidence.evidence_id,
                query_evidence.evidence_id,
                (),
                "empty",
                False,
            )
        if len(records) != 1:
            raise OnspdLifecycleError("ONSPD postcode lookup returned multiple features")
        payload, locator = _normalise_record(
            records[0], postcode=artifacts.postcode, layer=layer
        )
        observation_id = store.persist_observation(
            run,
            record_key=onspd_postcode_record_key(payload),
            payload=payload,
            record_type="geography",
            category="postcode_geography",
            evidence=(metadata_evidence, query_evidence),
            source_date=layer.vintage[:10],
            period_label=layer.vintage,
            definition_text=(
                "ONS Postcode Directory current-vintage postcode geography"
            ),
            limitations=(
                "On-demand point lookup; not a complete postcode-directory snapshot.",
            ),
            locator=locator,
            now=at,
        )
        promote = (
            lane == "production_ingestion"
            and store.registry.lookup(
                ONSPD_DATASOURCE_ID, run.definition_version
            ).promotion_policy
            == "automatic"
        )
        store.finish_run(run, status="succeeded", promote=promote, now=at)
        return OnspdLifecycleResult(
            run.run_id,
            metadata_evidence.evidence_id,
            query_evidence.evidence_id,
            (observation_id,),
            "succeeded",
            promote,
        )
    except Exception as error:
        _finish_failed(store, run, error, at=at)
        raise


def reparse_onspd_postcode_evidence(
    store: OperationalStore,
    query_evidence_id: str,
    *,
    lane: str | None = None,
    worker_id: str = "onspd-reparse",
    active_run: RunHandle | None = None,
    clock: Callable[[], datetime] = _utc_now,
    isolate_parser: bool = True,
) -> OnspdLifecycleResult:
    """Replay one persisted ONSPD query and its companion metadata offline."""

    replay = _load_replay_artifacts(store, query_evidence_id)
    if lane is not None and lane != replay.lane:
        raise OperationalError("ONSPD reparse lane must match the source lane")
    replay_run = active_run
    if replay_run is None and replay.lane == "production_ingestion":
        at = _clock_utc(clock)
        queued = store.enqueue(
            ONSPD_DATASOURCE_ID,
            definition_version=replay.definition_version,
            request={"reparse_evidence_id": query_evidence_id},
            trigger="reparse",
            job_kind="offline_reparse",
            lane=replay.lane,
            scheduled_for=at,
            request_instance_id=new_id("onspd_reparse"),
        )
        claim = store.claim_job(queued.job_id, worker_id, now=at)
        if claim is None:
            raise OperationalError("ONSPD reparse job could not be claimed")
        replay_run = store.start_run(claim, worker_id, now=at)
    return ingest_onspd_postcode_artifacts(
        store,
        replay.artifacts,
        lane=replay.lane,
        worker_id=worker_id,
        active_run=replay_run,
        definition_version=replay.definition_version,
        clock=clock,
        retention_until=replay.retention_until,
        isolate_parser=isolate_parser,
    )


@dataclass(frozen=True, slots=True)
class _ReplayArtifacts:
    lane: str
    artifacts: OnspdArtifacts
    retention_until: datetime | None
    definition_version: int


def _load_replay_artifacts(
    store: OperationalStore, query_evidence_id: str
) -> _ReplayArtifacts:
    connection = connect_database(store.database_path, read_only=True)
    try:
        row = connection.execute(
            """
            SELECT r.lane AS original_lane,
                   r.definition_version AS original_version,
                   m.evidence_id AS metadata_evidence_id,
                   m.retention_until AS metadata_retention_until,
                   m.retrieved_at AS metadata_retrieved_at,
                   m.request_json AS metadata_request_json,
                   m.response_json AS metadata_response_json,
                   q.evidence_id AS query_evidence_id,
                   q.retention_until AS query_retention_until,
                   q.retrieved_at AS query_retrieved_at,
                   q.request_json AS query_request_json,
                   q.response_json AS query_response_json
            FROM evidence_artifact AS q
            JOIN run_evidence AS rq
              ON rq.evidence_id = q.evidence_id AND rq.role = 'postcode_query'
            JOIN ingestion_run AS r ON r.run_id = rq.run_id
            JOIN run_evidence AS rm
              ON rm.run_id = r.run_id AND rm.role = 'layer_metadata'
            JOIN evidence_artifact AS m ON m.evidence_id = rm.evidence_id
            WHERE q.evidence_id = ?
              AND q.source_id = ? AND q.source_version = 1
              AND m.source_id = ? AND m.source_version = 1
              AND r.datasource_id = ?
            """,
            (
                query_evidence_id,
                ONSPD_SOURCE_ID,
                ONSPD_SOURCE_ID,
                ONSPD_DATASOURCE_ID,
            ),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise OperationalError("evidence is not bound to ONSPD point-lookup provenance")
    lane = row["original_lane"]
    if not isinstance(lane, str):
        raise OperationalError("ONSPD evidence is missing its source lane")
    definition_version = row["original_version"]
    if isinstance(definition_version, bool) or not isinstance(definition_version, int):
        raise OperationalError("ONSPD evidence is missing its definition version")
    metadata = _replay_response(
        store,
        evidence_id=row["metadata_evidence_id"],
        retrieved_at=row["metadata_retrieved_at"],
        request_json=row["metadata_request_json"],
        response_json=row["metadata_response_json"],
    )
    query = _replay_response(
        store,
        evidence_id=row["query_evidence_id"],
        retrieved_at=row["query_retrieved_at"],
        request_json=row["query_request_json"],
        response_json=row["query_response_json"],
    )
    metadata_retention = _optional_timestamp(row["metadata_retention_until"])
    query_retention = _optional_timestamp(row["query_retention_until"])
    if metadata_retention != query_retention:
        raise OperationalError("ONSPD replay evidence has inconsistent retention deadlines")
    return _ReplayArtifacts(
        lane,
        OnspdArtifacts(_postcode_from_query_url(query.request_url), metadata, query),
        metadata_retention,
        definition_version,
    )


def _replay_response(
    store: OperationalStore,
    *,
    evidence_id: object,
    retrieved_at: object,
    request_json: object,
    response_json: object,
) -> AcquisitionResponse:
    if not isinstance(evidence_id, str) or not isinstance(retrieved_at, str):
        raise OperationalError("ONSPD evidence is missing required replay metadata")
    try:
        request = json_object(request_json)
        response = json_object(response_json)
    except OnspdLifecycleError as error:
        raise OperationalError("ONSPD evidence has invalid replay metadata") from error
    request_url = request.get("url")
    final_url = response.get("final_url")
    status = response.get("status")
    method = request.get("method")
    headers = response.get("headers")
    if not isinstance(request_url, str) or not isinstance(final_url, str):
        raise OperationalError("ONSPD evidence is missing URL provenance")
    if isinstance(status, bool) or not isinstance(status, int):
        raise OperationalError("ONSPD evidence is missing response status")
    if not isinstance(method, str) or not isinstance(headers, Mapping):
        raise OperationalError("ONSPD evidence is missing request metadata")
    return AcquisitionResponse(
        request_url=request_url,
        final_url=final_url,
        status=status,
        headers={str(name): str(value) for name, value in headers.items()},
        body=store.read_evidence(evidence_id),
        retrieved_at=retrieved_at,
        method=method,
    )


def json_object(value: object) -> dict[str, Any]:
    """Decode one persisted JSON metadata object without accepting other shapes."""

    if not isinstance(value, str):
        raise OnspdLifecycleError("persisted metadata must be JSON text")
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise OnspdLifecycleError("persisted metadata must be a JSON object")
    return decoded


def _postcode_from_query_url(url: str) -> str:
    where_values = [
        value
        for key, value in parse_qsl(urlparse(url).query, keep_blank_values=True)
        if key == "where"
    ]
    if len(where_values) != 1:
        raise OperationalError("ONSPD query evidence is missing its postcode selector")
    where = where_values[0]
    if not where.startswith("PCDS='") or not where.endswith("'"):
        raise OperationalError("ONSPD query evidence has an invalid postcode selector")
    return normalize_postcode(where[6:-1])


def _existing_or_new_run(
    store: OperationalStore,
    *,
    postcode: str,
    lane: str,
    worker_id: str,
    active_run: RunHandle | None,
    definition_version: int | None,
    at: datetime,
) -> RunHandle:
    if active_run is not None:
        _validate_active_run(
            store,
            active_run,
            lane=lane,
            definition_version=definition_version,
        )
        return active_run
    if lane == "production_ingestion":
        raise OnspdLifecycleError("production ONSPD ingestion requires an active run")
    definition = store.registry.lookup(ONSPD_DATASOURCE_ID, definition_version)
    _require_static_onspd_contract(store, definition.definition_version)
    store.runtime_bindings.validate(definition, operation="ingest").require()
    queued = store.enqueue(
        ONSPD_DATASOURCE_ID,
        definition_version=definition.definition_version,
        request={"postcode": postcode},
        trigger="manual",
        lane=lane,
        scheduled_for=at,
        request_instance_id=new_id("onspd"),
    )
    claim = store.claim_job(queued.job_id, worker_id, now=at)
    if claim is None:
        raise OperationalError("ONSPD point-lookup job could not be claimed")
    return store.start_run(claim, worker_id, now=at)


def _validate_active_run(
    store: OperationalStore,
    active_run: RunHandle,
    *,
    lane: str,
    definition_version: int | None = None,
) -> None:
    if (
        active_run.datasource_id != ONSPD_DATASOURCE_ID
        or active_run.lane != lane
        or (
            definition_version is not None
            and active_run.definition_version != definition_version
        )
    ):
        raise OnspdLifecycleError("active run does not match the ONSPD lifecycle")
    definition = store.registry.lookup(
        ONSPD_DATASOURCE_ID, active_run.definition_version
    )
    _require_static_onspd_contract(store, definition.definition_version)
    store.runtime_bindings.validate(definition, operation="ingest").require()


def _require_static_onspd_contract(
    store: OperationalStore, definition_version: int
) -> None:
    """Keep a changed ONSPD parser/selector definition from using v1 code."""

    definition = store.registry.lookup(ONSPD_DATASOURCE_ID, definition_version)
    expected = (
        BindingDescriptor("collector", "ons_onspd_postcode.collector", "v1"),
        BindingDescriptor("parser", "ons_onspd_postcode.parser", "v1"),
        BindingDescriptor("record_key", "ons_onspd_postcode.record_key", "v1"),
    )
    actual = (
        definition.collector_binding,
        definition.parser_binding,
        definition.record_key_binding,
    )
    sources = {(item.source_id, item.source_version) for item in definition.source_bindings}
    if actual != expected or (ONSPD_SOURCE_ID, 1) not in sources:
        raise OnspdLifecycleError("ONSPD definition requires a new bound lifecycle")


def _preflight_callback(
    store: OperationalStore,
    active_run: RunHandle,
    *,
    expected_url: str,
    clock: Callable[[], datetime],
    retention_until: datetime | None,
) -> Callable[[AcquisitionMetadata], None]:
    def before_publish(metadata: AcquisitionMetadata) -> None:
        _validate_metadata_contract(metadata, expected_url=expected_url)
        store.preflight_evidence(
            active_run,
            request={"method": "GET", "url": metadata.request_url},
            response={
                "status": metadata.status,
                "final_url": metadata.final_url,
                "headers": dict(metadata.headers),
            },
            source_id=ONSPD_SOURCE_ID,
            retrieved_at=_parse_timestamp(metadata.retrieved_at),
            retention_until=retention_until,
            now=_clock_utc(clock),
        )

    return before_publish


def _persist_artifact(
    store: OperationalStore,
    run: RunHandle,
    artifact: OnspdArtifact,
    *,
    role: str,
    at: datetime,
    retention_until: datetime | None,
) -> PersistedEvidence:
    kwargs = {
        "role": role,
        "media_type": _media_type(artifact.headers),
        "request": {"method": artifact.method, "url": artifact.request_url},
        "response": {
            "status": artifact.status,
            "final_url": artifact.final_url,
            "headers": dict(artifact.headers),
        },
        "source_id": ONSPD_SOURCE_ID,
        "retrieved_at": _parse_timestamp(artifact.retrieved_at),
        "retention_until": retention_until,
        "now": at,
    }
    if isinstance(artifact, StoredAcquisitionResponse):
        return store.persist_evidence(run, artifact=artifact.artifact, **kwargs)
    return store.persist_evidence(run, artifact.body, **kwargs)


def _validate_artifact_contract(
    store: OperationalStore,
    artifact: OnspdArtifact,
    *,
    expected_url: str | None = None,
    expected_endpoint: str | None = None,
) -> None:
    _validate_metadata_contract(
        AcquisitionMetadata(
            request_url=artifact.request_url,
            final_url=artifact.final_url,
            status=artifact.status,
            headers=artifact.headers,
            retrieved_at=artifact.retrieved_at,
            method=artifact.method,
        ),
        expected_url=expected_url,
        expected_endpoint=expected_endpoint,
    )
    media_type = _media_type(artifact.headers)
    try:
        if isinstance(artifact, StoredAcquisitionResponse):
            validate_artifact_file(
                store.artifacts.object_path(artifact.artifact.content_sha256),
                byte_size=artifact.artifact.byte_size,
                media_type=media_type,
                policy=ONSPD_SOURCE_POLICY.artifact,
            )
        else:
            validate_artifact_bytes(
                artifact.body,
                media_type=media_type,
                policy=ONSPD_SOURCE_POLICY.artifact,
            )
    except PolicyError as error:
        raise OnspdLifecycleError(str(error)) from error


def _validate_metadata_contract(
    metadata: AcquisitionMetadata,
    *,
    expected_url: str | None = None,
    expected_endpoint: str | None = None,
) -> None:
    if metadata.method.upper() != "GET":
        raise OnspdLifecycleError("ONSPD acquisition method must be GET")
    if metadata.status != 200 or _has_content_range(metadata.headers):
        raise OnspdLifecycleError("ONSPD lifecycle requires a complete HTTP 200 response")
    _validate_url(metadata.request_url, expected_url, expected_endpoint)
    _validate_url(metadata.final_url, expected_url, expected_endpoint)


def _validate_url(
    value: str,
    expected_url: str | None,
    expected_endpoint: str | None,
) -> None:
    try:
        validate_source_url(value, ONSPD_SOURCE_POLICY, resolver=None)
    except PolicyError as error:
        raise OnspdLifecycleError("ONSPD evidence has unapproved source provenance") from error
    parsed = urlparse(value)
    endpoint = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if expected_url is not None and value != expected_url:
        raise OnspdLifecycleError("ONSPD evidence does not match the fixed request contract")
    if expected_endpoint is not None and endpoint != expected_endpoint:
        raise OnspdLifecycleError("ONSPD evidence does not match the fixed request endpoint")


def _parse_stored_metadata(
    store: OperationalStore,
    artifact: StoredAcquisitionResponse,
    *,
    isolate_parser: bool,
) -> dict[str, Any]:
    if isolate_parser:
        parsed = parse_saved_artifact(
            store.artifacts, artifact.artifact, parse_onspd_layer_metadata_json
        )
    else:
        with store.artifacts.open(artifact.artifact) as saved:
            parsed = parse_onspd_layer_metadata_json(saved.read())
    if not isinstance(parsed, Mapping):
        raise OnspdLifecycleError("ONSPD metadata parser returned an invalid object")
    return dict(parsed)


def _parse_metadata_evidence(
    store: OperationalStore,
    evidence: PersistedEvidence,
    *,
    isolate_parser: bool,
) -> dict[str, Any]:
    if isolate_parser:
        parsed = parse_saved_artifact(
            store.artifacts, evidence.artifact, parse_onspd_layer_metadata_json
        )
    else:
        parsed = parse_onspd_layer_metadata_json(store.read_evidence(evidence))
    if not isinstance(parsed, Mapping):
        raise OnspdLifecycleError("ONSPD metadata parser returned an invalid object")
    return dict(parsed)


def parse_onspd_layer_metadata_json(evidence: bytes) -> dict[str, object]:
    """Parse only the ONSPD metadata fields needed for the fixed query.

    Layer metadata commonly contains map extents expressed as binary floats.
    They are not part of the point-lookup contract and cannot cross the
    parser protocol's canonical-JSON frame, so the parser emits only the
    object-ID declaration and edit timestamp used downstream.
    """

    metadata = parse_arcgis_layer_metadata_json(evidence)
    parsed: dict[str, object] = {}
    for key in ("objectIdField", "objectIdFieldName"):
        value = metadata.get(key)
        if isinstance(value, str):
            parsed[key] = value
    editing_info = metadata.get("editingInfo")
    if isinstance(editing_info, Mapping):
        data_last_edit = editing_info.get("dataLastEditDate")
        if isinstance(data_last_edit, int) and not isinstance(data_last_edit, bool):
            parsed["editingInfo"] = {"dataLastEditDate": data_last_edit}
    return parsed


def _parse_query_evidence(
    store: OperationalStore,
    evidence: PersistedEvidence,
    *,
    isolate_parser: bool,
) -> list[dict[str, object]]:
    if isolate_parser:
        parsed = parse_saved_artifact(
            store.artifacts, evidence.artifact, parse_onspd_feature_page_json
        )
    else:
        parsed = parse_onspd_feature_page_json(store.read_evidence(evidence))
    if not isinstance(parsed, list) or not all(isinstance(record, Mapping) for record in parsed):
        raise OnspdLifecycleError("ONSPD query parser returned invalid feature records")
    return [dict(record) for record in parsed]


def parse_onspd_feature_page_json(evidence: bytes) -> list[dict[str, object]]:
    """Parse one saved ONSPD page into the parser protocol's canonical domain.

    ArcGIS JSON uses binary floats for coordinates.  The parser child emits
    canonical JSON, so decimal source values become strings before crossing
    that boundary; the normalized observation keeps those exact decimal texts.
    """

    return [
        _canonicalise_source_numbers(record)
        for record in parse_arcgis_feature_page_json(evidence)
    ]


def _canonicalise_source_numbers(value: object) -> object:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OnspdLifecycleError("ONSPD JSON contains a non-finite number")
        decimal = Decimal(str(value))
        if decimal.is_zero():
            return "0"
        return format(decimal.normalize(), "f")
    if isinstance(value, Mapping):
        return {str(key): _canonicalise_source_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonicalise_source_numbers(item) for item in value]
    return value


def _layer_contract(metadata: Mapping[str, object]) -> _LayerContract:
    object_id_field = arcgis_object_id_field(metadata)
    if object_id_field is None:
        raise OnspdLifecycleError("ONSPD metadata does not advertise an object ID field")
    try:
        onspd_postcode_query_params("EC2Y 5AS", object_id_field=object_id_field)
    except ValueError as error:
        raise OnspdLifecycleError("ONSPD metadata has an invalid object ID field") from error
    vintage = arcgis_layer_vintage(metadata)
    if vintage is None:
        raise OnspdLifecycleError("ONSPD metadata does not advertise a data vintage")
    try:
        normalised_vintage = _parse_timestamp(vintage).isoformat()
    except OnspdLifecycleError as error:
        raise OnspdLifecycleError("ONSPD metadata has an invalid data vintage") from error
    return _LayerContract(object_id_field, normalised_vintage)


def _normalise_record(
    record: Mapping[str, object],
    *,
    postcode: str,
    layer: _LayerContract,
) -> tuple[dict[str, object], dict[str, object]]:
    returned_postcode = record.get("PCDS")
    if not isinstance(returned_postcode, str) or normalize_postcode(returned_postcode) != postcode:
        raise OnspdLifecycleError("ONSPD feature postcode does not match the requested postcode")
    feature_id = record.get(layer.object_id_field)
    if feature_id is None or isinstance(feature_id, bool):
        raise OnspdLifecycleError("ONSPD feature is missing its requested object ID")
    if record.get("source_feature_id") != feature_id:
        raise OnspdLifecycleError("ONSPD response does not advertise the requested object ID")
    spatial_reference = record.get("spatial_reference")
    if not isinstance(spatial_reference, Mapping):
        raise OnspdLifecycleError("ONSPD feature is missing its spatial reference")
    srid = _srid(spatial_reference)
    if srid != 4326:
        raise OnspdLifecycleError("ONSPD feature did not honor the fixed output SRID")
    geometry = record.get("geometry")
    if not isinstance(geometry, Mapping):
        raise OnspdLifecycleError("ONSPD feature is missing point geometry")
    payload: dict[str, object] = {
        "postcode": postcode,
        "PCDS": postcode,
        "local_authority_district_code": _optional_text(record, "LAD25CD"),
        "ward_code": _optional_text(record, "WD25CD"),
        "output_area_code": _optional_text(record, "OA21CD"),
        "lower_super_output_area_code": _optional_text(record, "LSOA21CD"),
        "middle_super_output_area_code": _optional_text(record, "MSOA21CD"),
        "latitude": record.get("LAT"),
        "longitude": record.get("LONG"),
        "geometry": dict(geometry),
        "srid": srid,
        "vintage": layer.vintage,
    }
    locator: dict[str, object] = {
        "kind": "arcgis_feature",
        "layer": ONSPD_LAYER_URL,
        "feature_id_field": layer.object_id_field,
        "feature_id": feature_id,
        "PCDS": postcode,
        "SRID": srid,
        "vintage": layer.vintage,
    }
    validate_onspd_postcode_record(payload)
    return payload, locator


def onspd_postcode_record_key(record: Mapping[str, object]) -> tuple[str]:
    """Return the stable ONSPD stream key for one normalized postcode record."""

    postcode = record.get("postcode", record.get("PCDS"))
    if not isinstance(postcode, str):
        raise OnspdLifecycleError("normalized ONSPD record requires a postcode")
    return (normalize_postcode(postcode),)


def validate_onspd_postcode_record(record: Mapping[str, object]) -> None:
    """Validate the normalized record shape exposed to the registry binding."""

    postcode = onspd_postcode_record_key(record)[0]
    pcds = record.get("PCDS")
    if not isinstance(pcds, str) or normalize_postcode(pcds) != postcode:
        raise OnspdLifecycleError("normalized ONSPD record PCDS does not match postcode")
    if record.get("srid") != 4326:
        raise OnspdLifecycleError("normalized ONSPD record must use SRID 4326")
    if not isinstance(record.get("geometry"), Mapping):
        raise OnspdLifecycleError("normalized ONSPD record requires point geometry")
    vintage = record.get("vintage")
    if not isinstance(vintage, str):
        raise OnspdLifecycleError("normalized ONSPD record requires a vintage")
    _parse_timestamp(vintage)


def _optional_text(record: Mapping[str, object], key: str) -> str | None:
    value = record.get(key)
    if value is not None and not isinstance(value, str):
        raise OnspdLifecycleError(f"ONSPD feature field {key} must be a string")
    return value


def _srid(spatial_reference: Mapping[str, object]) -> int:
    for key in ("latestWkid", "wkid"):
        value = spatial_reference.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    raise OnspdLifecycleError("ONSPD spatial reference has no integer SRID")


def _metadata_url() -> str:
    return build_url(ONSPD_LAYER_URL, onspd_layer_metadata_params())


def _media_type(headers: Mapping[str, str]) -> str | None:
    for name, value in headers.items():
        if name.lower() == "content-type" and isinstance(value, str):
            return value
    return None


def _has_content_range(headers: Mapping[str, str]) -> bool:
    return any(name.lower() == "content-range" for name in headers)


def _required_retention(
    store: OperationalStore,
    retention_until: datetime | None,
    *,
    definition_version: int | None,
) -> datetime | None:
    policy = store.registry.lookup(
        ONSPD_DATASOURCE_ID, definition_version
    ).retention_policy
    if policy in {"open_official", "internal_config"}:
        if retention_until is not None:
            raise OnspdLifecycleError("ONSPD retention policy does not accept a deadline")
        return None
    if retention_until is None:
        raise OnspdLifecycleError("ONSPD lifecycle requires an approved retention deadline")
    if retention_until.tzinfo is None or retention_until.utcoffset() is None:
        raise OnspdLifecycleError("ONSPD retention deadline must be timezone-aware")
    return retention_until.astimezone(UTC)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise OnspdLifecycleError("ONSPD timestamp must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OnspdLifecycleError("ONSPD timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _clock_utc(clock: Callable[[], datetime]) -> datetime:
    return _parse_timestamp(clock().isoformat())


def _optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OperationalError("ONSPD evidence has an invalid retention deadline")
    return _parse_timestamp(value)


def _finish_failed(
    store: OperationalStore, run: RunHandle, error: Exception, *, at: datetime
) -> None:
    try:
        store.finish_run(
            run,
            status="failed",
            error={
                "schema_version": "error.v1",
                "code": "ONSPD_LOOKUP_FAILED",
                "stage": "parse_or_persist",
                "retryable": False,
                "details": {"exception": type(error).__name__},
            },
            now=at,
        )
    except OperationalError:
        pass
