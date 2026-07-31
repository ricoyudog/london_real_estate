"""Free official non-domestic EPC indicators for London."""

from __future__ import annotations

from io import BytesIO
from typing import Any

from odf import teletype
from odf.opendocument import load
from odf.table import Table, TableCell, TableRow

from nan_fung.datasources.common import SourceResult, get_bytes, get_json, source_result

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


def _latest_region_record(ods_bytes: bytes, region: str) -> dict[str, Any]:
    document = load(BytesIO(ods_bytes))
    region_table = next(
        table
        for table in document.spreadsheet.getElementsByType(Table)
        if table.getAttribute("name") == "A_by_Region"
    )
    rows = region_table.getElementsByType(TableRow)
    records = []
    for row in rows[4:]:
        values = _row_values(row)
        if len(values) < 13 or values[0] != region or not values[1]:
            continue
        records.append(
            {
                "region": values[0],
                "quarter": values[1],
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
                "indicator_type": "proxy",
                "scope": "all non-domestic properties, not offices only",
            }
        )
    return records[-1]


def fetch_non_domestic_epc_ratings(region: str = "London") -> SourceResult:
    """Return the latest regional non-domestic EPC rating counts.

    The attachment is discovered anonymously through GOV.UK's Content API.
    It covers all non-domestic uses and must only be treated as an office ESG
    proxy. Record-level property APIs require an account/token and are not used.
    """

    content = get_json(EPB_CONTENT_API_URL)
    attachments = content["details"]["attachments"]
    attachment = next(
        item for item in attachments if item["title"] == _NON_DOMESTIC_TITLE
    )
    record = _latest_region_record(get_bytes(attachment["url"]), region)
    record["attachment_url"] = attachment["url"]

    return source_result(
        category="esg_energy_efficiency",
        source="MHCLG Energy Performance of Buildings live table A",
        source_url=EPB_LIVE_TABLES_URL,
        source_updated_at=content.get("public_updated_at"),
        records=[record],
    )
