from __future__ import annotations

import json
from typing import Any

import pytest

from nan_fung.datasources import macro
from nan_fung.ingestion.bank_rate import BANK_RATE_DATASOURCE_ID
from nan_fung.ingestion.parser_runner import parser_isolation_status
from nan_fung.operational import OperationalStore
from nan_fung.storage.db import connect_database
from nan_fung.supervisor import DatasourceSupervisor


def test_fetch_bank_rate_parses_official_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"DATE,IUDBEDR\r\n29 Jul 2026,3.75\r\n30 Jul 2026,3.75\r\n"

    def fake_get_bytes(url: str, **kwargs: Any) -> bytes:
        assert url == macro.BOE_BANK_RATE_URL
        assert kwargs["params"]["SeriesCodes"] == "IUDBEDR"
        return payload

    monkeypatch.setattr(macro, "get_bytes", fake_get_bytes)

    result = macro.fetch_bank_rate("29/Jul/2026", "30/Jul/2026")

    assert result["published_at"] is None
    assert result["records"][-1] == {
        "date": "2026-07-30",
        "bank_rate_percent": 3.75,
        "series": "IUDBEDR",
    }


def test_parse_bank_rate_csv_is_a_pure_artifact_parser() -> None:
    records = macro.parse_bank_rate_csv(
        b"DATE,IUDBEDR\r\n29 Jul 2026,3.75\r\n"
    )

    assert records == [
        {"date": "2026-07-29", "bank_rate_percent": 3.75, "series": "IUDBEDR"}
    ]


def test_fetch_latest_mpc_decision_filters_news_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"""<?xml version="1.0"?>
    <rss><channel>
      <item><title>Other news</title><link>https://example.com/other</link></item>
      <item>
        <title>Bank Rate maintained at 3.75%</title>
        <link>https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2026/july-2026</link>
        <pubDate>Thu, 30 Jul 2026 12:00:00 +0100</pubDate>
        <description>Latest MPC decision.</description>
      </item>
    </channel></rss>"""
    monkeypatch.setattr(macro, "get_bytes", lambda _url, **_kwargs: payload)

    result = macro.fetch_latest_mpc_decision()

    assert len(result["records"]) == 1
    assert result["records"][0]["title"] == "Bank Rate maintained at 3.75%"
    assert result["published_at"] == "2026-07-30T12:00:00+01:00"


@pytest.mark.parametrize(
    ("function", "expected_codes"),
    [
        (macro.fetch_uk_gdp, {"ECYX", "IHYQ"}),
        (macro.fetch_uk_inflation, {"D7G7", "L55O", "CZBH"}),
        (macro.fetch_uk_labour_market, {"LF24", "MGSX", "AP2Y", "KAI9"}),
    ],
)
def test_fetch_ons_series_uses_latest_observation(
    monkeypatch: pytest.MonkeyPatch,
    function: Any,
    expected_codes: set[str],
) -> None:
    def fake_get_bytes(_url: str, **kwargs: Any) -> bytes:
        uri = kwargs["params"]["uri"]
        code = uri.split("/")[-2].upper()
        frequency = "quarters" if code == "IHYQ" else "months"
        return json.dumps(
            {
                "description": {
                    "title": f"Official {code}",
                    "unit": "%",
                    "releaseDate": "2026-07-21T23:00:00.000Z",
                },
                frequency: [
                    {
                        "label": "2026 Q1"
                        if frequency == "quarters"
                        else "2026 JUN",
                        "value": "2.6",
                        "updateDate": "2026-07-21T23:00:00.000Z",
                    }
                ],
            }
        ).encode()

    monkeypatch.setattr(macro, "get_bytes", fake_get_bytes)

    result = function()

    assert {record["series"] for record in result["records"]} == expected_codes
    assert all(record["value"] == 2.6 for record in result["records"])
    assert result["published_at"] == "2026-07-21T23:00:00.000Z"


def test_fetch_ons_series_can_return_current_vintage_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        macro,
        "get_bytes",
        lambda _url, **_kwargs: json.dumps(
            {
                "description": {"title": "Official ECYX", "unit": "%"},
                "months": [
                    {
                        "label": "2026 MAY",
                        "value": "2.5",
                        "updateDate": "2026-06-01Z",
                    },
                    {
                        "label": "2026 JUN",
                        "value": "2.6",
                        "updateDate": "2026-07-01Z",
                    },
                ],
                "quarters": [
                    {
                        "label": "2026 Q1",
                        "value": "0.7",
                        "updateDate": "2026-07-01Z",
                    },
                ],
            }
        ).encode(),
    )

    result = macro.fetch_uk_gdp(include_history=True)

    assert [(item["series"], item["period"]) for item in result["records"]] == [
        ("ECYX", "2026 MAY"),
        ("ECYX", "2026 JUN"),
        ("IHYQ", "2026 Q1"),
    ]
    assert all(isinstance(item["source_value"], str) for item in result["records"])


def test_fetch_london_labour_market_combines_nomis_datasets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get_bytes(url: str, **kwargs: Any) -> bytes:
        assert kwargs["params"]["geography"] == "E12000007"
        common = {
            "geography": {"description": "London", "geogcode": "E12000007"},
            "time": {"description": "Mar 2026-May 2026", "value": "2026-05"},
            "obs_status": {"description": "Normal Value"},
        }
        if "NM_59_1" in url:
            return json.dumps(
                {
                    "obs": [
                        {
                            **common,
                            "economic_activity": {"description": "Employment rate"},
                            "obs_value": {"value": 73.8},
                        }
                    ]
                }
            ).encode()
        return json.dumps(
            {
                "obs": [
                    {
                        **common,
                        "item": {"description": "total workforce jobs"},
                        "obs_value": {"value": 6_466_474},
                    }
                ]
            }
        ).encode()

    monkeypatch.setattr(macro, "get_bytes", fake_get_bytes)

    result = macro.fetch_london_labour_market()

    assert [record["dataset"] for record in result["records"]] == [
        "NM_59_1",
        "NM_130_1",
    ]
    assert all(record["geography_code"] == "E12000007" for record in result["records"])
    assert result["records"][1]["value"] == 6_466_474


@pytest.mark.network
@pytest.mark.legacy_live_probe
def test_legacy_live_bank_rate_adapter() -> None:
    rates = macro.fetch_bank_rate("01/Jul/2026", "now")

    assert rates["records"]
    assert 0 <= rates["records"][-1]["bank_rate_percent"] <= 20


@pytest.mark.network
@pytest.mark.restricted_live_probe
def test_restricted_live_mpc_decision_adapter() -> None:
    decision = macro.fetch_latest_mpc_decision()

    assert decision["records"]
    assert "Bank Rate" in decision["records"][0]["title"]


@pytest.mark.network
@pytest.mark.legacy_live_probe
def test_live_ons_macro_sources() -> None:
    results = (
        macro.fetch_uk_gdp(),
        macro.fetch_uk_inflation(),
        macro.fetch_uk_labour_market(),
    )

    assert all(result["records"] for result in results)
    assert {record["series"] for record in results[0]["records"]} == {"ECYX", "IHYQ"}
    assert all(
        isinstance(record["value"], float)
        for result in results
        for record in result["records"]
    )
    labour = {record["series"]: record for record in results[2]["records"]}
    assert labour["AP2Y"]["unit"] == "thousand vacancies"
    assert labour["AP2Y"]["period_basis"] == "three month average"


@pytest.mark.network
@pytest.mark.legacy_live_probe
def test_live_nomis_london_sources() -> None:
    result = macro.fetch_london_labour_market()

    assert result["records"]
    assert {record["dataset"] for record in result["records"]} == {
        "NM_59_1",
        "NM_130_1",
    }
    assert all(record["geography_code"] == "E12000007" for record in result["records"])


@pytest.mark.network
@pytest.mark.live
def test_live_bank_rate_operational_workflow_writes_canonical_data(tmp_path) -> None:
    """Verify real acquisition reaches evidence, observations, and promotion."""

    isolation = parser_isolation_status()
    if not isolation["available"]:
        pytest.skip(f"operational parser isolation unavailable: {isolation['reason']}")

    store = OperationalStore(tmp_path)
    queued = store.enqueue(BANK_RATE_DATASOURCE_ID, request={"series": "IUDBEDR"})
    tick = DatasourceSupervisor(
        store,
        worker_id="recorded-live-smoke",
        allow_network=True,
    ).run_once()

    assert tick.state == "succeeded"
    assert tick.job_id == queued.job_id
    assert tick.run_id
    job = store.get_job(queued.job_id)
    assert job is not None
    assert job["state"] == "succeeded"
    run = job["run"]
    assert run is not None
    assert run["record_count"] > 0
    assert run["accepted_record_count"] == run["record_count"]
    assert run["rejected_record_count"] == 0
    assert job["promotions"][-1]["decision"] == "approved"
    verification = store.verify_evidence()
    assert verification["ok"]
    assert verification["checked"] == 1

    connection = connect_database(store.database_path, read_only=True)
    try:
        canonical_count = connection.execute(
            """
            SELECT COUNT(*) FROM canonical_latest_v1
            WHERE datasource_id = ?
            """,
            (BANK_RATE_DATASOURCE_ID,),
        ).fetchone()[0]
        evidence_count = connection.execute(
            "SELECT COUNT(*) FROM run_evidence WHERE run_id = ?",
            (tick.run_id,),
        ).fetchone()[0]
    finally:
        connection.close()

    assert canonical_count == run["accepted_record_count"]
    assert evidence_count == 1
