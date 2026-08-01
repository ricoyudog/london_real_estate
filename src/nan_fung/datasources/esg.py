"""Free official non-domestic EPC indicators for London."""

from __future__ import annotations

import json
from io import BytesIO
import re
from typing import Any, Mapping
from urllib.parse import urlparse

from odf import teletype
from odf.opendocument import load
from odf.table import Table, TableCell, TableRow

from nan_fung.datasources.common import SourceResult, get_bytes, source_result
from nan_fung.ingestion.policies import ArtifactPolicy, SourcePolicy, validate_zip_artifact

EPB_LIVE_TABLES_URL = (
    "https://www.gov.uk/government/statistical-data-sets/"
    "live-tables-on-energy-performance-of-buildings-certificates"
)
EPB_CONTENT_API_URL = (
    "https://www.gov.uk/api/content/government/statistical-data-sets/"
    "live-tables-on-energy-performance-of-buildings-certificates"
)
_NON_DOMESTIC_TITLE = (
    "Table A: Non-domestic Energy Performance Certificates by energy "
    "performance asset rating"
)
_EPC_SCOPE = "all non-domestic properties, not offices only"
_EPC_ARTIFACT_POLICY = ArtifactPolicy(max_bytes=250 * 1024 * 1024)
_GOVUK_CONTENT_POLICY = SourcePolicy(("www.gov.uk",))
_EPC_ATTACHMENT_POLICY = SourcePolicy(("assets.publishing.service.gov.uk",))
_EPC_TABLE_TITLE_PREFIX = (
    "a- non-domestic properties by region by energy performance asset rating "
    "- in each year/quarter"
)
_EPC_SOURCE_PREFIX = (
    "source: energy performance certificates for buildings register"
)
_EPC_HEADERS = (
    "region",
    "quarter",
    "number lodgements",
    "total floor area (m2)",
    "a+",
    "a",
    "b",
    "c",
    "d",
    "e",
    "f",
    "g",
    "not recorded",
)


def _cell_value(cell: Any) -> str | int | float:
    value = cell.getAttribute("value")
    if value is not None:
        number = float(value)
        return int(number) if number.is_integer() else number
    return teletype.extractText(cell).strip()


def _row_values(row: Any) -> list[str | int | float]:
    values: list[str | int | float] = []
    for cell in row.getElementsByType(TableCell):
        repeated = int(cell.getAttribute("numbercolumnsrepeated") or 1)
        values.extend([_cell_value(cell)] * repeated)
    return values


def parse_non_domestic_epc_ratings_ods(
    evidence: bytes,
    *,
    region: str = "London",
) -> list[dict[str, Any]]:
    """Parse all regional Table A rows from captured EPC ODS evidence.

    The records deliberately retain the source's all-non-domestic scope and
    proxy label: these numbers are not office-only observations.
    """

    validate_zip_artifact(evidence, _EPC_ARTIFACT_POLICY)
    document = load(BytesIO(evidence))
    try:
        region_table = next(
            table
            for table in document.spreadsheet.getElementsByType(Table)
            if table.getAttribute("name") == "A_by_Region"
        )
    except StopIteration as error:
        raise ValueError("EPC workbook has no A_by_Region table") from error

    rows = region_table.getElementsByType(TableRow)
    _validate_epc_table_schema(rows)
    records: list[dict[str, Any]] = []
    for row_number, row in enumerate(rows[4:], start=5):
        values = _row_values(row)
        if not values or not values[0]:
            continue
        if len(values) < len(_EPC_HEADERS):
            raise ValueError("EPC workbook has a truncated A_by_Region row")
        if values[0] != region or not values[1]:
            continue
        quarter = values[1]
        numeric_values = values[2:13]
        if (
            not isinstance(quarter, str)
            or re.fullmatch(r"\d{4}/[1-4]", quarter.strip()) is None
            or any(not _is_nonnegative_number(value) for value in numeric_values)
        ):
            raise ValueError("EPC workbook has an invalid London Table A row")
        records.append(
            {
                "region": values[0],
                "quarter": quarter,
                "number_lodgements": values[2],
                "total_floor_area_m2": values[3],
                "rating_a_plus": values[4],
                "rating_a": values[5],
                "rating_b": values[6],
                "rating_c": values[7],
                "rating_d": values[8],
                "rating_e": values[9],
                "rating_f": values[10],
                "rating_g": values[11],
                "not_recorded": values[12],
                "source_row": row_number,
                "indicator_type": "proxy",
                "scope": _EPC_SCOPE,
            }
        )
    return records


def _validate_epc_table_schema(rows: list[Any]) -> None:
    if len(rows) < 5:
        raise ValueError("EPC workbook has no Table A data rows")
    title = _normalise_sheet_text(_row_values(rows[0])[0])
    source = _normalise_sheet_text(_row_values(rows[2])[0])
    headers = tuple(_normalise_sheet_text(value) for value in _row_values(rows[3])[:13])
    if (
        not title.startswith(_EPC_TABLE_TITLE_PREFIX)
        or not source.startswith(_EPC_SOURCE_PREFIX)
        or headers != _EPC_HEADERS
    ):
        raise ValueError("EPC workbook A_by_Region schema is not recognized")


def _normalise_sheet_text(value: object) -> str:
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""


def _is_nonnegative_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _latest_region_record(ods_bytes: bytes, region: str) -> dict[str, Any]:
    """Return the latest row for the legacy one-record fetcher."""

    records = parse_non_domestic_epc_ratings_ods(ods_bytes, region=region)
    if not records:
        raise ValueError(f"no EPC records found for region {region!r}")
    return records[-1]


def parse_non_domestic_epc_attachment_json(
    evidence: bytes,
    *,
    require_exact_title: bool = False,
) -> dict[str, str | None]:
    """Discover the current ODS attachment from captured Content API JSON.

    ``require_exact_title`` is used by automatic release collection so a
    renamed attachment cannot silently become the canonical Table A route.
    """

    content = _json_object(evidence)
    details = content.get("details")
    attachments = details.get("attachments") if isinstance(details, Mapping) else None
    if not isinstance(attachments, list):
        raise ValueError("EPC Content API response has no attachment list")
    attachment = _find_non_domestic_attachment(
        attachments,
        require_exact_title=require_exact_title,
    )
    source_updated_at = content.get("public_updated_at")
    return {
        "attachment_url": attachment["url"],
        "attachment_title": attachment["title"],
        "source_updated_at": (
            source_updated_at if isinstance(source_updated_at, str) else None
        ),
    }


def parse_exact_non_domestic_epc_attachment_json(
    evidence: bytes,
) -> dict[str, str | None]:
    """Select only the official Table A ODS attachment for automatic collection."""

    return parse_non_domestic_epc_attachment_json(
        evidence,
        require_exact_title=True,
    )


def _find_non_domestic_attachment(
    attachments: list[object],
    *,
    require_exact_title: bool,
) -> dict[str, str]:
    exact_matches: dict[str, dict[str, str]] = {}
    fallback_matches: list[dict[str, str]] = []
    for item in attachments:
        if not isinstance(item, Mapping):
            continue
        title = item.get("title")
        url = item.get("url")
        if (
            not isinstance(title, str)
            or not isinstance(url, str)
            or not _is_ods_attachment_url(url)
        ):
            continue
        candidate = {"title": title, "url": url}
        if title == _NON_DOMESTIC_TITLE:
            exact_matches[url] = candidate
            continue
        normalized_title = title.casefold()
        if (
            "non-domestic" in normalized_title
            and "energy performance" in normalized_title
        ):
            fallback_matches.append(candidate)
    if exact_matches:
        if len(exact_matches) != 1:
            raise ValueError("EPC Content API has ambiguous exact Table A attachments")
        return next(iter(exact_matches.values()))
    if not require_exact_title and fallback_matches:
        return fallback_matches[0]
    if require_exact_title:
        raise ValueError("EPC Content API has no exact Table A ODS attachment")
    raise ValueError("non-domestic EPC ODS attachment was not found")


def _is_ods_attachment_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme.casefold() == "https"
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path.casefold().endswith(".ods")
        )
    except ValueError:
        return False


def _json_object(evidence: bytes) -> dict[str, Any]:
    value = json.loads(evidence)
    if not isinstance(value, dict):
        raise ValueError("EPC Content API evidence must contain a JSON object")
    return value


def fetch_non_domestic_epc_ratings(region: str = "London") -> SourceResult:
    """Return the latest regional non-domestic EPC rating counts.

    The attachment is discovered anonymously through GOV.UK's Content API.
    It covers all non-domestic uses and must only be treated as an office ESG
    proxy. Record-level property APIs require an account/token and are not used.
    """

    attachment = parse_exact_non_domestic_epc_attachment_json(
        get_bytes(EPB_CONTENT_API_URL, policy=_GOVUK_CONTENT_POLICY)
    )
    attachment_url = attachment["attachment_url"]
    if attachment_url is None:
        raise ValueError("EPC attachment has no URL")
    record = _latest_region_record(
        get_bytes(attachment_url, policy=_EPC_ATTACHMENT_POLICY), region
    )
    record["attachment_url"] = attachment_url

    return source_result(
        category="esg_energy_efficiency",
        source="MHCLG Energy Performance of Buildings live table A",
        source_url=EPB_LIVE_TABLES_URL,
        source_updated_at=attachment["source_updated_at"],
        records=[record],
    )
