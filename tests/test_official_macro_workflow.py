from __future__ import annotations

import pytest

from nan_fung.datasources.common import AcquisitionResponse
from nan_fung.ingestion.official_macro_workflow import (
    OfficialMacroWorkflowError,
    mpc_rss_request,
    nomis_request_for,
    normalize_mpc_rss_response,
    normalize_nomis_response,
    normalize_ons_response,
    ons_request_for,
    request_for,
)


ONS_FIXTURE = b"""{
  "description": {
    "title": "Monthly gross domestic product: Index",
    "unit": "%",
    "releaseDate": "2026-07-21T23:00:00.000Z",
    "monthLabelStyle": "three month average"
  },
  "months": [{
    "label": "2026 JUN",
    "value": "2.60",
    "updateDate": "2026-07-21T23:00:00.000Z"
  }]
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


def _response(
    request_url: str,
    body: bytes,
    *,
    final_url: str | None = None,
    status: int = 200,
    method: str = "GET",
) -> AcquisitionResponse:
    return AcquisitionResponse(
        request_url=request_url,
        final_url=final_url or request_url,
        status=status,
        headers={"Content-Type": "application/json", "Set-Cookie": "secret"},
        body=body,
        retrieved_at="2026-08-01T00:00:00+00:00",
        method=method,
    )


def test_ons_contract_builds_immutable_request_and_normalizes_its_one_series() -> None:
    request = ons_request_for("ons.gdp.ecyx")

    assert request.source_id == "ons.data_api"
    assert request.url == (
        "https://api.beta.ons.gov.uk/v1/data?"
        "uri=%2Feconomy%2Fgrossdomesticproductgdp%2Ftimeseries%2Fecyx%2Fmgdp"
    )
    assert request.policy.allowed_hosts == ("api.beta.ons.gov.uk",)

    normalized = normalize_ons_response(
        "ons.gdp.ecyx", _response(request.url, ONS_FIXTURE)
    )

    assert normalized.datasource_id == "ons.gdp.ecyx"
    assert normalized.record_keys == (("ECYX", "2026 JUN"),)
    assert normalized.records[0]["value"] == "2.6"
    assert normalized.artifact_metadata["record_count"] == 1
    assert normalized.acquisition.retrieved_at == "2026-08-01T00:00:00.000000Z"
    assert dict(normalized.acquisition.headers) == {"Content-Type": "application/json"}


@pytest.mark.parametrize(
    ("datasource_id", "fixture", "expected_key"),
    [
        (
            "nomis.nm_59_1.london_lfs",
            NOMIS_LFS_FIXTURE,
            ("NM_59_1", "E12000007", "2026-05", "Employment rate"),
        ),
        (
            "nomis.nm_130_1.london_workforce_jobs",
            NOMIS_JOBS_FIXTURE,
            ("NM_130_1", "E12000007", "2026-03", "total workforce jobs"),
        ),
    ],
)
def test_nomis_contracts_normalize_only_their_fixed_london_dataset(
    datasource_id: str,
    fixture: bytes,
    expected_key: tuple[str, str, str, str],
) -> None:
    request = nomis_request_for(datasource_id)

    assert request.source_id == "nomis.api"
    assert request.url.startswith("https://www.nomisweb.co.uk/api/v01/dataset/NM_")
    assert "geography=E12000007" in request.url

    normalized = normalize_nomis_response(datasource_id, _response(request.url, fixture))

    assert normalized.record_keys == (expected_key,)
    assert normalized.records[0]["geography_code"] == "E12000007"
    assert normalized.artifact_metadata["dataset"] == expected_key[0]


def test_mpc_rss_contract_normalizes_release_metadata() -> None:
    request = mpc_rss_request()

    assert request.datasource_id == "boe.mpc_news"
    assert request.url == "https://www.bankofengland.co.uk/rss/news"
    assert request.policy.allowed_hosts == ("www.bankofengland.co.uk",)

    normalized = normalize_mpc_rss_response(_response(request.url, MPC_RSS_FIXTURE))

    assert normalized.record_keys == (
        ("/monetary-policy-summary-and-minutes/2026/july-2026",),
    )
    assert normalized.records[0]["datasource_id"] == "boe.mpc_news"
    assert normalized.artifact_metadata["record_count"] == 1


def test_unknown_datasource_and_forged_request_fail_closed() -> None:
    with pytest.raises(OfficialMacroWorkflowError, match="unsupported ONS datasource"):
        ons_request_for("ons.gdp.unapproved")

    request = ons_request_for("ons.gdp.ecyx")
    forged = _response("https://evil.example/data", ONS_FIXTURE)
    with pytest.raises(OfficialMacroWorkflowError, match="request URL does not match"):
        normalize_ons_response("ons.gdp.ecyx", forged)

    wrong_method = _response(request.url, ONS_FIXTURE, method="POST")
    with pytest.raises(OfficialMacroWorkflowError, match="must use GET"):
        normalize_ons_response("ons.gdp.ecyx", wrong_method)


@pytest.mark.parametrize(
    ("status", "headers"),
    (
        (206, {"Content-Type": "application/json"}),
        (200, {"Content-Type": "application/json", "Content-Range": "bytes 0-4/10"}),
    ),
)
def test_official_macro_contract_rejects_partial_responses(
    status: int, headers: dict[str, str]
) -> None:
    request = ons_request_for("ons.gdp.ecyx")
    response = AcquisitionResponse(
        request_url=request.url,
        final_url=request.url,
        status=status,
        headers=headers,
        body=ONS_FIXTURE,
        retrieved_at="2026-08-01T00:00:00Z",
        method="GET",
    )

    with pytest.raises(OfficialMacroWorkflowError, match="complete HTTP 200"):
        normalize_ons_response("ons.gdp.ecyx", response)


@pytest.mark.parametrize(
    ("datasource_id", "body", "final_url"),
    (
        (
            "ons.gdp.ecyx",
            ONS_FIXTURE,
            "https://api.beta.ons.gov.uk/v1/data?uri=%2Feconomy%2Fother",
        ),
        (
            "nomis.nm_59_1.london_lfs",
            NOMIS_LFS_FIXTURE,
            "https://www.nomisweb.co.uk/api/v01/dataset/NM_130_1.data.json?"
            "geography=E12000007&time=latest&sex=7&economic_activity=3%2C7&"
            "value_type=0&measures=20207",
        ),
    ),
)
def test_official_macro_contract_rejects_same_host_wrong_final_endpoint(
    datasource_id: str, body: bytes, final_url: str
) -> None:
    request = request_for(datasource_id)

    with pytest.raises(OfficialMacroWorkflowError, match="final URL does not match"):
        if datasource_id.startswith("ons."):
            normalize_ons_response(datasource_id, _response(request.url, body, final_url=final_url))
        else:
            normalize_nomis_response(datasource_id, _response(request.url, body, final_url=final_url))
