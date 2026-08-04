from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from io import BytesIO
import json
from pathlib import Path
import re
import sqlite3
from zipfile import ZipFile

from nan_fung.agent_tools import AgentToolFacade
from nan_fung.datasources.common import AcquisitionResponse
from nan_fung.ingestion.file_release_lifecycle import ingest_file_release_artifacts
from nan_fung.ingestion.file_release_workflow import VOA_DATASOURCE_ID, contract_for
from nan_fung.operational import OperationalStore
from nan_fung.read_api import ReadService, SQLiteReadRepository


NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
_BANK_RATE_URL = "https://www.bankofengland.co.uk/boeapps/database/offline.csv"
_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "agent_tools" / "v1"


def _store(tmp_path: Path) -> OperationalStore:
    return OperationalStore(tmp_path)


def _seed_bank_rate(store: OperationalStore) -> None:
    queued = store.enqueue(
        "boe.bank_rate.iudbedr",
        scheduled_for=NOW,
        request={"fixture": "finalizer-contract"},
    )
    claim = store.claim_job(queued.job_id, "worker", now=NOW)
    assert claim is not None
    run = store.start_run(claim, "worker", now=NOW)
    evidence = store.persist_evidence(
        run,
        b"DATE,IUDBEDR\n30 Jul 2026,3.75\n",
        media_type="text/csv",
        request={"method": "GET", "url": _BANK_RATE_URL},
        response={"status": 200, "final_url": _BANK_RATE_URL},
        retrieved_at=NOW,
        now=NOW,
    )
    store.persist_observation(
        run,
        record_key=("IUDBEDR", "2026-07-30"),
        payload={"bank_rate_percent": "3.75"},
        record_type="metric",
        category="macro",
        evidence=(evidence,),
        locator={"kind": "csv_row", "row_key": "2026-07-30"},
        source_date="2026-07-30",
        unit="percent",
        definition_text="Official Bank Rate",
        now=NOW,
    )
    store.finish_run(run, status="succeeded", promote=True, now=NOW)


def _query_bank_rate_request():
    requests = json.loads((_FIXTURE_PATH / "requests.json").read_text(encoding="utf-8"))
    request = deepcopy(requests["query_bank_rate"])
    assert isinstance(request, dict)
    arguments = request["arguments"]
    assert isinstance(arguments, dict)
    arguments["limit"] = 1
    return request


def _voa_artifacts() -> tuple[AcquisitionResponse, AcquisitionResponse]:
    release_url = "https://assets.publishing.service.gov.uk/media/example/ndr_stock_of_properties_2026.zip"
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        header = "geography,area_code,area_name,2025,2026\n"
        archive.writestr("table_SOP5_1.csv", header + "REGL,E12000007,London,100,103400\n")
        archive.writestr("table_SOP5_2.csv", header + "REGL,E12000007,London,900,9264908\n")
    discovery = AcquisitionResponse(
        request_url=contract_for(VOA_DATASOURCE_ID).discovery_url,
        final_url=contract_for(VOA_DATASOURCE_ID).discovery_url,
        status=200,
        headers={"Content-Type": "text/html"},
        body=f'<a href="{release_url}">release</a>'.encode(),
        retrieved_at="2026-08-01T00:00:00Z",
        method="GET",
    )
    release = AcquisitionResponse(
        request_url=release_url,
        final_url=release_url,
        status=200,
        headers={"Content-Type": "application/zip"},
        body=output.getvalue(),
        retrieved_at="2026-08-01T00:00:00Z",
        method="GET",
    )
    return discovery, release


def test_bank_rate_facade_projects_decimal_numeric_contract(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_bank_rate(store)
    facade = AgentToolFacade(
        read_service=ReadService(
            SQLiteReadRepository(store.database_path),
            cursor_secret=b"finalizer-contract-read-secret",
            clock=lambda: NOW,
        ),
        citation_projection=SQLiteReadRepository(store.database_path),
        handle_secret=b"f" * 32,
        clock=lambda: NOW,
    )

    result = facade.execute("query_market_data", _query_bank_rate_request())

    assert result["status"] == "ok"
    data = result["data"]
    assert isinstance(data, dict)
    records = data["records"]
    assert isinstance(records, list)
    numeric = records[0]["numeric"]
    assert isinstance(numeric, dict)
    assert isinstance(numeric["source_date"], str)
    assert re.fullmatch(r"[0-9.]+", numeric["value"])
    assert isinstance(numeric["unit"], str)
    assert isinstance(numeric["definition"], str)


def test_voa_file_release_persists_finalizer_source_date_and_decimal_strings(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    discovery, release = _voa_artifacts()
    result = ingest_file_release_artifacts(
        store,
        VOA_DATASOURCE_ID,
        discovery=discovery,
        release=release,
    )

    connection = sqlite3.connect(store.database_path)
    try:
        canonical = connection.execute(
            "SELECT source_date, payload_json FROM canonical_latest_v1 WHERE observation_id = ?",
            (result.observation_ids[0],),
        ).fetchone()
    finally:
        connection.close()
    assert canonical is not None
    source_date, payload_json = canonical
    assert isinstance(source_date, str)
    assert source_date
    payload = json.loads(payload_json)
    assert isinstance(payload["office_property_count"], str)
    assert re.fullmatch(r"[0-9]+", payload["office_property_count"])
