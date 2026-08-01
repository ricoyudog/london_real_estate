from __future__ import annotations

import pytest

from nan_fung.ingestion import official_macro


ONS_MONTH_FIXTURE = b"""{
  "description": {
    "title": "Monthly gross domestic product: Index",
    "unit": "%",
    "releaseDate": "2026-07-21T23:00:00.000Z",
    "monthLabelStyle": "three month average"
  },
  "months": [
    {"label": "2026 MAY", "value": "2.50", "updateDate": "2026-06-21T23:00:00.000Z"},
    {"label": "2026 JUN", "value": 2.60, "updateDate": "2026-07-21T23:00:00.000Z"}
  ]
}"""

ONS_QUARTER_FIXTURE = b"""{
  "description": {
    "title": "Quarterly gross domestic product",
    "releaseDate": "2026-07-21T23:00:00.000Z",
    "quarterLabelStyle": "quarterly"
  },
  "quarters": [
    {"label": "2026 Q1", "value": "0.70", "updateDate": "2026-07-21T23:00:00.000Z"}
  ]
}"""

NOMIS_LFS_FIXTURE = b"""{
  "obs": [{
    "geography": {"description": "London", "geogcode": "E12000007"},
    "time": {"description": "Mar 2026-May 2026", "value": "2026-05"},
    "economic_activity": {"description": "Employment rate"},
    "obs_value": {"value": 73.80},
    "obs_status": {"description": "Normal Value"}
  }]
}"""

NOMIS_JOBS_FIXTURE = b"""{
  "obs": [{
    "geography": {"description": "London", "geogcode": "E12000007"},
    "time": {"description": "March 2026", "value": "2026-03"},
    "item": {"description": "total workforce jobs"},
    "obs_value": {"value": "6466474"},
    "obs_status": {"description": "Normal Value"}
  }]
}"""

MPC_RSS_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss><channel>
  <item><title>Other news</title><link>https://example.test/other</link></item>
  <item>
    <title>Bank Rate maintained at 3.75%</title>
    <link>https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2026/july-2026</link>
    <guid>mpc-july-2026</guid>
    <pubDate>Thu, 30 Jul 2026 12:00:00 +0100</pubDate>
    <description>Latest MPC decision.</description>
  </item>
</channel></rss>"""


def test_parse_ons_months_normalizes_persisted_json_with_locator_and_key() -> None:
    records = official_macro.parse_ons_series_json(
        ONS_MONTH_FIXTURE,
        series="ECYX",
        uri="/economy/grossdomesticproductgdp/timeseries/ecyx/mgdp",
        frequency="months",
        source_url="https://api.beta.ons.gov.uk/v1/data?uri=ecyx",
    )

    assert [record["value"] for record in records] == ["2.5", "2.6"]
    assert records[-1]["source_value"] == "2.60"
    assert records[-1]["locator"] == {
        "kind": "json_pointer",
        "pointer": "/months/1",
        "uri": "/economy/grossdomesticproductgdp/timeseries/ecyx/mgdp",
    }
    assert official_macro.ons_record_key(records[-1]) == ("ECYX", "2026 JUN")
    assert official_macro.ons_record_metadata(records[-1])["period_label"] == "2026 JUN"
    assert official_macro.ons_artifact_metadata(records) == {
        "published_at": "2026-07-21T23:00:00.000Z",
        "source_updated_at": "2026-07-21T23:00:00.000Z",
        "record_count": 2,
    }


def test_parse_ons_quarters_uses_unit_fallback() -> None:
    records = official_macro.parse_ons_series_json(
        ONS_QUARTER_FIXTURE,
        series="IHYQ",
        uri="/economy/grossdomesticproductgdp/timeseries/ihyq/qna",
        frequency="quarters",
        source_url="https://api.beta.ons.gov.uk/v1/data?uri=ihyq",
    )

    assert records[0]["unit"] == ""
    assert records[0]["frequency"] == "quarter"
    assert official_macro.ons_record_key(records[0]) == ("IHYQ", "2026 Q1")
    assert official_macro.ons_record_metadata(records[0])["unit"] == ""


@pytest.mark.parametrize(
    ("series", "datasource_id"),
    [
        ("ECYX", "ons.gdp.ecyx"),
        ("IHYQ", "ons.gdp.ihyq"),
        ("D7G7", "ons.inflation.d7g7"),
        ("L55O", "ons.inflation.l55o"),
        ("CZBH", "ons.inflation.czbh"),
        ("LF24", "ons.labour.lf24"),
        ("MGSX", "ons.labour.mgsx"),
        ("AP2Y", "ons.labour.ap2y"),
        ("KAI9", "ons.labour.kai9"),
    ],
)
def test_ons_datasource_id_covers_the_configured_official_series(
    series: str,
    datasource_id: str,
) -> None:
    assert official_macro.ons_datasource_id(series) == datasource_id


@pytest.mark.parametrize(
    ("dataset", "fixture", "expected_key", "expected_unit"),
    [
        (
            "NM_59_1",
            NOMIS_LFS_FIXTURE,
            ("NM_59_1", "E12000007", "2026-05", "Employment rate"),
            "percent",
        ),
        (
            "NM_130_1",
            NOMIS_JOBS_FIXTURE,
            ("NM_130_1", "E12000007", "2026-03", "total workforce jobs"),
            "jobs",
        ),
    ],
)
def test_parse_nomis_dataset_json_normalizes_both_configured_datasets(
    dataset: str,
    fixture: bytes,
    expected_key: tuple[str, str, str, str],
    expected_unit: str,
) -> None:
    records = official_macro.parse_nomis_dataset_json(
        fixture,
        dataset=dataset,
        source_url=f"https://www.nomisweb.co.uk/api/v01/dataset/{dataset}.data.json",
    )

    assert official_macro.nomis_record_key(records[0]) == expected_key
    assert records[0]["unit"] == expected_unit
    assert records[0]["locator"]["pointer"] == "/obs/0"
    assert official_macro.nomis_record_metadata(records[0])["datasource_id"].startswith(
        "nomis."
    )
    assert official_macro.nomis_artifact_metadata(records) == {
        "dataset": dataset,
        "record_count": 1,
        "period_codes": [expected_key[2]],
    }


def test_parse_mpc_rss_metadata_filters_and_keys_canonical_mpc_path() -> None:
    records = official_macro.parse_mpc_rss_xml(
        MPC_RSS_FIXTURE,
        source_url="https://www.bankofengland.co.uk/rss/news",
    )

    assert len(records) == 1
    assert records[0]["canonical_path"] == "/monetary-policy-summary-and-minutes/2026/july-2026"
    assert official_macro.mpc_rss_record_key(records[0]) == (
        "/monetary-policy-summary-and-minutes/2026/july-2026",
    )
    assert records[0]["locator"] == {
        "kind": "rss_item",
        "item_index": 1,
        "guid": "mpc-july-2026",
        "canonical_path": "/monetary-policy-summary-and-minutes/2026/july-2026",
    }
    assert official_macro.mpc_rss_record_metadata(records[0])["record_type"] == "event"
    assert official_macro.mpc_rss_artifact_metadata(records) == {
        "published_at": "2026-07-30T12:00:00+01:00",
        "source_updated_at": "2026-07-30T12:00:00+01:00",
        "record_count": 1,
    }
