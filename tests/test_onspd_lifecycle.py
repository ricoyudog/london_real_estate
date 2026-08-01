from __future__ import annotations

import json
from dataclasses import replace
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from nan_fung.datasources.common import (
    AcquisitionMetadata,
    AcquisitionResponse,
    StoredAcquisitionResponse,
    build_url,
)
from nan_fung.datasources.geography import (
    ONSPD_LAYER_URL,
    ONSPD_QUERY_URL,
    onspd_layer_metadata_params,
    onspd_postcode_query_params,
)
from nan_fung.ingestion.onspd_lifecycle import (
    ONSPD_DATASOURCE_ID,
    OnspdArtifacts,
    OnspdLifecycleError,
    acquire_live_onspd_postcode,
    ingest_onspd_postcode_artifacts,
    onspd_postcode_record_key,
    parse_onspd_feature_page_json,
    reparse_onspd_postcode_evidence,
    validate_onspd_postcode_record,
)
from nan_fung.ingestion.registry import DatasourceRegistry, default_registry
from nan_fung.ingestion.registry import (
    DatasourceRegistry,
    default_registry,
    default_runtime_bindings,
)
from nan_fung.operational import OperationalStore, RunHandle
from nan_fung.storage.db import connect_database


_AT = datetime(2026, 8, 1, 12, tzinfo=UTC)
_RETENTION = datetime(2030, 1, 1, tzinfo=UTC)
_POSTCODE = "EC2Y 5AS"
_METADATA = json.dumps(
    {
        "objectIdField": "OBJECTID",
        "editingInfo": {"dataLastEditDate": 1781277038010},
        "extent": {"xmin": -8.2, "ymin": 49.8},
    }
).encode()
_QUERY = json.dumps(
    {
        "objectIdFieldName": "OBJECTID",
        "spatialReference": {"wkid": 4326},
        "features": [
            {
                "attributes": {
                    "OBJECTID": 7,
                    "PCDS": _POSTCODE,
                    "LAD25CD": "E09000001",
                    "WD25CD": "E05000649",
                    "OA21CD": "E00000001",
                    "LSOA21CD": "E01000001",
                    "MSOA21CD": "E02000001",
                    "LAT": 51.52,
                    "LONG": -0.09,
                },
                "geometry": {"x": -0.09, "y": 51.52},
            }
        ],
    }
).encode()


def _clock() -> datetime:
    return _AT


def _metadata_url() -> str:
    return build_url(ONSPD_LAYER_URL, onspd_layer_metadata_params())


def _query_url() -> str:
    return build_url(
        ONSPD_QUERY_URL,
        onspd_postcode_query_params(_POSTCODE, object_id_field="OBJECTID"),
    )


def _response(url: str, body: bytes) -> AcquisitionResponse:
    return AcquisitionResponse(
        request_url=url,
        final_url=url,
        status=200,
        headers={"Content-Type": "application/json"},
        body=body,
        retrieved_at="2026-08-01T12:00:00Z",
        method="GET",
    )


def _artifacts() -> OnspdArtifacts:
    return OnspdArtifacts(
        _POSTCODE,
        _response(_metadata_url(), _METADATA),
        _response(_query_url(), _QUERY),
    )


def _runtime_bindings():
    bindings = default_runtime_bindings()
    definition = default_registry().lookup(ONSPD_DATASOURCE_ID)
    for descriptor, callable_ in (
        (definition.collector_binding, acquire_live_onspd_postcode),
        (definition.parser_binding, parse_onspd_feature_page_json),
        (definition.record_key_binding, onspd_postcode_record_key),
    ):
        if not bindings.contains(descriptor):
            bindings.register(
                descriptor.kind,
                descriptor.name,
                descriptor.version,
                callable_,
            )
    for descriptor in definition.validator_bindings:
        if not bindings.contains(descriptor):
            bindings.register(
                descriptor.kind,
                descriptor.name,
                descriptor.version,
                validate_onspd_postcode_record,
            )
    return bindings


def _store(tmp_path, *, registry=None) -> OperationalStore:
    return OperationalStore(
        tmp_path,
        registry=registry,
        runtime_bindings=_runtime_bindings(),
    )


def _active_run(store: OperationalStore, *, lane: str) -> RunHandle:
    queued = store.enqueue(
        ONSPD_DATASOURCE_ID,
        request={"postcode": _POSTCODE},
        trigger="manual",
        lane=lane,
        scheduled_for=_AT,
        request_instance_id=f"onspd-{lane}",
    )
    claim = store.claim_job(queued.job_id, "test-worker", now=_AT)
    assert claim is not None
    return store.start_run(claim, "test-worker", now=_AT)


def test_fixture_lifecycle_sandboxes_parse_and_keeps_nonproduction_noncanonical(tmp_path) -> None:
    store = _store(tmp_path)

    result = ingest_onspd_postcode_artifacts(
        store,
        _artifacts(),
        lane="source_discovery",
        clock=_clock,
        retention_until=_RETENTION,
    )

    assert result.status == "succeeded"
    assert result.canonical_changed is False
    assert len(result.observation_ids) == 1
    connection = connect_database(store.database_path, read_only=True)
    try:
        canonical = connection.execute(
            "SELECT observation_id FROM canonical_latest_v1 WHERE observation_id = ?",
            (result.observation_ids[0],),
        ).fetchone()
        locators = connection.execute(
            "SELECT locator_json FROM observation_evidence WHERE observation_id = ?",
            (result.observation_ids[0],),
        ).fetchall()
    finally:
        connection.close()

    assert canonical is None
    assert len(locators) == 2
    locator = json.loads(locators[0]["locator_json"])["record_locator"]
    assert locator == {
        "kind": "arcgis_feature",
        "layer": ONSPD_LAYER_URL,
        "feature_id_field": "OBJECTID",
        "feature_id": 7,
        "PCDS": _POSTCODE,
        "SRID": 4326,
        "vintage": "2026-06-12T15:10:38.010000+00:00",
    }


def test_production_requires_a_passed_active_run_and_promotes_only_that_run(tmp_path) -> None:
    store = _store(tmp_path)

    with pytest.raises(OnspdLifecycleError, match="requires an active run"):
        ingest_onspd_postcode_artifacts(store, _artifacts(), clock=_clock)

    assert not (tmp_path / "evidence").exists()
    active_run = _active_run(store, lane="production_ingestion")
    result = ingest_onspd_postcode_artifacts(
        store,
        _artifacts(),
        active_run=active_run,
        clock=_clock,
        retention_until=_RETENTION,
    )

    assert result.canonical_changed is True
    connection = connect_database(store.database_path, read_only=True)
    try:
        canonical = connection.execute(
            "SELECT observation_id FROM canonical_latest_v1 WHERE observation_id = ?",
            (result.observation_ids[0],),
        ).fetchone()
    finally:
        connection.close()
    assert canonical is not None


def test_fixture_rejects_unapproved_query_provenance_before_evidence_write(tmp_path) -> None:
    store = _store(tmp_path)
    invalid = OnspdArtifacts(
        _POSTCODE,
        _response(_metadata_url(), _METADATA),
        AcquisitionResponse(
            request_url="https://example.invalid/query?f=json",
            final_url="https://example.invalid/query?f=json",
            status=200,
            headers={"Content-Type": "application/json"},
            body=_QUERY,
            retrieved_at="2026-08-01T12:00:00Z",
            method="GET",
        ),
    )

    with pytest.raises(OnspdLifecycleError, match="unapproved source provenance"):
        ingest_onspd_postcode_artifacts(
            store,
            invalid,
            lane="source_discovery",
            clock=_clock,
            retention_until=_RETENTION,
        )

    assert not (tmp_path / "evidence").exists()


def test_reparse_uses_only_saved_evidence_without_live_acquisition(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    original = ingest_onspd_postcode_artifacts(
        store,
        _artifacts(),
        lane="source_discovery",
        clock=_clock,
        retention_until=_RETENTION,
    )
    monkeypatch.setattr(
        "nan_fung.ingestion.onspd_lifecycle.acquire_to_artifact",
        lambda *_args, **_kwargs: pytest.fail("offline replay must not acquire live data"),
    )

    replay = reparse_onspd_postcode_evidence(
        store,
        original.query_evidence_id,
        clock=_clock,
    )

    assert replay.status == "succeeded"
    assert replay.canonical_changed is False
    assert replay.observation_ids == original.observation_ids


def test_production_reparse_creates_an_offline_run_and_preserves_canonical_data(
    tmp_path,
) -> None:
    store = OperationalStore(tmp_path)
    original = ingest_onspd_postcode_artifacts(
        store,
        _artifacts(),
        active_run=_active_run(store, lane="production_ingestion"),
        clock=_clock,
        retention_until=_RETENTION,
    )

    replay = reparse_onspd_postcode_evidence(
        store,
        original.query_evidence_id,
        clock=_clock,
    )

    assert replay.status == "succeeded"
    assert replay.canonical_changed is True
    assert replay.observation_ids == original.observation_ids
    assert replay.run_id != original.run_id


def test_onspd_refuses_a_definition_with_changed_executable_bindings(tmp_path) -> None:
    seed = default_registry()
    version_one = seed.lookup(ONSPD_DATASOURCE_ID)
    changed = replace(version_one, definition_version=2, parser_version="v2")
    store = OperationalStore(
        tmp_path,
        registry=DatasourceRegistry(
            (version_one, changed), (seed.lookup_source("ons.onspd"),)
        ),
    )

    with pytest.raises(OnspdLifecycleError, match="new bound lifecycle"):
        ingest_onspd_postcode_artifacts(
            store,
            _artifacts(),
            lane="source_discovery",
            definition_version=2,
            clock=_clock,
            retention_until=_RETENTION,
        )

    assert not (tmp_path / "evidence").exists()


def test_reparse_preserves_the_captured_definition_version(tmp_path) -> None:
    seed = default_registry()
    version_one = seed.lookup(ONSPD_DATASOURCE_ID)
    version_two = replace(version_one, definition_version=2)
    store = _store(
        tmp_path,
        registry=DatasourceRegistry(
            (version_one, version_two),
            (seed.lookup_source("ons.onspd"),),
        ),
    )
    original = ingest_onspd_postcode_artifacts(
        store,
        _artifacts(),
        lane="source_discovery",
        definition_version=2,
        clock=_clock,
        retention_until=_RETENTION,
    )

    replay = reparse_onspd_postcode_evidence(
        store,
        original.query_evidence_id,
        clock=_clock,
    )

    connection = connect_database(store.database_path, read_only=True)
    try:
        version = connection.execute(
            "SELECT definition_version FROM ingestion_run WHERE run_id = ?",
            (replay.run_id,),
        ).fetchone()
    finally:
        connection.close()
    assert version is not None
    assert version["definition_version"] == 2


def test_live_capture_is_fixed_to_metadata_derived_object_id_without_real_network(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    active_run = _active_run(store, lane="production_ingestion")
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_acquire(url, params=None, **kwargs):
        assert kwargs["policy"]
        assert kwargs["resolver"] is resolver
        request_url = build_url(url, params)
        metadata = AcquisitionMetadata(
            request_url=request_url,
            final_url=request_url,
            status=200,
            headers={"Content-Type": "application/json"},
            retrieved_at="2026-08-01T12:00:00Z",
            method="GET",
        )
        kwargs["before_publish"](metadata)
        calls.append((url, dict(params or {})))
        body = _METADATA if url == ONSPD_LAYER_URL else _QUERY
        stored = kwargs["artifact_store"].put_bytes(
            body, media_type="application/json"
        )
        return StoredAcquisitionResponse(
            request_url=request_url,
            final_url=request_url,
            status=200,
            headers={"Content-Type": "application/json"},
            artifact=stored,
            retrieved_at="2026-08-01T12:00:00Z",
            method="GET",
        )

    def resolver(host: str):
        pytest.fail(f"fake acquisition should not resolve {host}")

    monkeypatch.setattr(
        "nan_fung.ingestion.onspd_lifecycle.acquire_to_artifact", fake_acquire
    )

    captured = acquire_live_onspd_postcode(
        store,
        _POSTCODE,
        active_run=active_run,
        resolver=resolver,
        clock=_clock,
        retention_until=_RETENTION,
    )

    assert [url for url, _params in calls] == [ONSPD_LAYER_URL, ONSPD_QUERY_URL]
    assert calls[1][1]["where"] == "PCDS='EC2Y 5AS'"
    assert "OBJECTID" in calls[1][1]["outFields"].split(",")
    result = ingest_onspd_postcode_artifacts(
        store,
        captured,
        active_run=active_run,
        clock=_clock,
        retention_until=_RETENTION,
    )
    assert result.canonical_changed is True
