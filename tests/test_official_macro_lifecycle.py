from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from nan_fung.datasources.common import AcquisitionResponse, StoredAcquisitionResponse
from nan_fung.ingestion.official_macro_lifecycle import (
    OfficialMacroLifecycleError,
    ingest_official_macro_artifact,
    reparse_official_macro_evidence,
)
from nan_fung.ingestion.official_macro_workflow import request_for
from nan_fung.ingestion.registry import DatasourceRegistry, default_registry
from nan_fung.ingestion.policies import PolicyError
from nan_fung.operational import OperationalError, OperationalStore
from nan_fung.storage.db import connect_database


_ONS = b'''{
  "description": {
    "title": "Monthly gross domestic product: Index",
    "unit": "%",
    "releaseDate": "2026-07-21T23:00:00.000Z",
    "monthLabelStyle": "three month average"
  },
  "months": [{"label": "2026 JUN", "value": "2.60", "updateDate": "2026-07-21T23:00:00.000Z"}]
}'''
_NOMIS_LFS = b'''{
  "obs": [{
    "geography": {"description": "London", "geogcode": "E12000007"},
    "time": {"description": "Mar 2026-May 2026", "value": "2026-05"},
    "economic_activity": {"description": "Employment rate"},
    "obs_value": {"value": 73.80},
    "obs_status": {"description": "Normal Value"}
  }]
}'''
_NOMIS_JOBS = b'''{
  "obs": [{
    "geography": {"description": "London", "geogcode": "E12000007"},
    "time": {"description": "March 2026", "value": "2026-03"},
    "item": {"description": "total workforce jobs"},
    "obs_value": {"value": "6466474"},
    "obs_status": {"description": "Normal Value"}
  }]
}'''
_ONS_CONTRACTS = (
    (
        "ons.gdp.ecyx",
        "ECYX",
        "months",
        "/economy/grossdomesticproductgdp/timeseries/ecyx/mgdp",
    ),
    (
        "ons.gdp.ihyq",
        "IHYQ",
        "quarters",
        "/economy/grossdomesticproductgdp/timeseries/ihyq/qna",
    ),
    (
        "ons.inflation.d7g7",
        "D7G7",
        "months",
        "/economy/inflationandpriceindices/timeseries/d7g7/mm23",
    ),
    (
        "ons.inflation.l55o",
        "L55O",
        "months",
        "/economy/inflationandpriceindices/timeseries/l55o/mm23",
    ),
    (
        "ons.inflation.czbh",
        "CZBH",
        "months",
        "/economy/inflationandpriceindices/timeseries/czbh/mm23",
    ),
    (
        "ons.labour.lf24",
        "LF24",
        "months",
        "/employmentandlabourmarket/peopleinwork/employmentandemployeetypes/timeseries/lf24/lms",
    ),
    (
        "ons.labour.mgsx",
        "MGSX",
        "months",
        "/employmentandlabourmarket/peoplenotinwork/unemployment/timeseries/mgsx/lms",
    ),
    (
        "ons.labour.ap2y",
        "AP2Y",
        "months",
        "/employmentandlabourmarket/peopleinwork/employmentandemployeetypes/timeseries/ap2y/lms",
    ),
    (
        "ons.labour.kai9",
        "KAI9",
        "months",
        "/employmentandlabourmarket/peopleinwork/earningsandworkinghours/timeseries/kai9/lms",
    ),
)


def _response(datasource_id: str, body: bytes) -> AcquisitionResponse:
    request = request_for(datasource_id)
    return AcquisitionResponse(
        request_url=request.url,
        final_url=request.url,
        status=200,
        headers={"Content-Type": "application/json"},
        body=body,
        retrieved_at="2026-08-01T00:00:00Z",
        method="GET",
    )


@pytest.mark.parametrize(("datasource_id", "series", "frequency", "uri"), _ONS_CONTRACTS)
def test_each_ons_contract_has_its_own_fixed_uri_frequency_and_lifecycle(
    tmp_path: Path, datasource_id: str, series: str, frequency: str, uri: str
) -> None:
    request = request_for(datasource_id)
    assert parse_qs(urlparse(request.url).query) == {"uri": [uri]}
    period = "2026 Q1" if frequency == "quarters" else "2026 JUN"
    body = json.dumps(
        {
            "description": {"title": series, "unit": "%"},
            frequency: [{"label": period, "value": "2.6"}],
        }
    ).encode()

    result = ingest_official_macro_artifact(
        OperationalStore(tmp_path), datasource_id, _response(datasource_id, body)
    )

    assert result.status == "succeeded"
    assert len(result.observation_ids) == 1


@pytest.mark.parametrize(
    ("datasource_id", "body", "pointer"),
    (
        ("ons.gdp.ecyx", _ONS, "/months/0"),
        ("nomis.nm_59_1.london_lfs", _NOMIS_LFS, "/obs/0"),
        ("nomis.nm_130_1.london_workforce_jobs", _NOMIS_JOBS, "/obs/0"),
    ),
)
def test_official_macro_fixture_lifecycle_persists_before_isolated_parse_and_promotes(
    tmp_path: Path, datasource_id: str, body: bytes, pointer: str
) -> None:
    store = OperationalStore(tmp_path)

    result = ingest_official_macro_artifact(store, datasource_id, _response(datasource_id, body))

    assert result.status == "succeeded"
    assert result.canonical_changed is True
    assert len(result.observation_ids) == 1
    connection = connect_database(store.database_path, read_only=True)
    try:
        evidence = connection.execute(
            "SELECT source_id, content_sha256 FROM evidence_artifact WHERE evidence_id = ?",
            (result.evidence_id,),
        ).fetchone()
        locator = connection.execute(
            "SELECT locator_json FROM observation_evidence WHERE observation_id = ?",
            (result.observation_ids[0],),
        ).fetchone()
        canonical = connection.execute(
            "SELECT observation_id FROM canonical_latest_v1 WHERE observation_id = ?",
            (result.observation_ids[0],),
        ).fetchone()
    finally:
        connection.close()

    assert evidence is not None
    assert evidence["source_id"] == request_for(datasource_id).source_id
    assert locator is not None
    assert json.loads(locator["locator_json"])["record_locator"]["pointer"] == pointer
    assert canonical is not None
    assert store.artifacts.object_path(evidence["content_sha256"]).is_file()


def test_stored_macro_artifact_is_referenced_without_a_second_cas_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasource_id = "ons.gdp.ecyx"
    store = OperationalStore(tmp_path)
    request = request_for(datasource_id)
    stored = store.artifacts.put_bytes(_ONS, media_type="application/json")
    monkeypatch.setattr(
        store.artifacts,
        "put_bytes",
        lambda *_args, **_kwargs: pytest.fail("stored macro artifact must not be written twice"),
    )

    result = ingest_official_macro_artifact(
        store,
        datasource_id,
        StoredAcquisitionResponse(
            request_url=request.url,
            final_url=request.url,
            status=200,
            headers={"Content-Type": "application/json"},
            artifact=stored,
            retrieved_at="2026-08-01T00:00:00Z",
            method="GET",
        ),
    )

    assert result.status == "succeeded"
    assert store.read_evidence(result.evidence_id) == _ONS


def test_official_macro_reparse_reuses_saved_evidence_without_network(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    original = ingest_official_macro_artifact(store, "ons.gdp.ecyx", _response("ons.gdp.ecyx", _ONS))

    replay = reparse_official_macro_evidence(
        store, "ons.gdp.ecyx", original.evidence_id
    )

    assert replay.status == "succeeded"
    assert replay.observation_ids == original.observation_ids


def test_official_macro_reparse_rejects_evidence_from_another_datasource(
    tmp_path: Path,
) -> None:
    store = OperationalStore(tmp_path)
    original = ingest_official_macro_artifact(store, "ons.gdp.ecyx", _response("ons.gdp.ecyx", _ONS))

    with pytest.raises(OperationalError, match="official-macro provenance"):
        reparse_official_macro_evidence(
            store, "nomis.nm_59_1.london_lfs", original.evidence_id
        )


def test_official_macro_reparse_preserves_the_captured_definition_version(
    tmp_path: Path,
) -> None:
    seed = default_registry()
    version_one = seed.lookup("ons.gdp.ecyx")
    version_two = replace(version_one, definition_version=2)
    store = OperationalStore(
        tmp_path,
        registry=DatasourceRegistry(
            (version_one, version_two), (seed.lookup_source("ons.data_api"),)
        ),
    )
    original = ingest_official_macro_artifact(
        store,
        "ons.gdp.ecyx",
        _response("ons.gdp.ecyx", _ONS),
        definition_version=2,
    )

    replay = reparse_official_macro_evidence(
        store, "ons.gdp.ecyx", original.evidence_id
    )

    connection = connect_database(store.database_path, read_only=True)
    try:
        lineage = connection.execute(
            """
            SELECT r.definition_version, j.trigger, j.job_kind, j.request_json
            FROM ingestion_run AS r
            JOIN workflow_job AS j ON j.job_id = r.job_id
            WHERE r.run_id = ?
            """,
            (replay.run_id,),
        ).fetchone()
    finally:
        connection.close()
    assert lineage is not None
    assert lineage["definition_version"] == 2
    assert lineage["trigger"] == "reparse"
    assert lineage["job_kind"] == "offline_reparse"
    assert json.loads(lineage["request_json"]) == {
        "reparse_evidence_id": original.evidence_id
    }


@pytest.mark.parametrize(
    ("captured_policy", "latest_policy", "canonical_changed"),
    (
        ("automatic", "manual_review", True),
        ("manual_review", "automatic", False),
    ),
)
def test_official_macro_promotion_uses_the_captured_definition_policy(
    tmp_path: Path,
    captured_policy: str,
    latest_policy: str,
    canonical_changed: bool,
) -> None:
    seed = default_registry()
    original = replace(
        seed.lookup("ons.gdp.ecyx"),
        definition_version=1,
        promotion_policy=captured_policy,
    )
    latest = replace(
        original,
        definition_version=2,
        promotion_policy=latest_policy,
    )
    store = OperationalStore(
        tmp_path,
        registry=DatasourceRegistry(
            (original, latest), (seed.lookup_source("ons.data_api"),)
        ),
    )
    queued = store.enqueue(
        "ons.gdp.ecyx",
        definition_version=1,
        request={"url": request_for("ons.gdp.ecyx").url},
    )
    claim = store.claim_job(queued.job_id, "worker")
    assert claim is not None
    run = store.start_run(claim, "worker")

    result = ingest_official_macro_artifact(
        store,
        "ons.gdp.ecyx",
        _response("ons.gdp.ecyx", _ONS),
        existing_run=run,
        isolate_parser=False,
    )

    assert result.canonical_changed is canonical_changed
    connection = connect_database(store.database_path, read_only=True)
    try:
        promotion = connection.execute(
            "SELECT decision FROM run_promotion WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
    finally:
        connection.close()
    assert (promotion is not None) is canonical_changed


def test_official_macro_refuses_a_definition_with_changed_executable_bindings(
    tmp_path: Path,
) -> None:
    seed = default_registry()
    version_one = seed.lookup("ons.gdp.ecyx")
    changed = replace(version_one, definition_version=2, parser_version="v2")
    store = OperationalStore(
        tmp_path,
        registry=DatasourceRegistry(
            (version_one, changed), (seed.lookup_source("ons.data_api"),)
        ),
    )

    with pytest.raises(OfficialMacroLifecycleError, match="new bound lifecycle"):
        ingest_official_macro_artifact(
            store,
            "ons.gdp.ecyx",
            _response("ons.gdp.ecyx", _ONS),
            definition_version=2,
        )

    assert not (tmp_path / "evidence").exists()


@pytest.mark.parametrize("lane", ("source_discovery", "ad_hoc_research"))
def test_official_macro_reparse_inherits_its_captured_nonproduction_lane(
    tmp_path: Path, lane: str
) -> None:
    store = OperationalStore(tmp_path)
    original = ingest_official_macro_artifact(
        store,
        "ons.gdp.ecyx",
        _response("ons.gdp.ecyx", _ONS),
        lane=lane,
    )

    replay = reparse_official_macro_evidence(
        store, "ons.gdp.ecyx", original.evidence_id
    )

    assert replay.canonical_changed is False
    with pytest.raises(OperationalError, match="must match the source lane"):
        reparse_official_macro_evidence(
            store,
            "ons.gdp.ecyx",
            original.evidence_id,
            lane="production_ingestion",
        )


def test_restricted_mpc_feed_remains_blocked_before_evidence_write(tmp_path: Path) -> None:
    store = OperationalStore(tmp_path)
    request = request_for("boe.mpc_news")
    artifact = AcquisitionResponse(
        request_url=request.url,
        final_url=request.url,
        status=200,
        headers={"Content-Type": "application/rss+xml"},
        body=b"<rss><channel /></rss>",
        retrieved_at="2026-08-01T00:00:00Z",
        method="GET",
    )

    with pytest.raises(OfficialMacroLifecycleError, match="not an approved automatic"):
        ingest_official_macro_artifact(store, "boe.mpc_news", artifact)

    assert not (tmp_path / "evidence").exists()


@pytest.mark.parametrize(
    ("headers", "body", "message"),
    (
        ({"Content-Type": "text/html"}, _ONS, "media type is not allowed"),
        ({"Content-Type": "application/json"}, b"x" * (4 * 1024 * 1024 + 1), "exceeds"),
    ),
)
def test_official_macro_fixtures_obey_the_same_source_artifact_policy_as_live(
    tmp_path: Path, headers: dict[str, str], body: bytes, message: str
) -> None:
    datasource_id = "ons.gdp.ecyx"
    request = request_for(datasource_id)
    artifact = AcquisitionResponse(
        request_url=request.url,
        final_url=request.url,
        status=200,
        headers=headers,
        body=body,
        retrieved_at="2026-08-01T00:00:00Z",
        method="GET",
    )

    with pytest.raises(PolicyError, match=message):
        ingest_official_macro_artifact(
            OperationalStore(tmp_path), datasource_id, artifact, isolate_parser=False
        )

    assert not (tmp_path / "evidence").exists()
