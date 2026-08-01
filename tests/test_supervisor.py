from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zipfile import ZipFile

import pytest

from nan_fung.datasources import common
from nan_fung.datasources.common import AcquisitionResponse, build_url
from nan_fung.datasources.geography import (
    ONSPD_LAYER_URL,
    ONSPD_QUERY_URL,
    onspd_layer_metadata_params,
    onspd_postcode_query_params,
)
from nan_fung.ingestion.bank_rate import AcquiredArtifact
from nan_fung.ingestion.file_release_lifecycle import FileReleaseCapture
from nan_fung.ingestion.file_release_workflow import VOA_DATASOURCE_ID, contract_for
from nan_fung.ingestion.onspd_lifecycle import ONSPD_DATASOURCE_ID, OnspdArtifacts
from nan_fung.ingestion.official_macro_workflow import request_for
from nan_fung.operational import OperationalStore
from nan_fung.storage.artifacts import StoredArtifact
from nan_fung.storage.db import connect_database
from nan_fung.supervisor import DatasourceSupervisor
from nan_fung.workflows import ingest_bank_rate_artifact


def _voa_zip() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        header = "geography,area_code,area_name,2026\n"
        archive.writestr(
            "table_SOP5_1.csv", header + "REGL,E12000007,London,103400\n"
        )
        archive.writestr(
            "table_SOP5_2.csv", header + "REGL,E12000007,London,9264908\n"
        )
    return output.getvalue()


def _onspd_artifacts(postcode: str = "EC2Y 5AS") -> OnspdArtifacts:
    metadata_url = build_url(ONSPD_LAYER_URL, onspd_layer_metadata_params())
    query_url = build_url(
        ONSPD_QUERY_URL,
        onspd_postcode_query_params(postcode, object_id_field="OBJECTID"),
    )
    return OnspdArtifacts(
        postcode,
        AcquisitionResponse(
            request_url=metadata_url,
            final_url=metadata_url,
            status=200,
            headers={"Content-Type": "application/json"},
            body=(
                b'{"objectIdField":"OBJECTID",'
                b'"editingInfo":{"dataLastEditDate":1781277038010}}'
            ),
            retrieved_at="2026-08-01T20:00:00Z",
            method="GET",
        ),
        AcquisitionResponse(
            request_url=query_url,
            final_url=query_url,
            status=200,
            headers={"Content-Type": "application/json"},
            body=(
                b'{"objectIdFieldName":"OBJECTID","spatialReference":{"wkid":4326},'
                b'"features":[{"attributes":{"OBJECTID":7,"PCDS":"EC2Y 5AS",'
                b'"LAD25CD":"E09000001","WD25CD":"E05000649",'
                b'"OA21CD":"E00000001","LSOA21CD":"E01000001",'
                b'"MSOA21CD":"E02000001","LAT":51.52,"LONG":-0.09},'
                b'"geometry":{"x":-0.09,"y":51.52}}]}'
            ),
            retrieved_at="2026-08-01T20:00:00Z",
            method="GET",
        ),
    )


def test_supervisor_claims_and_completes_bank_rate_job(tmp_path: Path) -> None:
    now = datetime(2026, 8, 1, 20, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    queued = store.enqueue(
        "boe.bank_rate.iudbedr",
        request={"series": "IUDBEDR"},
        scheduled_for=now,
    )
    artifact = AcquiredArtifact(
        body=b"DATE,IUDBEDR\n31 Jul 2026,3.75\n",
        source_url="https://www.bankofengland.co.uk/data.csv",
        retrieved_at=now,
    )
    supervisor = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        bank_rate_collector=lambda _request: artifact,
    )

    result = supervisor.run_once(now=now)

    assert result.job_id == queued.job_id
    assert result.state == "succeeded"
    assert store.get_job(queued.job_id)["state"] == "succeeded"  # type: ignore[index]


def test_supervisor_executes_a_durable_projection_delivery_system_job(tmp_path: Path) -> None:
    now = datetime(2026, 8, 1, 20, tzinfo=UTC)
    store = OperationalStore(tmp_path / "state")
    ingest_bank_rate_artifact(
        store,
        AcquiredArtifact(
            body=b"DATE,IUDBEDR\n31 Jul 2026,3.75\n",
            source_url="https://www.bankofengland.co.uk/data.csv",
            retrieved_at=now,
        ),
        execution_at=now,
        isolate_parser=False,
    )
    queued = store.enqueue_projection_delivery(
        tmp_path / "published", as_of_at=now
    )

    result = DatasourceSupervisor(store, worker_id="test-worker").run_once(now=now)

    assert result.job_id == queued.job_id
    assert result.state == "succeeded"
    assert store.get_job(queued.job_id)["state"] == "succeeded"  # type: ignore[index]
    assert (tmp_path / "published" / "wiki" / "market.md").is_file()
    connection = connect_database(store.database_path, read_only=True)
    try:
        count = connection.execute("SELECT count(*) FROM output_artifact").fetchone()[0]
    finally:
        connection.close()
    assert count == 5


def test_supervisor_run_until_stops_between_ticks_and_records_shutdown(
    tmp_path: Path,
) -> None:
    store = OperationalStore(tmp_path)
    stopped = [False]
    waits: list[float] = []

    def wait(seconds: float) -> None:
        waits.append(seconds)
        stopped[0] = True

    result = DatasourceSupervisor(store, worker_id="test-worker").run_until(
        should_stop=lambda: stopped[0],
        wait=wait,
        poll_interval_seconds=12.5,
    )

    assert result.tick_count == 1
    assert result.last_state == "idle"
    assert result.shutdown_state == "stopping"
    assert waits == [12.5]
    connection = connect_database(store.database_path, read_only=True)
    try:
        heartbeat = connection.execute(
            "SELECT state FROM service_heartbeat WHERE instance_id = ?",
            ("test-worker",),
        ).fetchone()
    finally:
        connection.close()
    assert heartbeat["state"] == "stopping"


def test_supervisor_rejects_a_poll_interval_that_can_outlive_its_heartbeat(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="120"):
        DatasourceSupervisor(
            OperationalStore(tmp_path), worker_id="test-worker"
        ).run_until(
            should_stop=lambda: True,
            wait=lambda _seconds: None,
            poll_interval_seconds=120.1,
        )


def test_supervisor_live_bank_rate_streams_to_cas_without_put_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    now = datetime(2026, 8, 1, 20, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    queued = store.enqueue(
        "boe.bank_rate.iudbedr",
        request={"series": "IUDBEDR"},
        scheduled_for=now,
    )

    class Response:
        status = 200
        headers = {"Content-Type": "text/csv"}

        def __init__(self) -> None:
            self._chunks = iter((b"DATE,IUDBEDR\n", b"31 Jul 2026,3.75\n"))

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            return None

        def read(self, _size: int) -> bytes:
            return next(self._chunks, b"")

        def getcode(self) -> int:
            return self.status

        def geturl(self) -> str:
            return "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&Datefrom=01%2FJan%2F2025&Dateto=now&SeriesCodes=IUDBEDR&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N"

    monkeypatch.setattr(common, "_open_once", lambda _request, _timeout: Response())
    monkeypatch.setattr(
        store.artifacts,
        "put_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("live Bank Rate must use the stored-artifact path")
        ),
    )
    persisted_artifacts: list[StoredArtifact | None] = []
    original_persist = store.persist_evidence

    def capture_persist(run, body=None, **kwargs):
        persisted_artifacts.append(kwargs.get("artifact"))
        return original_persist(run, body, **kwargs)

    monkeypatch.setattr(store, "persist_evidence", capture_persist)

    result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        resolver=lambda _host: ("8.8.8.8",),
        allow_network=True,
    ).run_once(now=now)

    assert result.job_id == queued.job_id
    assert result.state == "succeeded"
    assert len(persisted_artifacts) == 1
    assert isinstance(persisted_artifacts[0], StoredArtifact)
    connection = connect_database(store.database_path, read_only=True)
    try:
        evidence = connection.execute(
            "SELECT evidence_id FROM run_evidence WHERE run_id = ?", (result.run_id,)
        ).fetchone()
    finally:
        connection.close()
    assert evidence is not None
    assert store.read_evidence(evidence["evidence_id"]) == (
        b"DATE,IUDBEDR\n31 Jul 2026,3.75\n"
    )


def test_supervisor_allows_a_same_host_redirect_with_the_real_throttle_gate(
    tmp_path: Path, monkeypatch
) -> None:
    now = datetime(2026, 8, 1, 20, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    queued = store.enqueue("boe.bank_rate.iudbedr", scheduled_for=now)
    opened: list[str] = []

    class Response:
        def __init__(
            self, final_url: str, *, status: int, headers: dict[str, str], body: bytes = b""
        ) -> None:
            self.status = status
            self.headers = headers
            self._final_url = final_url
            self._body = body
            self._read = False

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            return None

        def read(self, _size: int) -> bytes:
            if self._read:
                return b""
            self._read = True
            return self._body

        def getcode(self) -> int:
            return self.status

        def geturl(self) -> str:
            return self._final_url

    def open_once(request, _timeout: int) -> Response:
        opened.append(request.full_url)
        if len(opened) == 1:
            return Response(
                request.full_url,
                status=302,
                headers={
                    "Location": (
                        "https://WWW.BankOfEngland.Co.Uk./boeapps/database/"
                        "_iadb-fromshowcolumns.asp"
                    )
                },
            )
        return Response(
            request.full_url,
            status=200,
            headers={"Content-Type": "text/csv"},
            body=b"DATE,IUDBEDR\n31 Jul 2026,3.75\n",
        )

    monkeypatch.setattr(common, "_open_once", open_once)

    result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        resolver=lambda _host: ("8.8.8.8",),
        allow_network=True,
    ).run_once(now=now)

    assert result.job_id == queued.job_id
    assert result.state == "succeeded"
    assert len(opened) == 2


def test_supervisor_bank_rate_backfill_pushes_its_window_to_url_and_records(
    tmp_path: Path, monkeypatch
) -> None:
    start = datetime(2025, 1, 5, tzinfo=UTC)
    end = datetime(2025, 1, 6, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    queued = store.enqueue_backfill(
        "boe.bank_rate.iudbedr", window_start=start, window_end=end
    )
    requested_urls: list[str] = []

    class Response:
        status = 200
        headers = {"Content-Type": "text/csv"}

        def __init__(self, final_url: str) -> None:
            self._final_url = final_url
            self._chunks = iter(
                (
                    b"DATE,IUDBEDR\n",
                    b"05 Jan 2025,4.75\n06 Jan 2025,4.75\n",
                )
            )

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            return None

        def read(self, _size: int) -> bytes:
            return next(self._chunks, b"")

        def getcode(self) -> int:
            return self.status

        def geturl(self) -> str:
            return self._final_url

    def open_once(request, _timeout: int) -> Response:
        requested_urls.append(request.full_url)
        return Response(request.full_url)

    monkeypatch.setattr(common, "_open_once", open_once)

    result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        resolver=lambda _host: ("8.8.8.8",),
        allow_network=True,
    ).run_once(now=end)

    assert result.job_id == queued.job_id
    assert result.state == "succeeded"
    query = parse_qs(urlparse(requested_urls[0]).query)
    assert query["Datefrom"] == ["05/Jan/2025"]
    assert query["Dateto"] == ["06/Jan/2025"]


def test_supervisor_ignores_window_strings_on_a_non_backfill_bank_rate_job(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 1, 20, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    queued = store.enqueue(
        "boe.bank_rate.iudbedr",
        request={
            "window_start": "2025-01-05T00:00:00.000000Z",
            "window_end": "2025-01-06T00:00:00.000000Z",
        },
        scheduled_for=now,
    )
    artifact = AcquiredArtifact(
        body=b"DATE,IUDBEDR\n31 Jul 2026,3.75\n",
        source_url="https://www.bankofengland.co.uk/data.csv",
        retrieved_at=now,
    )

    result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        bank_rate_collector=lambda _request: artifact,
    ).run_once(now=now)

    assert result.job_id == queued.job_id
    assert result.state == "succeeded"


def test_supervisor_rejects_bank_rate_backfill_records_outside_its_window(
    tmp_path: Path,
) -> None:
    start = datetime(2025, 1, 5, tzinfo=UTC)
    end = datetime(2025, 1, 6, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    queued = store.enqueue_backfill(
        "boe.bank_rate.iudbedr", window_start=start, window_end=end
    )
    artifact = AcquiredArtifact(
        body=b"DATE,IUDBEDR\n04 Jan 2025,4.75\n",
        source_url="https://www.bankofengland.co.uk/data.csv",
        retrieved_at=end,
    )

    result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        bank_rate_collector=lambda _request: artifact,
    ).run_once(now=end)

    assert result.job_id == queued.job_id
    assert result.state == "failed"
    assert store.get_job(queued.job_id)["state"] == "failed"  # type: ignore[index]


def test_supervisor_does_not_publish_streamed_cas_after_lease_expiry(
    tmp_path: Path, monkeypatch
) -> None:
    started_at = datetime(2026, 8, 1, 20, tzinfo=UTC)
    current = [started_at]
    store = OperationalStore(tmp_path)
    queued = store.enqueue(
        "boe.bank_rate.iudbedr",
        request={"series": "IUDBEDR"},
        scheduled_for=started_at,
    )

    class Response:
        status = 200
        headers = {"Content-Type": "text/csv"}

        def __init__(self) -> None:
            self._chunks = iter((b"DATE,IUDBEDR\n", b"31 Jul 2026,3.75\n"))

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            return None

        def read(self, _size: int) -> bytes:
            current[0] = started_at + timedelta(seconds=181)
            return next(self._chunks, b"")

        def getcode(self) -> int:
            return self.status

        def geturl(self) -> str:
            return "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?csv.x=yes&Datefrom=01%2FJan%2F2025&Dateto=now&SeriesCodes=IUDBEDR&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N"

    monkeypatch.setattr(common, "_open_once", lambda _request, _timeout: Response())

    result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        clock=lambda: current[0],
        resolver=lambda _host: ("8.8.8.8",),
        allow_network=True,
    ).run_once()

    assert result.job_id == queued.job_id
    assert result.error_code == "ACQUIRE_FAILED"
    assert store.artifacts.published_digests() == ()
    assert list((tmp_path / "evidence" / ".tmp").iterdir()) == []
    assert store.verify_evidence()["unreferenced"] == []


def test_supervisor_closes_a_policy_blocked_job_with_the_injected_clock(tmp_path: Path) -> None:
    now = datetime(2020, 1, 1, 12, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    queued = store.enqueue("boe.mpc_news", scheduled_for=now)

    result = DatasourceSupervisor(store, worker_id="test-worker").run_once(now=now)

    assert result.job_id == queued.job_id
    assert result.state == "failed"
    assert result.error_code == "WORKER_UNBOUND"


def test_supervisor_dispatches_fixed_ons_lifecycle_without_network(tmp_path: Path) -> None:
    now = datetime(2026, 8, 1, 20, tzinfo=UTC)
    datasource_id = "ons.gdp.ecyx"
    store = OperationalStore(tmp_path)
    queued = store.enqueue(datasource_id, scheduled_for=now)
    request = request_for(datasource_id)
    artifact = AcquisitionResponse(
        request_url=request.url,
        final_url=request.url,
        status=200,
        headers={"Content-Type": "application/json"},
        body=(
            b'{"description":{"title":"GDP","unit":"%"},'
            b'"months":[{"label":"2026 JUN","value":"2.6"}]}'
        ),
        retrieved_at="2026-08-01T20:00:00Z",
        method="GET",
    )
    calls: list[tuple[str, object]] = []

    result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        official_macro_collector=lambda source_id, job_request: (
            calls.append((source_id, job_request)) or artifact
        ),
    ).run_once(now=now)

    assert result.job_id == queued.job_id
    assert result.state == "succeeded"
    assert calls == [(datasource_id, {"series": "ECYX"})]


def test_supervisor_dispatches_bounded_on_demand_onspd_with_retention(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 1, 20, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    queued = store.enqueue(
        ONSPD_DATASOURCE_ID,
        request={"scope": {"postcode": ["EC2Y 5AS"]}},
        trigger="agent_request",
        scheduled_for=now,
    )
    calls: list[str] = []

    result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        onspd_collector=lambda postcode, _request: (
            calls.append(postcode) or _onspd_artifacts(postcode)
        ),
        onspd_retention_until=datetime(2030, 1, 1, tzinfo=UTC),
    ).run_once(now=now)

    assert result.job_id == queued.job_id
    assert result.state == "succeeded"
    assert calls == ["EC2Y 5AS"]
    assert store.get_job(queued.job_id)["state"] == "succeeded"  # type: ignore[index]


def test_supervisor_blocks_onspd_without_explicit_retention_or_a_bounded_postcode(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 1, 20, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    missing_retention = store.enqueue(
        ONSPD_DATASOURCE_ID,
        request={"postcode": "EC2Y 5AS"},
        scheduled_for=now,
    )

    retention_result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        onspd_collector=lambda *_args: pytest.fail("retention must fail before collection"),
    ).run_once(now=now)

    assert retention_result.job_id == missing_retention.job_id
    assert retention_result.error_code == "RETENTION_APPROVAL_REQUIRED"

    invalid_request = store.enqueue(
        ONSPD_DATASOURCE_ID,
        request={"scope": {"postcode": ["EC2Y 5AS", "SW1A 1AA"]}},
        trigger="agent_request",
        scheduled_for=now + timedelta(seconds=1),
    )
    invalid_result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        onspd_retention_until=datetime(2030, 1, 1, tzinfo=UTC),
        onspd_collector=lambda *_args: pytest.fail("invalid request must not collect"),
    ).run_once(now=now + timedelta(seconds=1))

    assert invalid_result.job_id == invalid_request.job_id
    assert invalid_result.error_code == "INVALID_ON_DEMAND_REQUEST"
    assert store.artifacts.published_digests() == ()


def test_supervisor_requires_explicit_network_opt_in_for_live_macro_collection(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 1, 20, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    queued = store.enqueue("ons.gdp.ecyx", scheduled_for=now)

    result = DatasourceSupervisor(store, worker_id="test-worker").run_once(now=now)

    assert result.job_id == queued.job_id
    assert result.error_code == "ACQUIRE_FAILED"
    assert store.artifacts.published_digests() == ()


def test_supervisor_rejects_current_vintage_macro_backfill_before_collection(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 1, 20, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    queued = store.enqueue_backfill(
        "ons.gdp.ecyx",
        window_start=datetime(2020, 1, 1, tzinfo=UTC),
        window_end=datetime(2020, 1, 2, tzinfo=UTC),
    )

    result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        official_macro_collector=lambda *_args: pytest.fail(
            "current-vintage macro backfill must not collect"
        ),
    ).run_once(now=now)

    assert result.job_id == queued.job_id
    assert result.error_code == "BACKFILL_UNSUPPORTED"
    assert store.artifacts.published_digests() == ()


def test_supervisor_dispatches_fixed_voa_release_lifecycle_without_network(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 1, 20, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    queued = store.enqueue(VOA_DATASOURCE_ID, scheduled_for=now)
    contract = contract_for(VOA_DATASOURCE_ID)
    release_url = (
        "https://assets.publishing.service.gov.uk/media/example/"
        "ndr_stock_of_properties_2026.zip"
    )
    capture = FileReleaseCapture(
        discovery=AcquisitionResponse(
            request_url=contract.discovery_url,
            final_url=contract.discovery_url,
            status=200,
            headers={"Content-Type": "text/html"},
            body=f'<a href="{release_url}">release</a>'.encode(),
            retrieved_at="2026-08-01T20:00:00Z",
            method="GET",
        ),
        release=AcquisitionResponse(
            request_url=release_url,
            final_url=release_url,
            status=200,
            headers={"Content-Type": "application/zip"},
            body=_voa_zip(),
            retrieved_at="2026-08-01T20:00:00Z",
            method="GET",
        ),
    )

    result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        file_release_collector=lambda _source_id, _request: capture,
    ).run_once(now=now)

    assert result.job_id == queued.job_id
    assert result.state == "succeeded"


def test_supervisor_rejects_file_release_backfill_before_collection(tmp_path: Path) -> None:
    now = datetime(2026, 8, 1, 20, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    queued = store.enqueue_backfill(
        VOA_DATASOURCE_ID,
        window_start=datetime(2020, 1, 1, tzinfo=UTC),
        window_end=datetime(2020, 1, 2, tzinfo=UTC),
    )

    result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        file_release_collector=lambda *_args: pytest.fail(
            "file-release backfill must not collect"
        ),
    ).run_once(now=now)

    assert result.job_id == queued.job_id
    assert result.error_code == "BACKFILL_UNSUPPORTED"


def test_supervisor_streams_fixed_ons_artifact_before_isolated_parse(
    tmp_path: Path, monkeypatch
) -> None:
    now = datetime(2026, 8, 1, 20, tzinfo=UTC)
    datasource_id = "ons.gdp.ecyx"
    store = OperationalStore(tmp_path)
    queued = store.enqueue(datasource_id, scheduled_for=now)

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self) -> None:
            self._chunks = iter(
                (
                    b'{"description":{"title":"GDP","unit":"%"},',
                    b'"months":[{"label":"2026 JUN","value":"2.6"}]}',
                )
            )

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            return None

        def read(self, _size: int) -> bytes:
            return next(self._chunks, b"")

        def getcode(self) -> int:
            return self.status

        def geturl(self) -> str:
            return request_for(datasource_id).url

    monkeypatch.setattr(common, "_open_once", lambda _request, _timeout: Response())
    monkeypatch.setattr(
        store.artifacts,
        "put_bytes",
        lambda *_args, **_kwargs: pytest.fail("live macro collection must stream to CAS"),
    )

    result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        allow_network=True,
        resolver=lambda _host: ("8.8.8.8",),
    ).run_once(now=now)

    assert result.job_id == queued.job_id
    assert result.state == "succeeded"


def test_supervisor_bootstraps_a_fresh_store_before_heartbeat_with_injected_clock(
    tmp_path: Path,
) -> None:
    now = datetime(2020, 1, 7, 20, tzinfo=UTC)
    store = OperationalStore(tmp_path)

    result = DatasourceSupervisor(store, worker_id="test-worker").run_once(now=now)

    assert result.state == "idle"
    connection = connect_database(store.database_path, read_only=True)
    try:
        definition_count = connection.execute(
            "SELECT count(*) FROM datasource_definition"
        ).fetchone()[0]
        schedule = connection.execute(
            """
            SELECT created_at FROM workflow_schedule
            WHERE datasource_id = 'boe.bank_rate.iudbedr'
            """
        ).fetchone()
        heartbeat = connection.execute(
            """
            SELECT heartbeat_at, lease_expires_at FROM service_heartbeat
            WHERE instance_id = 'test-worker'
            """
        ).fetchone()
    finally:
        connection.close()

    assert definition_count == len(store.registry.definitions)
    assert schedule["created_at"] == "2020-01-07T20:00:00.000000Z"
    assert tuple(heartbeat) == (
        "2020-01-07T20:00:00.000000Z",
        "2020-01-07T20:03:00.000000Z",
    )


def test_supervisor_defers_a_429_until_retry_after_without_sleeping(
    tmp_path: Path, monkeypatch
) -> None:
    now = datetime(2026, 8, 1, 20, tzinfo=UTC)
    store = OperationalStore(tmp_path)
    queued = store.enqueue(
        "boe.bank_rate.iudbedr",
        request={"series": "IUDBEDR"},
        scheduled_for=now,
    )

    class Response:
        status = 429
        headers = {"Content-Type": "text/csv", "Retry-After": "120"}

        def close(self) -> None:
            return None

        def getcode(self) -> int:
            return self.status

        def geturl(self) -> str:
            return "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"

    monkeypatch.setattr(common, "_open_once", lambda _request, _timeout: Response())

    result = DatasourceSupervisor(
        store,
        worker_id="test-worker",
        resolver=lambda _host: ("8.8.8.8",),
        allow_network=True,
    ).run_once(now=now)

    assert result.job_id == queued.job_id
    assert result.error_code == "HOST_THROTTLED"
    connection = connect_database(store.database_path, read_only=True)
    try:
        job = connection.execute(
            "SELECT state, available_at FROM workflow_job WHERE job_id = ?",
            (queued.job_id,),
        ).fetchone()
        throttle = connection.execute(
            """
            SELECT next_allowed_at, blocked_until, last_http_status
            FROM host_throttle WHERE rate_limit_group = 'www.bankofengland.co.uk'
            """
        ).fetchone()
    finally:
        connection.close()

    assert tuple(job) == ("retry_wait", "2026-08-01T20:02:00.000000Z")
    assert tuple(throttle) == (
        "2026-08-01T20:02:00.000000Z",
        "2026-08-01T20:02:00.000000Z",
        429,
    )
    assert DatasourceSupervisor(store, worker_id="test-worker").run_once(
        now=now + timedelta(seconds=60)
    ).state == "idle"
