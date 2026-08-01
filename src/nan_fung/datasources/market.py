"""Free public-report and official-stock sources for London offices."""

from __future__ import annotations

import csv
import html
import re
from html.parser import HTMLParser
from io import BytesIO, TextIOWrapper
from urllib.parse import urljoin, urlparse
from zipfile import ZipFile

from pypdf import PdfReader

from nan_fung.ingestion.policies import ArtifactPolicy, SourcePolicy, validate_zip_artifact

from .common import SourceResult, get_bytes, source_result

BNP_REPORT_URL = (
    "https://www.realestate.bnpparibas.co.uk/sites/default/files/2026-05/"
    "Q12026CentralLondonMarketUpdate.pdf"
)
VOA_STOCK_URL = (
    "https://assets.publishing.service.gov.uk/media/"
    "69f9bdf9a96f4d06cda76fbf/ndr_stock_of_properties_2026.zip"
)
VOA_STOCK_COLLECTION_URL = (
    "https://www.gov.uk/government/collections/"
    "non-domestic-rating-stock-of-properties-collection"
)
_LONDON_REGION_CODE = "E12000007"
_VOA_COUNT_MEMBER = "table_SOP5_1.csv"
_VOA_RATEABLE_VALUE_MEMBER = "table_SOP5_2.csv"
_VOA_STOCK_FILENAME = re.compile(r"ndr_stock_of_properties_\d{4}\.zip", re.I)
_VOA_RELEASE_PAGE_PATH = re.compile(
    r"^/government/statistics/non-domestic-rating-stock-of-properties"
    r"(?:-[a-z]+)?-(?P<year>\d{4})$",
    re.I,
)
_VOA_YEAR_COLUMN = re.compile(r"\d{4}")
_BNP_POLICY = SourcePolicy(("www.realestate.bnpparibas.co.uk",))
_VOA_ARTIFACT_POLICY = ArtifactPolicy(max_bytes=250 * 1024 * 1024)
_REPORT_ARTIFACT_POLICY = ArtifactPolicy(max_bytes=100 * 1024 * 1024)
_VOA_ASSET_POLICY = SourcePolicy(
    ("assets.publishing.service.gov.uk",), artifact=_VOA_ARTIFACT_POLICY
)
_GOVUK_COLLECTION_POLICY = SourcePolicy(("www.gov.uk",))


def fetch_public_market_report(
    url: str = BNP_REPORT_URL,
    *,
    published_at: str | None = None,
    max_pages: int | None = None,
) -> SourceResult:
    """Download a public market-report PDF and return text page by page."""

    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be at least 1")

    records = parse_public_market_report_pdf(
        get_bytes(url, policy=_BNP_POLICY), max_pages=max_pages
    )
    return source_result(
        category="office_market_report",
        source="BNP Paribas Real Estate Central London Office Market Update",
        source_url=url,
        published_at=published_at or ("2026-05-18" if url == BNP_REPORT_URL else None),
        records=records,
    )


def parse_public_market_report_pdf(
    evidence: bytes, *, max_pages: int | None = None
) -> list[dict[str, object]]:
    """Parse persisted report bytes without network or filesystem access."""

    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    from nan_fung.ingestion.policies import validate_pdf_artifact

    validate_pdf_artifact(evidence, _REPORT_ARTIFACT_POLICY)
    reader = PdfReader(BytesIO(evidence))
    pages = reader.pages if max_pages is None else reader.pages[:max_pages]
    return [
        {"page": page_number, "text": page.extract_text() or ""}
        for page_number, page in enumerate(pages, start=1)
    ]


def fetch_voa_office_stock(
    area_code: str = _LONDON_REGION_CODE,
    *,
    year: int = 2026,
    url: str = VOA_STOCK_URL,
) -> SourceResult:
    """Return VOA office-property count and rateable value for one area code."""

    records = parse_voa_office_stock_zip(
        get_bytes(url, policy=_VOA_ASSET_POLICY), area_code=area_code, year=year
    )
    return source_result(
        category="office_stock",
        source="Valuation Office Agency NDR Stock of Properties",
        source_url=url,
        published_at="2026-05-14" if url == VOA_STOCK_URL else None,
        records=records,
    )


def parse_voa_office_stock_zip(
    evidence: bytes, *, area_code: str = _LONDON_REGION_CODE, year: int = 2026
) -> list[dict[str, object]]:
    """Parse an already-captured VOA release archive."""

    validate_zip_artifact(evidence, _VOA_ARTIFACT_POLICY)
    with ZipFile(BytesIO(evidence)) as archive:
        return _parse_voa_office_stock_archive(archive, area_code=area_code, year=year)


def parse_current_voa_london_office_stock_zip(
    evidence: bytes,
) -> list[dict[str, object]]:
    """Parse the latest common annual London value in a captured VOA archive."""

    validate_zip_artifact(evidence, _VOA_ARTIFACT_POLICY)
    with ZipFile(BytesIO(evidence)) as archive:
        year = _latest_common_voa_year(archive)
        return _parse_voa_office_stock_archive(
            archive,
            area_code=_LONDON_REGION_CODE,
            year=year,
            include_locator=True,
        )


class _HrefCollector(HTMLParser):
    """Collect anchor href values without treating script text as page links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        for name, value in attrs:
            if name.casefold() == "href" and value:
                self.hrefs.append(value)


def parse_voa_office_stock_collection_html(evidence: bytes) -> str:
    """Select one safe NDR stock ZIP from a captured official release page."""

    candidates: set[str] = set()
    for href in _hrefs_from_html(evidence):
        url = html.unescape(href).strip()
        if not _looks_like_voa_stock_url(url):
            continue
        if not _is_approved_voa_stock_url(url):
            raise ValueError("VOA stock ZIP URL is not an approved official asset")
        candidates.add(_canonical_voa_stock_url(url))
    if not candidates:
        raise ValueError("no approved VOA NDR stock ZIP was found in collection HTML")
    if len(candidates) != 1:
        raise ValueError("VOA collection HTML contains ambiguous NDR stock ZIP links")
    return candidates.pop()


def parse_voa_current_release_page_html(evidence: bytes) -> str:
    """Select the newest dated VOA stock release page from its collection.

    GOV.UK's collection links to release landing pages rather than directly to
    the ZIP.  The page itself is retained as evidence before its attachment is
    acquired, so a later parser can audit each selection step.
    """

    direct_assets: set[str] = set()
    release_pages: dict[str, int] = {}
    for href in _hrefs_from_html(evidence):
        url = urljoin("https://www.gov.uk", html.unescape(href).strip())
        if _looks_like_voa_stock_url(url):
            if not _is_approved_voa_stock_url(url):
                raise ValueError("VOA stock ZIP URL is not an approved official asset")
            direct_assets.add(_canonical_voa_stock_url(url))
            continue
        match = _VOA_RELEASE_PAGE_PATH.fullmatch(urlparse(url).path)
        if not match or not is_current_voa_stock_release_page_url(url):
            continue
        release_pages[url] = int(match["year"])
    if direct_assets:
        if len(direct_assets) != 1:
            raise ValueError("VOA collection HTML contains ambiguous NDR stock ZIP links")
        return direct_assets.pop()
    if not release_pages:
        raise ValueError("no approved VOA NDR stock release page was found in collection HTML")
    newest_year = max(release_pages.values())
    newest = sorted(url for url, year in release_pages.items() if year == newest_year)
    if len(newest) != 1:
        raise ValueError("VOA collection HTML contains ambiguous current release pages")
    return newest[0]


def is_current_voa_stock_release_page_url(url: str) -> bool:
    """Return whether ``url`` is a dated, fixed GOV.UK VOA release page."""

    try:
        parsed = urlparse(url)
        return (
            parsed.scheme.casefold() == "https"
            and parsed.hostname == "www.gov.uk"
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and not parsed.query
            and not parsed.fragment
            and _VOA_RELEASE_PAGE_PATH.fullmatch(parsed.path) is not None
        )
    except ValueError:
        return False


def discover_voa_office_stock_url(
    collection_url: str = VOA_STOCK_COLLECTION_URL,
) -> str:
    """Discover the current VOA ZIP release rather than pinning an edition URL."""

    evidence = get_bytes(collection_url, policy=_GOVUK_COLLECTION_POLICY)
    if collection_url == VOA_STOCK_COLLECTION_URL:
        selected = parse_voa_current_release_page_html(evidence)
        if _looks_like_voa_stock_url(selected):
            return selected
        release_evidence = get_bytes(selected, policy=_GOVUK_COLLECTION_POLICY)
        return parse_voa_office_stock_collection_html(release_evidence)

    # The caller-provided URL path remains for the historical test adapter.
    page = evidence.decode("utf-8", errors="replace")
    candidates = re.findall(r'''href=["']([^"']+\.zip(?:\?[^"']*)?)["']''', page, re.I)
    matches = [
        urljoin(collection_url, html.unescape(candidate))
        for candidate in candidates
        if "ndr" in candidate.lower() or "stock" in candidate.lower()
    ]
    if not matches:
        raise ValueError("no VOA stock ZIP was found on the collection page")
    return matches[-1]


def _hrefs_from_html(evidence: bytes) -> tuple[str, ...]:
    parser = _HrefCollector()
    parser.feed(evidence.decode("utf-8", errors="replace"))
    parser.close()
    return tuple(parser.hrefs)


def _looks_like_voa_stock_url(url: str) -> bool:
    return bool(_VOA_STOCK_FILENAME.fullmatch(urlparse(url).path.rsplit("/", 1)[-1]))


def _is_approved_voa_stock_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme.casefold() == "https"
            and parsed.hostname == "assets.publishing.service.gov.uk"
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path.startswith("/media/")
            and _looks_like_voa_stock_url(url)
        )
    except ValueError:
        return False


def _canonical_voa_stock_url(url: str) -> str:
    return "https://assets.publishing.service.gov.uk" + urlparse(url).path


def _latest_common_voa_year(archive: ZipFile) -> int:
    common_years = _voa_year_columns(archive, _VOA_COUNT_MEMBER) & _voa_year_columns(
        archive, _VOA_RATEABLE_VALUE_MEMBER
    )
    if not common_years:
        raise ValueError("VOA stock tables have no common annual column")
    return max(common_years)


def _voa_year_columns(archive: ZipFile, filename: str) -> set[int]:
    try:
        with archive.open(filename) as raw:
            header = next(csv.reader(TextIOWrapper(raw, encoding="utf-8-sig")), None)
    except KeyError as error:
        raise ValueError(f"VOA archive has no {filename}") from error
    if header is None:
        raise ValueError(f"VOA archive {filename} has no header row")
    years = {
        int(column)
        for column in header
        if _VOA_YEAR_COLUMN.fullmatch(column.strip())
    }
    if not years:
        raise ValueError(f"VOA archive {filename} has no annual columns")
    return years


def _parse_voa_office_stock_archive(
    archive: ZipFile,
    *,
    area_code: str,
    year: int,
    include_locator: bool = False,
) -> list[dict[str, object]]:
    count = _read_voa_row(archive, _VOA_COUNT_MEMBER, area_code, year)
    rateable_value = _read_voa_row(
        archive, _VOA_RATEABLE_VALUE_MEMBER, area_code, year
    )
    if count is None or rateable_value is None:
        return []

    count_row, count_row_number = count
    value_row, value_row_number = rateable_value
    record: dict[str, object] = {
        "geography": count_row["geography"],
        "area_code": count_row["area_code"],
        "area_name": count_row["area_name"],
        "year": year,
        "office_property_count": int(count_row[str(year)]),
        "total_rateable_value_gbp_thousands": int(value_row[str(year)]),
    }
    if include_locator:
        record["locator"] = {
            "kind": "zip_csv_rows",
            "area_code": area_code,
            "year_column": str(year),
            "rows": {
                "office_property_count": {
                    "member": _VOA_COUNT_MEMBER,
                    "row": count_row_number,
                },
                "total_rateable_value_gbp_thousands": {
                    "member": _VOA_RATEABLE_VALUE_MEMBER,
                    "row": value_row_number,
                },
            },
        }
    return [record]


def _read_voa_row(
    archive: ZipFile,
    filename: str,
    area_code: str,
    year: int,
) -> tuple[dict[str, str], int] | None:
    """Read one VOA CSV row from an open release archive."""

    try:
        with archive.open(filename) as raw:
            rows = csv.DictReader(TextIOWrapper(raw, encoding="utf-8-sig"))
            fields = set(rows.fieldnames or ())
            required_fields = {"geography", "area_code", "area_name", str(year)}
            if required_fields - fields:
                raise ValueError(f"VOA archive {filename} has unexpected columns")
            for row_number, row in enumerate(rows, start=2):
                if row["area_code"] == area_code:
                    return row, row_number
    except KeyError as error:
        raise ValueError(f"VOA archive has no {filename}") from error
    return None
