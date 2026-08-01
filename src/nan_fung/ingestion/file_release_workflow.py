"""Fixed two-stage contracts for approved official file releases.

Each supported datasource has a closed discovery endpoint and a narrowly
validated release URL.  This is not a connector framework: callers can only
select one of the three named contracts below.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlparse

from nan_fung.datasources.esg import (
    EPB_CONTENT_API_URL,
    _NON_DOMESTIC_TITLE,
    parse_exact_non_domestic_epc_attachment_json,
    parse_non_domestic_epc_ratings_ods,
)
from nan_fung.datasources.hybrid import (
    ONS_WORKING_ARRANGEMENTS_URL,
    parse_hybrid_working_dataset_html,
    parse_hybrid_working_xlsx,
)
from nan_fung.datasources.market import (
    VOA_STOCK_COLLECTION_URL,
    is_current_voa_stock_release_page_url,
    parse_current_voa_london_office_stock_zip,
    parse_voa_current_release_page_html,
    parse_voa_office_stock_collection_html,
)

from .canonical import CanonicalizationError, normalize_timestamp
from .policies import (
    ArtifactPolicy,
    PolicyError,
    SourcePolicy,
    redact_headers,
    redact_url,
    validate_source_url,
)


VOA_DATASOURCE_ID = "voa.ndr_office_stock"
HYBRID_DATASOURCE_ID = "ons.opn.hybrid_working"
EPC_DATASOURCE_ID = "mhclg.epc.live_table_a_london"
FILE_RELEASE_AUTOMATIC_DATASOURCE_IDS = frozenset(
    {VOA_DATASOURCE_ID, HYBRID_DATASOURCE_ID, EPC_DATASOURCE_ID}
)


class FileReleaseWorkflowError(ValueError):
    """A file-release acquisition is outside its fixed source contract."""


class AcquisitionMetadataLike(Protocol):
    """The metadata available for both in-memory and streamed artifacts."""

    request_url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    retrieved_at: str
    method: str


@dataclass(frozen=True, slots=True)
class FileReleaseContract:
    """One fixed discovery surface and its resulting release surface."""

    datasource_id: str
    discovery_source_id: str
    release_source_id: str
    discovery_url: str
    discovery_policy: SourcePolicy
    release_policy: SourcePolicy
    discovery_parser: Callable[[bytes], Any]
    release_parser: Callable[[bytes], Any]
    selection_parser: Callable[[bytes], Any] | None = None


@dataclass(frozen=True, slots=True)
class FileReleaseAcquisition:
    """Validated, redacted metadata for one discovery or release artifact."""

    datasource_id: str
    stage: str
    source_id: str
    request_url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    retrieved_at: str
    method: str


_HTML_POLICY = ArtifactPolicy(
    max_bytes=4 * 1024 * 1024,
    allowed_media_types=("text/html", "application/xhtml+xml"),
)
_JSON_POLICY = ArtifactPolicy(
    max_bytes=4 * 1024 * 1024,
    allowed_media_types=("application/json",),
)
_VOA_RELEASE_POLICY = ArtifactPolicy(
    max_bytes=250 * 1024 * 1024,
    allowed_media_types=(
        "application/zip",
        "application/x-zip-compressed",
        "application/octet-stream",
    ),
)
_XLSX_RELEASE_POLICY = ArtifactPolicy(
    max_bytes=250 * 1024 * 1024,
    allowed_media_types=(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream",
    ),
)
_ODS_RELEASE_POLICY = ArtifactPolicy(
    max_bytes=250 * 1024 * 1024,
    allowed_media_types=(
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/octet-stream",
    ),
)


_CONTRACTS = MappingProxyType(
    {
        VOA_DATASOURCE_ID: FileReleaseContract(
            datasource_id=VOA_DATASOURCE_ID,
            discovery_source_id="govuk.voa_collection",
            release_source_id="voa.ndr_stock",
            discovery_url=VOA_STOCK_COLLECTION_URL,
            discovery_policy=SourcePolicy(
                ("www.gov.uk",), artifact=_HTML_POLICY
            ),
            release_policy=SourcePolicy(
                ("assets.publishing.service.gov.uk",), artifact=_VOA_RELEASE_POLICY
            ),
            discovery_parser=parse_voa_current_release_page_html,
            release_parser=parse_current_voa_london_office_stock_zip,
            selection_parser=parse_voa_office_stock_collection_html,
        ),
        HYBRID_DATASOURCE_ID: FileReleaseContract(
            datasource_id=HYBRID_DATASOURCE_ID,
            discovery_source_id="ons.opn",
            release_source_id="ons.opn",
            discovery_url=ONS_WORKING_ARRANGEMENTS_URL,
            discovery_policy=SourcePolicy(
                ("www.ons.gov.uk",), artifact=_HTML_POLICY
            ),
            release_policy=SourcePolicy(
                ("www.ons.gov.uk",),
                allowed_query_keys=("uri",),
                artifact=_XLSX_RELEASE_POLICY,
            ),
            discovery_parser=parse_hybrid_working_dataset_html,
            release_parser=parse_hybrid_working_xlsx,
        ),
        EPC_DATASOURCE_ID: FileReleaseContract(
            datasource_id=EPC_DATASOURCE_ID,
            discovery_source_id="mhclg.epc",
            release_source_id="mhclg.epc_attachment",
            discovery_url=EPB_CONTENT_API_URL,
            discovery_policy=SourcePolicy(("www.gov.uk",), artifact=_JSON_POLICY),
            release_policy=SourcePolicy(
                ("assets.publishing.service.gov.uk",), artifact=_ODS_RELEASE_POLICY
            ),
            discovery_parser=parse_exact_non_domestic_epc_attachment_json,
            release_parser=parse_non_domestic_epc_ratings_ods,
        ),
    }
)


def contract_for(datasource_id: str) -> FileReleaseContract:
    """Return one closed, approved file-release contract."""

    try:
        return _CONTRACTS[datasource_id]
    except KeyError as error:
        raise FileReleaseWorkflowError("unsupported automatic file-release datasource") from error


def collect_voa_ndr_office_stock() -> FileReleaseContract:
    return contract_for(VOA_DATASOURCE_ID)


def collect_ons_hybrid_working() -> FileReleaseContract:
    return contract_for(HYBRID_DATASOURCE_ID)


def collect_mhclg_epc_live_table_a_london() -> FileReleaseContract:
    return contract_for(EPC_DATASOURCE_ID)


def voa_ndr_office_stock_record_key(record: Mapping[str, Any]) -> tuple[str, ...]:
    return record_metadata_for(VOA_DATASOURCE_ID, record)["record_key"]


def ons_hybrid_working_record_key(record: Mapping[str, Any]) -> tuple[str, ...]:
    return record_metadata_for(HYBRID_DATASOURCE_ID, record)["record_key"]


def mhclg_epc_live_table_a_london_record_key(
    record: Mapping[str, Any],
) -> tuple[str, ...]:
    return record_metadata_for(EPC_DATASOURCE_ID, record)["record_key"]


def adapt_discovery_metadata(
    datasource_id: str, response: AcquisitionMetadataLike
) -> FileReleaseAcquisition:
    """Accept only the exact fixed discovery response for a datasource."""

    return _adapt_metadata(contract_for(datasource_id), "discovery", response)


def adapt_release_metadata(
    datasource_id: str,
    response: AcquisitionMetadataLike,
    *,
    release_url: str,
) -> FileReleaseAcquisition:
    """Accept only the release response selected by persisted discovery data."""

    return _adapt_metadata(
        contract_for(datasource_id), "release", response, release_url=release_url
    )


def adapt_selection_metadata(
    datasource_id: str,
    response: AcquisitionMetadataLike,
    *,
    selection_url: str,
) -> FileReleaseAcquisition:
    """Validate the VOA collection-selected release page before parsing it."""

    return _adapt_metadata(
        contract_for(datasource_id), "selection", response, selection_url=selection_url
    )


def release_url_from_discovery(datasource_id: str, parsed: Any) -> str:
    """Validate a parser-selected URL before it becomes a second acquisition."""

    contract = contract_for(datasource_id)
    if datasource_id == EPC_DATASOURCE_ID:
        if not isinstance(parsed, Mapping):
            raise FileReleaseWorkflowError("EPC discovery parser returned invalid metadata")
        title = parsed.get("attachment_title")
        candidate = parsed.get("attachment_url")
        if title != _NON_DOMESTIC_TITLE or not isinstance(candidate, str):
            raise FileReleaseWorkflowError("EPC discovery did not select the exact Table A attachment")
    else:
        candidate = parsed
    return validate_release_url(datasource_id, candidate)


def selection_url_from_discovery(datasource_id: str, parsed: Any) -> str:
    """Validate the selected intermediate release page for the VOA workflow."""

    contract = contract_for(datasource_id)
    if contract.selection_parser is None:
        raise FileReleaseWorkflowError("datasource has no intermediate release page")
    if not isinstance(parsed, str) or not parsed:
        raise FileReleaseWorkflowError("collection did not return a release page URL")
    try:
        validate_source_url(parsed, contract.discovery_policy, resolver=None)
    except PolicyError as error:
        raise FileReleaseWorkflowError("release page URL is not approved") from error
    if datasource_id != VOA_DATASOURCE_ID or not is_current_voa_stock_release_page_url(parsed):
        raise FileReleaseWorkflowError("collection selected an invalid VOA release page")
    return parsed


def validate_release_url(datasource_id: str, candidate: object) -> str:
    """Validate a release URL when replay already has no discovery artifact."""

    contract = contract_for(datasource_id)
    try:
        if not isinstance(candidate, str) or not candidate:
            raise FileReleaseWorkflowError("file-release discovery did not return a URL")
        parsed_url = validate_source_url(candidate, contract.release_policy, resolver=None)
    except PolicyError as error:
        raise FileReleaseWorkflowError("discovered release URL is not approved") from error
    _validate_release_shape(datasource_id, parsed_url)
    return candidate


def record_metadata_for(
    datasource_id: str, record: Mapping[str, Any]
) -> dict[str, Any]:
    """Build one fixed record key and artifact locator for a parsed release row."""

    if datasource_id == VOA_DATASOURCE_ID:
        area_code = _required_text(record.get("area_code"), "VOA area code")
        year = record.get("year")
        if isinstance(year, bool) or not isinstance(year, int):
            raise FileReleaseWorkflowError("VOA record year is required")
        source_locator = record.get("locator")
        if not isinstance(source_locator, Mapping):
            source_locator = {
                "kind": "zip_csv_rows",
                "members": ["table_SOP5_1.csv", "table_SOP5_2.csv"],
                "area_code": area_code,
                "year": year,
            }
        return {
            "record_key": (area_code, str(year)),
            "record_type": "supply",
            "category": "office_stock",
            "period_label": str(year),
            "unit": "properties",
            "definition": "VOA non-domestic rating office stock for the London region",
            "limitations": ["Official current-release stock table"],
            "locator": dict(source_locator),
        }
    if datasource_id == HYBRID_DATASOURCE_ID:
        period = _required_text(record.get("period"), "ONS hybrid period")
        geography = _required_text(record.get("geography"), "ONS hybrid geography")
        metric = _required_text(record.get("metric"), "ONS hybrid metric")
        return {
            "record_key": (metric, geography, period),
            "record_type": "metric",
            "category": "hybrid_working",
            "period_label": period,
            "unit": "percent",
            "definition": metric,
            "limitations": [
                "Great Britain survey proxy, not London office occupancy or building access"
            ],
            "locator": {
                "kind": "xlsx_sheet_row",
                "sheet": "Table_6",
                "period": period,
                "columns": ["A", "B", "C", "D"],
            },
        }
    if datasource_id == EPC_DATASOURCE_ID:
        region = _required_text(record.get("region"), "EPC region")
        quarter = _required_text(record.get("quarter"), "EPC quarter")
        source_row = record.get("source_row")
        if region != "London":
            raise FileReleaseWorkflowError("EPC release is outside the London contract")
        if isinstance(source_row, bool) or not isinstance(source_row, int) or source_row < 5:
            raise FileReleaseWorkflowError("EPC source row is required")
        return {
            "record_key": (region, quarter),
            "record_type": "metric",
            "category": "esg_energy_efficiency",
            "period_label": quarter,
            "unit": "certificates",
            "definition": "MHCLG Table A non-domestic EPC ratings by region",
            "limitations": ["All non-domestic properties, not offices only"],
            "locator": {
                "kind": "ods_sheet_row",
                "sheet": "A_by_Region",
                "row": source_row,
                "region": region,
                "quarter": quarter,
                "columns": "A:M",
            },
        }
    raise FileReleaseWorkflowError("unsupported file-release datasource")


def _adapt_metadata(
    contract: FileReleaseContract,
    stage: str,
    response: AcquisitionMetadataLike,
    *,
    release_url: str | None = None,
    selection_url: str | None = None,
) -> FileReleaseAcquisition:
    if stage not in {"discovery", "selection", "release"}:
        raise FileReleaseWorkflowError("unsupported file-release stage")
    expected_url = (
        contract.discovery_url
        if stage == "discovery"
        else selection_url
        if stage == "selection"
        else release_url
    )
    if not isinstance(expected_url, str) or not expected_url:
        raise FileReleaseWorkflowError("release metadata requires its discovered URL")
    if response.request_url != expected_url:
        raise FileReleaseWorkflowError("acquisition request URL does not match contract")
    if not isinstance(response.method, str) or response.method.upper() != "GET":
        raise FileReleaseWorkflowError("file-release requests must use GET")
    if response.status != 200 or _has_content_range(response.headers):
        raise FileReleaseWorkflowError(
            "file-release responses require a complete HTTP 200 response"
        )
    if not isinstance(response.headers, Mapping) or not _content_type(response.headers):
        raise FileReleaseWorkflowError("file-release responses require Content-Type")
    policy = contract.release_policy if stage == "release" else contract.discovery_policy
    try:
        validate_source_url(expected_url, policy, resolver=None)
        validate_source_url(response.final_url, policy, resolver=None)
        retrieved_at = normalize_timestamp(response.retrieved_at)
    except (CanonicalizationError, PolicyError) as error:
        raise FileReleaseWorkflowError("acquisition provenance is not approved") from error
    if not _same_endpoint(expected_url, response.final_url):
        raise FileReleaseWorkflowError(
            "acquisition final URL does not match the fixed source contract"
        )
    return FileReleaseAcquisition(
        datasource_id=contract.datasource_id,
        stage=stage,
        source_id=(
            contract.release_source_id if stage == "release" else contract.discovery_source_id
        ),
        request_url=redact_url(expected_url),
        final_url=redact_url(response.final_url),
        status=200,
        headers=MappingProxyType(redact_headers(response.headers)),
        retrieved_at=retrieved_at,
        method="GET",
    )


def _validate_release_shape(datasource_id: str, parsed: Any) -> None:
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    query = tuple(parse_qsl(parsed.query, keep_blank_values=True))
    if datasource_id == VOA_DATASOURCE_ID:
        if host != "assets.publishing.service.gov.uk" or not path.endswith(".zip"):
            raise FileReleaseWorkflowError("VOA discovery did not select an official ZIP")
        if "ndr" not in path or "stock" not in path or query:
            raise FileReleaseWorkflowError("VOA release URL is outside the stock contract")
        return
    if datasource_id == HYBRID_DATASOURCE_ID:
        if host != "www.ons.gov.uk" or path != "/file" or len(query) != 1:
            raise FileReleaseWorkflowError("ONS discovery did not select its workbook endpoint")
        key, value = query[0]
        if (
            key != "uri"
            or not value.lower().endswith(".xlsx")
            or "/datasets/publicopinionsandsocialtrendsgreatbritainworkingarrangements/"
            not in value.lower()
        ):
            raise FileReleaseWorkflowError("ONS workbook URL is outside the dataset contract")
        return
    if datasource_id == EPC_DATASOURCE_ID:
        if host != "assets.publishing.service.gov.uk" or not path.endswith(".ods") or query:
            raise FileReleaseWorkflowError("EPC attachment URL is outside the Table A contract")
        return
    raise FileReleaseWorkflowError("unsupported file-release datasource")


def _same_endpoint(expected: str, actual: str) -> bool:
    left = urlparse(expected)
    right = urlparse(actual)
    return (
        left.scheme.lower() == right.scheme.lower()
        and (left.hostname or "").lower() == (right.hostname or "").lower()
        and left.path == right.path
        and tuple(parse_qsl(left.query, keep_blank_values=True))
        == tuple(parse_qsl(right.query, keep_blank_values=True))
    )


def _content_type(headers: Mapping[str, str]) -> str | None:
    for name, value in headers.items():
        if name.lower() == "content-type" and isinstance(value, str) and value.strip():
            return value
    return None


def _has_content_range(headers: Mapping[str, str]) -> bool:
    return any(name.lower() == "content-range" for name in headers)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise FileReleaseWorkflowError(f"{label} is required")
    return value
