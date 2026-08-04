"""Fixed acquisition and normalization contracts for official macro sources.

This module deliberately stops short of wiring a collector into the durable
workflow.  It gives that later wiring a small, source-specific boundary: it
can request only the approved ONS series, Nomis datasets, or MPC RSS feed and
can normalize only an acquisition that matches that request.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlparse

from .canonical import CanonicalizationError, normalize_timestamp
from .official_macro import (
    OfficialMacroParseError,
    mpc_rss_artifact_metadata,
    mpc_rss_record_key,
    mpc_rss_record_metadata,
    nomis_artifact_metadata,
    nomis_record_key,
    nomis_record_metadata,
    ons_artifact_metadata,
    ons_record_key,
    ons_record_metadata,
    parse_mpc_rss_xml,
    parse_nomis_dataset_json,
    parse_ons_series_json,
)
from .policies import (
    ArtifactPolicy,
    PolicyError,
    SourcePolicy,
    redact_headers,
    redact_url,
    validate_source_url,
)


class OfficialMacroWorkflowError(ValueError):
    """An official-macro acquisition or record violates its fixed contract."""


class AcquisitionResponseLike(Protocol):
    """The stable subset consumed from ``datasources.common.AcquisitionResponse``."""

    request_url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    retrieved_at: str
    method: str


@dataclass(frozen=True, slots=True)
class OfficialMacroRequest:
    """One immutable, approved request with its executable source policy."""

    datasource_id: str
    source_id: str
    url: str
    policy: SourcePolicy


@dataclass(frozen=True, slots=True)
class OfficialMacroAcquisition:
    """Redacted successful response metadata plus the in-memory artifact body."""

    datasource_id: str
    request_url: str
    source_url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    retrieved_at: str
    method: str


@dataclass(frozen=True, slots=True)
class NormalizedOfficialMacroArtifact:
    """Validated normalized records derived from one approved response."""

    datasource_id: str
    acquisition: OfficialMacroAcquisition
    records: tuple[Mapping[str, Any], ...]
    record_keys: tuple[tuple[str, ...], ...]
    artifact_metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _OnsContract:
    datasource_id: str
    series: str
    uri: str
    frequency: str


@dataclass(frozen=True, slots=True)
class _NomisContract:
    datasource_id: str
    dataset: str
    query: tuple[tuple[str, str], ...]


ONS_DATA_URL = "https://api.beta.ons.gov.uk/v1/data"
NOMIS_DATASET_URL = "https://www.nomisweb.co.uk/api/v01/dataset"
MPC_RSS_URL = "https://www.bankofengland.co.uk/rss/news"

ONS_SOURCE_POLICY = SourcePolicy(
    allowed_hosts=("api.beta.ons.gov.uk",),
    allowed_query_keys=("uri",),
    artifact=ArtifactPolicy(
        max_bytes=4 * 1024 * 1024,
        allowed_media_types=("application/json",),
    ),
)
NOMIS_SOURCE_POLICY = SourcePolicy(
    allowed_hosts=("www.nomisweb.co.uk",),
    allowed_query_keys=(
        "geography",
        "time",
        "sex",
        "economic_activity",
        "value_type",
        "measures",
        "industry",
        "item",
    ),
    artifact=ArtifactPolicy(
        max_bytes=8 * 1024 * 1024,
        allowed_media_types=("application/json",),
    ),
)
MPC_RSS_SOURCE_POLICY = SourcePolicy(
    allowed_hosts=("www.bankofengland.co.uk",),
    artifact=ArtifactPolicy(
        max_bytes=4 * 1024 * 1024,
        allowed_media_types=("application/rss+xml", "application/xml", "text/xml"),
    ),
)


_ONS_CONTRACTS = MappingProxyType(
    {
        "ons.gdp.ecyx": _OnsContract(
            "ons.gdp.ecyx",
            "ECYX",
            "/economy/grossdomesticproductgdp/timeseries/ecyx/mgdp",
            "months",
        ),
        "ons.gdp.ihyq": _OnsContract(
            "ons.gdp.ihyq",
            "IHYQ",
            "/economy/grossdomesticproductgdp/timeseries/ihyq/qna",
            "quarters",
        ),
        "ons.inflation.d7g7": _OnsContract(
            "ons.inflation.d7g7",
            "D7G7",
            "/economy/inflationandpriceindices/timeseries/d7g7/mm23",
            "months",
        ),
        "ons.inflation.l55o": _OnsContract(
            "ons.inflation.l55o",
            "L55O",
            "/economy/inflationandpriceindices/timeseries/l55o/mm23",
            "months",
        ),
        "ons.inflation.czbh": _OnsContract(
            "ons.inflation.czbh",
            "CZBH",
            "/economy/inflationandpriceindices/timeseries/czbh/mm23",
            "months",
        ),
        "ons.labour.lf24": _OnsContract(
            "ons.labour.lf24",
            "LF24",
            "/employmentandlabourmarket/peopleinwork/employmentandemployeetypes/"
            "timeseries/lf24/lms",
            "months",
        ),
        "ons.labour.mgsx": _OnsContract(
            "ons.labour.mgsx",
            "MGSX",
            "/employmentandlabourmarket/peoplenotinwork/unemployment/timeseries/mgsx/lms",
            "months",
        ),
        "ons.labour.ap2y": _OnsContract(
            "ons.labour.ap2y",
            "AP2Y",
            "/employmentandlabourmarket/peopleinwork/employmentandemployeetypes/"
            "timeseries/ap2y/lms",
            "months",
        ),
        "ons.labour.kai9": _OnsContract(
            "ons.labour.kai9",
            "KAI9",
            "/employmentandlabourmarket/peopleinwork/earningsandworkinghours/"
            "timeseries/kai9/lms",
            "months",
        ),
    }
)
_NOMIS_CONTRACTS = MappingProxyType(
    {
        "nomis.nm_59_1.london_lfs": _NomisContract(
            "nomis.nm_59_1.london_lfs",
            "NM_59_1",
            (
                ("geography", "E12000007"),
                ("time", "latest"),
                ("sex", "7"),
                ("economic_activity", "3,7"),
                ("value_type", "0"),
                ("measures", "20207"),
            ),
        ),
        "nomis.nm_130_1.london_workforce_jobs": _NomisContract(
            "nomis.nm_130_1.london_workforce_jobs",
            "NM_130_1",
            (
                ("geography", "E12000007"),
                ("time", "latest"),
                ("industry", "37748736"),
                ("item", "1"),
                ("measures", "20100"),
            ),
        ),
    }
)


def ons_request_for(datasource_id: str) -> OfficialMacroRequest:
    """Build the only approved ONS request for one configured datasource."""

    contract = _ons_contract(datasource_id)
    return OfficialMacroRequest(
        datasource_id=contract.datasource_id,
        source_id="ons.data_api",
        url=f"{ONS_DATA_URL}?{urlencode((("uri", contract.uri),))}",
        policy=ONS_SOURCE_POLICY,
    )


def nomis_request_for(datasource_id: str) -> OfficialMacroRequest:
    """Build the only approved Nomis request for one configured datasource."""

    contract = _nomis_contract(datasource_id)
    endpoint = f"{NOMIS_DATASET_URL}/{contract.dataset}.data.json"
    return OfficialMacroRequest(
        datasource_id=contract.datasource_id,
        source_id="nomis.api",
        url=f"{endpoint}?{urlencode(contract.query)}",
        policy=NOMIS_SOURCE_POLICY,
    )


def mpc_rss_request() -> OfficialMacroRequest:
    """Build the sole approved MPC RSS discovery request."""

    return OfficialMacroRequest(
        datasource_id="boe.mpc_news",
        source_id="boe.rss",
        url=MPC_RSS_URL,
        policy=MPC_RSS_SOURCE_POLICY,
    )


def collect_ons_ecyx() -> OfficialMacroRequest:
    return ons_request_for("ons.gdp.ecyx")


def collect_ons_ihyq() -> OfficialMacroRequest:
    return ons_request_for("ons.gdp.ihyq")


def collect_ons_d7g7() -> OfficialMacroRequest:
    return ons_request_for("ons.inflation.d7g7")


def collect_ons_l55o() -> OfficialMacroRequest:
    return ons_request_for("ons.inflation.l55o")


def collect_ons_czbh() -> OfficialMacroRequest:
    return ons_request_for("ons.inflation.czbh")


def collect_ons_lf24() -> OfficialMacroRequest:
    return ons_request_for("ons.labour.lf24")


def collect_ons_mgsx() -> OfficialMacroRequest:
    return ons_request_for("ons.labour.mgsx")


def collect_ons_ap2y() -> OfficialMacroRequest:
    return ons_request_for("ons.labour.ap2y")


def collect_ons_kai9() -> OfficialMacroRequest:
    return ons_request_for("ons.labour.kai9")


def collect_nomis_nm_59_1() -> OfficialMacroRequest:
    return nomis_request_for("nomis.nm_59_1.london_lfs")


def collect_nomis_nm_130_1() -> OfficialMacroRequest:
    return nomis_request_for("nomis.nm_130_1.london_workforce_jobs")


def adapt_acquisition_response(
    request: OfficialMacroRequest,
    response: AcquisitionResponseLike,
) -> OfficialMacroAcquisition:
    """Fail closed unless an acquisition is exactly for its approved request."""

    if response.request_url != request.url:
        raise OfficialMacroWorkflowError("acquisition request URL does not match contract")
    if not isinstance(response.method, str) or response.method.upper() != "GET":
        raise OfficialMacroWorkflowError("official macro requests must use GET")
    if not isinstance(response.status, int) or isinstance(response.status, bool):
        raise OfficialMacroWorkflowError("acquisition status must be an integer")
    if not isinstance(response.body, bytes):
        raise OfficialMacroWorkflowError("acquisition body must be bytes")
    if not isinstance(response.headers, Mapping):
        raise OfficialMacroWorkflowError("acquisition headers must be a mapping")
    if not any(
        name.lower() == "content-type" and isinstance(value, str) and value.strip()
        for name, value in response.headers.items()
    ):
        raise OfficialMacroWorkflowError("official macro responses require Content-Type")
    if response.status != 200 or _has_content_range(response.headers):
        raise OfficialMacroWorkflowError(
            "official macro responses require a complete HTTP 200 response"
        )
    try:
        validate_source_url(request.url, request.policy, resolver=None)
        validate_source_url(response.final_url, request.policy, resolver=None)
        retrieved_at = normalize_timestamp(response.retrieved_at)
    except (CanonicalizationError, PolicyError) as error:
        raise OfficialMacroWorkflowError("acquisition provenance is not approved") from error
    if not _same_contract_endpoint(request.url, response.final_url):
        raise OfficialMacroWorkflowError(
            "acquisition final URL does not match the fixed source contract"
        )
    return OfficialMacroAcquisition(
        datasource_id=request.datasource_id,
        request_url=redact_url(request.url),
        source_url=redact_url(response.final_url),
        status=response.status,
        headers=MappingProxyType(redact_headers(response.headers)),
        body=response.body,
        retrieved_at=retrieved_at,
        method="GET",
    )


def normalize_ons_response(
    datasource_id: str,
    response: AcquisitionResponseLike,
) -> NormalizedOfficialMacroArtifact:
    """Parse an acquired fixed ONS series into validated normalized records."""

    contract = _ons_contract(datasource_id)
    acquisition = adapt_acquisition_response(ons_request_for(datasource_id), response)
    try:
        records = parse_ons_series_json(
            acquisition.body,
            series=contract.series,
            uri=contract.uri,
            frequency=contract.frequency,
            source_url=acquisition.source_url,
        )
        return _normalized_artifact(
            datasource_id,
            acquisition,
            records,
            ons_record_key,
            ons_record_metadata,
            ons_artifact_metadata(records),
        )
    except OfficialMacroParseError as error:
        raise OfficialMacroWorkflowError("ONS artifact does not match its contract") from error


def normalize_nomis_response(
    datasource_id: str,
    response: AcquisitionResponseLike,
) -> NormalizedOfficialMacroArtifact:
    """Parse an acquired fixed Nomis dataset into validated normalized records."""

    contract = _nomis_contract(datasource_id)
    acquisition = adapt_acquisition_response(nomis_request_for(datasource_id), response)
    try:
        records = tuple(
            record | {"updated_at": acquisition.retrieved_at}
            for record in parse_nomis_dataset_json(
                acquisition.body,
                dataset=contract.dataset,
                source_url=acquisition.source_url,
            )
        )
        for record in records:
            if record.get("geography_code") != "E12000007":
                raise OfficialMacroWorkflowError("Nomis response is outside the London contract")
        return _normalized_artifact(
            datasource_id,
            acquisition,
            records,
            nomis_record_key,
            nomis_record_metadata,
            nomis_artifact_metadata(records, dataset=contract.dataset),
        )
    except OfficialMacroParseError as error:
        raise OfficialMacroWorkflowError("Nomis artifact does not match its contract") from error


def normalize_mpc_rss_response(
    response: AcquisitionResponseLike,
) -> NormalizedOfficialMacroArtifact:
    """Parse the fixed MPC RSS feed into validated release metadata records."""

    request = mpc_rss_request()
    acquisition = adapt_acquisition_response(request, response)
    try:
        records = parse_mpc_rss_xml(acquisition.body, source_url=acquisition.source_url)
        return _normalized_artifact(
            request.datasource_id,
            acquisition,
            records,
            mpc_rss_record_key,
            mpc_rss_record_metadata,
            mpc_rss_artifact_metadata(records),
        )
    except OfficialMacroParseError as error:
        raise OfficialMacroWorkflowError("MPC RSS artifact does not match its contract") from error


def request_for(datasource_id: str) -> OfficialMacroRequest:
    """Return the one fixed request supported by an official-macro workflow."""

    if datasource_id in _ONS_CONTRACTS:
        return ons_request_for(datasource_id)
    if datasource_id in _NOMIS_CONTRACTS:
        return nomis_request_for(datasource_id)
    if datasource_id == "boe.mpc_news":
        return mpc_rss_request()
    raise OfficialMacroWorkflowError("unsupported official macro datasource")


def parser_for(datasource_id: str) -> Callable[[bytes], tuple[dict[str, Any], ...]]:
    """Return a named, module-level parser suitable for the isolated child."""

    parsers: Mapping[str, Callable[[bytes], tuple[dict[str, Any], ...]]] = {
        "ons.gdp.ecyx": parse_ons_ecyx_artifact,
        "ons.gdp.ihyq": parse_ons_ihyq_artifact,
        "ons.inflation.d7g7": parse_ons_d7g7_artifact,
        "ons.inflation.l55o": parse_ons_l55o_artifact,
        "ons.inflation.czbh": parse_ons_czbh_artifact,
        "ons.labour.lf24": parse_ons_lf24_artifact,
        "ons.labour.mgsx": parse_ons_mgsx_artifact,
        "ons.labour.ap2y": parse_ons_ap2y_artifact,
        "ons.labour.kai9": parse_ons_kai9_artifact,
        "nomis.nm_59_1.london_lfs": parse_nomis_nm_59_1_artifact,
        "nomis.nm_130_1.london_workforce_jobs": parse_nomis_nm_130_1_artifact,
        "boe.mpc_news": parse_mpc_rss_artifact,
    }
    try:
        return parsers[datasource_id]
    except KeyError as error:
        raise OfficialMacroWorkflowError("unsupported official macro datasource") from error


def validate_saved_records(
    datasource_id: str,
    acquisition: OfficialMacroAcquisition,
    records: object,
) -> NormalizedOfficialMacroArtifact:
    """Validate isolated parser output against its fixed record contract."""

    if not isinstance(records, (tuple, list)) or any(
        not isinstance(record, Mapping) for record in records
    ):
        raise OfficialMacroWorkflowError("isolated parser returned invalid records")
    copied = tuple(
        {
            **dict(record),
            # The parser has no network capability and therefore uses a fixed
            # source URL.  Restore the already validated persisted endpoint in
            # the normalized payload in the trusted parent.
            "source_url": acquisition.source_url,
        }
        for record in records
    )
    if datasource_id in _ONS_CONTRACTS:
        return _normalized_artifact(
            datasource_id,
            acquisition,
            copied,
            ons_record_key,
            ons_record_metadata,
            ons_artifact_metadata(copied),
        )
    if datasource_id in _NOMIS_CONTRACTS:
        contract = _nomis_contract(datasource_id)
        if any(record.get("geography_code") != "E12000007" for record in copied):
            raise OfficialMacroWorkflowError("Nomis response is outside the London contract")
        return _normalized_artifact(
            datasource_id,
            acquisition,
            copied,
            nomis_record_key,
            nomis_record_metadata,
            nomis_artifact_metadata(copied, dataset=contract.dataset),
        )
    if datasource_id == "boe.mpc_news":
        return _normalized_artifact(
            datasource_id,
            acquisition,
            copied,
            mpc_rss_record_key,
            mpc_rss_record_metadata,
            mpc_rss_artifact_metadata(copied),
        )
    raise OfficialMacroWorkflowError("unsupported official macro datasource")


def record_metadata_for(datasource_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the fixed metadata contract for one normalized record."""

    if datasource_id in _ONS_CONTRACTS:
        return ons_record_metadata(record)
    if datasource_id in _NOMIS_CONTRACTS:
        return nomis_record_metadata(record)
    if datasource_id == "boe.mpc_news":
        return mpc_rss_record_metadata(record)
    raise OfficialMacroWorkflowError("unsupported official macro datasource")


def _parse_ons_artifact(
    evidence: bytes, datasource_id: str
) -> tuple[dict[str, Any], ...]:
    contract = _ons_contract(datasource_id)
    return parse_ons_series_json(
        evidence,
        series=contract.series,
        uri=contract.uri,
        frequency=contract.frequency,
        source_url=ONS_DATA_URL,
    )


def parse_ons_ecyx_artifact(evidence: bytes) -> tuple[dict[str, Any], ...]:
    return _parse_ons_artifact(evidence, "ons.gdp.ecyx")


def parse_ons_ihyq_artifact(evidence: bytes) -> tuple[dict[str, Any], ...]:
    return _parse_ons_artifact(evidence, "ons.gdp.ihyq")


def parse_ons_d7g7_artifact(evidence: bytes) -> tuple[dict[str, Any], ...]:
    return _parse_ons_artifact(evidence, "ons.inflation.d7g7")


def parse_ons_l55o_artifact(evidence: bytes) -> tuple[dict[str, Any], ...]:
    return _parse_ons_artifact(evidence, "ons.inflation.l55o")


def parse_ons_czbh_artifact(evidence: bytes) -> tuple[dict[str, Any], ...]:
    return _parse_ons_artifact(evidence, "ons.inflation.czbh")


def parse_ons_lf24_artifact(evidence: bytes) -> tuple[dict[str, Any], ...]:
    return _parse_ons_artifact(evidence, "ons.labour.lf24")


def parse_ons_mgsx_artifact(evidence: bytes) -> tuple[dict[str, Any], ...]:
    return _parse_ons_artifact(evidence, "ons.labour.mgsx")


def parse_ons_ap2y_artifact(evidence: bytes) -> tuple[dict[str, Any], ...]:
    return _parse_ons_artifact(evidence, "ons.labour.ap2y")


def parse_ons_kai9_artifact(evidence: bytes) -> tuple[dict[str, Any], ...]:
    return _parse_ons_artifact(evidence, "ons.labour.kai9")


def _parse_nomis_artifact(
    evidence: bytes, datasource_id: str
) -> tuple[dict[str, Any], ...]:
    contract = _nomis_contract(datasource_id)
    return parse_nomis_dataset_json(
        evidence,
        dataset=contract.dataset,
        source_url=NOMIS_DATASET_URL,
    )


def parse_nomis_nm_59_1_artifact(evidence: bytes) -> tuple[dict[str, Any], ...]:
    return _parse_nomis_artifact(evidence, "nomis.nm_59_1.london_lfs")


def parse_nomis_nm_130_1_artifact(evidence: bytes) -> tuple[dict[str, Any], ...]:
    return _parse_nomis_artifact(evidence, "nomis.nm_130_1.london_workforce_jobs")


def parse_mpc_rss_artifact(evidence: bytes) -> tuple[dict[str, Any], ...]:
    return parse_mpc_rss_xml(evidence, source_url=MPC_RSS_URL)


def _normalized_artifact(
    datasource_id: str,
    acquisition: OfficialMacroAcquisition,
    records: tuple[dict[str, Any], ...],
    record_key: Callable[[Mapping[str, Any]], tuple[str, ...]],
    record_metadata: Callable[[Mapping[str, Any]], dict[str, Any]],
    artifact_metadata: Mapping[str, Any],
) -> NormalizedOfficialMacroArtifact:
    keys: list[tuple[str, ...]] = []
    seen_keys: set[tuple[str, ...]] = set()
    for record in records:
        if record.get("datasource_id") != datasource_id:
            raise OfficialMacroWorkflowError("normalized record has the wrong datasource")
        key = record_key(record)
        metadata = record_metadata(record)
        if metadata.get("datasource_id") != datasource_id:
            raise OfficialMacroWorkflowError("record metadata has the wrong datasource")
        if metadata.get("locator") != record.get("locator"):
            raise OfficialMacroWorkflowError("record metadata locator does not match record")
        if key in seen_keys:
            raise OfficialMacroWorkflowError("artifact contains duplicate record keys")
        seen_keys.add(key)
        keys.append(key)
    return NormalizedOfficialMacroArtifact(
        datasource_id=datasource_id,
        acquisition=acquisition,
        records=tuple(records),
        record_keys=tuple(keys),
        artifact_metadata=MappingProxyType(dict(artifact_metadata)),
    )


def _has_content_range(headers: Mapping[str, str]) -> bool:
    return any(name.lower() == "content-range" for name in headers)


def _same_contract_endpoint(expected_url: str, actual_url: str) -> bool:
    """Allow equivalent URL encoding/order, but no endpoint broadening."""

    expected = urlparse(expected_url)
    actual = urlparse(actual_url)
    return (
        expected.scheme.lower() == actual.scheme.lower()
        and (expected.hostname or "").rstrip(".").lower()
        == (actual.hostname or "").rstrip(".").lower()
        and (expected.port or 443) == (actual.port or 443)
        and expected.path == actual.path
        and sorted(parse_qsl(expected.query, keep_blank_values=True))
        == sorted(parse_qsl(actual.query, keep_blank_values=True))
    )


def _ons_contract(datasource_id: str) -> _OnsContract:
    try:
        return _ONS_CONTRACTS[datasource_id]
    except (KeyError, TypeError) as error:
        raise OfficialMacroWorkflowError("unsupported ONS datasource") from error


def _nomis_contract(datasource_id: str) -> _NomisContract:
    try:
        return _NOMIS_CONTRACTS[datasource_id]
    except (KeyError, TypeError) as error:
        raise OfficialMacroWorkflowError("unsupported Nomis datasource") from error
