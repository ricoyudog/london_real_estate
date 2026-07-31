"""Free public-report and official-stock sources for London offices."""

from __future__ import annotations

import csv
from io import BytesIO, TextIOWrapper
from zipfile import ZipFile

from pypdf import PdfReader

from .common import SourceResult, get_bytes, source_result

BNP_REPORT_URL = (
    "https://www.realestate.bnpparibas.co.uk/sites/default/files/2026-05/"
    "Q12026CentralLondonMarketUpdate.pdf"
)
VOA_STOCK_URL = (
    "https://assets.publishing.service.gov.uk/media/"
    "69f9bdf9a96f4d06cda76fbf/ndr_stock_of_properties_2026.zip"
)


def fetch_public_market_report(
    url: str = BNP_REPORT_URL,
    *,
    published_at: str | None = None,
    max_pages: int | None = None,
) -> SourceResult:
    """Download a public market-report PDF and return text page by page."""

    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be at least 1")

    reader = PdfReader(BytesIO(get_bytes(url)))
    pages = reader.pages if max_pages is None else reader.pages[:max_pages]
    records = [
        {"page": page_number, "text": page.extract_text() or ""}
        for page_number, page in enumerate(pages, start=1)
    ]
    return source_result(
        category="office_market_report",
        source="BNP Paribas Real Estate Central London Office Market Update",
        source_url=url,
        published_at=published_at or ("2026-05-18" if url == BNP_REPORT_URL else None),
        records=records,
    )


def fetch_voa_office_stock(
    area_code: str = "E12000007",
    *,
    year: int = 2026,
    url: str = VOA_STOCK_URL,
) -> SourceResult:
    """Return VOA office-property count and rateable value for one area code."""

    with ZipFile(BytesIO(get_bytes(url))) as archive:
        count = _read_voa_row(archive, "table_SOP5_1.csv", area_code, year)
        rateable_value = _read_voa_row(archive, "table_SOP5_2.csv", area_code, year)

    records = []
    if count and rateable_value:
        records.append(
            {
                "geography": count["geography"],
                "area_code": count["area_code"],
                "area_name": count["area_name"],
                "year": year,
                "office_property_count": int(count[str(year)]),
                "total_rateable_value_gbp_thousands": int(rateable_value[str(year)]),
            }
        )

    return source_result(
        category="office_stock",
        source="Valuation Office Agency NDR Stock of Properties",
        source_url=url,
        published_at="2026-05-14" if url == VOA_STOCK_URL else None,
        records=records,
    )


def _read_voa_row(
    archive: ZipFile,
    filename: str,
    area_code: str,
    year: int,
) -> dict[str, str] | None:
    """Read one VOA CSV row from an open release archive."""

    with archive.open(filename) as raw:
        rows = csv.DictReader(TextIOWrapper(raw, encoding="utf-8-sig"))
        if str(year) not in (rows.fieldnames or []):
            raise ValueError(f"year {year} is not present in {filename}")
        return next((row for row in rows if row["area_code"] == area_code), None)
