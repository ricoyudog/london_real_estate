from io import BytesIO
from zipfile import ZipFile

import pytest

from nan_fung.datasources import market


class _Page:
    def __init__(self, text: str) -> None:
        self.text = text

    def extract_text(self) -> str:
        return self.text


def test_fetch_public_market_report_returns_page_text(monkeypatch) -> None:
    monkeypatch.setattr(market, "get_bytes", lambda url, **_kwargs: b"%PDF-1.7\n")
    monkeypatch.setattr(
        market,
        "PdfReader",
        lambda stream: type("Reader", (), {"pages": [_Page("rent")]})(),
    )

    result = market.fetch_public_market_report(
        url="https://example.com/report.pdf",
        published_at="2026-07-01",
        max_pages=1,
    )

    assert result["category"] == "office_market_report"
    assert result["published_at"] == "2026-07-01"
    assert result["records"] == [{"page": 1, "text": "rent"}]


def test_fetch_voa_office_stock_combines_count_and_value(monkeypatch) -> None:
    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        header = "geography,area_code,area_name,2026\n"
        archive.writestr("table_SOP5_1.csv", header + "REGL,E12000007,London,103400\n")
        archive.writestr("table_SOP5_2.csv", header + "REGL,E12000007,London,9264908\n")
    monkeypatch.setattr(market, "get_bytes", lambda url, **_kwargs: payload.getvalue())

    result = market.fetch_voa_office_stock()

    assert result["records"] == [
        {
            "geography": "REGL",
            "area_code": "E12000007",
            "area_name": "London",
            "year": 2026,
            "office_property_count": 103400,
            "total_rateable_value_gbp_thousands": 9264908,
        }
    ]


def test_parse_voa_office_stock_zip_is_a_pure_artifact_parser() -> None:
    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        archive.writestr("table_SOP5_1.csv", "geography,area_code,area_name,2026\nREGL,E1,London,2\n")
        archive.writestr("table_SOP5_2.csv", "geography,area_code,area_name,2026\nREGL,E1,London,3\n")

    assert market.parse_voa_office_stock_zip(payload.getvalue(), area_code="E1") == [
        {
            "geography": "REGL",
            "area_code": "E1",
            "area_name": "London",
            "year": 2026,
            "office_property_count": 2,
            "total_rateable_value_gbp_thousands": 3,
        }
    ]


def test_parse_current_voa_london_office_stock_zip_uses_latest_common_year() -> None:
    payload = BytesIO()
    with ZipFile(payload, "w") as archive:
        archive.writestr(
            "table_SOP5_1.csv",
            "geography,area_code,area_name,2025,2028\n"
            "REGL,E12000007,London,100,200\n",
        )
        archive.writestr(
            "table_SOP5_2.csv",
            "geography,area_code,area_name,2026,2028\n"
            "REGL,E12000007,London,300,400\n",
        )

    assert market.parse_current_voa_london_office_stock_zip(payload.getvalue()) == [
        {
            "geography": "REGL",
            "area_code": "E12000007",
            "area_name": "London",
            "year": 2028,
            "office_property_count": 200,
            "total_rateable_value_gbp_thousands": 400,
            "locator": {
                "kind": "zip_csv_rows",
                "area_code": "E12000007",
                "year_column": "2028",
                "rows": {
                    "office_property_count": {
                        "member": "table_SOP5_1.csv",
                        "row": 2,
                    },
                    "total_rateable_value_gbp_thousands": {
                        "member": "table_SOP5_2.csv",
                        "row": 2,
                    },
                },
            },
        }
    ]


def test_parse_voa_office_stock_collection_html_selects_one_official_release() -> None:
    assert market.parse_voa_office_stock_collection_html(
        b'<a href="https://assets.publishing.service.gov.uk/media/release/'
        b'ndr_stock_of_properties_2027.zip">download</a>'
    ) == (
        "https://assets.publishing.service.gov.uk/media/release/"
        "ndr_stock_of_properties_2027.zip"
    )


def test_parse_voa_office_stock_collection_html_refuses_unsafe_or_nonmatching_links() -> None:
    for href in (
        "https://example.test/media/release/ndr_stock_of_properties_2027.zip",
        "/media/release/ndr_stock_of_properties_2027.zip",
        "https://assets.publishing.service.gov.uk/media/release/other.zip",
    ):
        with pytest.raises(ValueError):
            market.parse_voa_office_stock_collection_html(
                f'<a href="{href}">download</a>'.encode()
            )


def test_parse_voa_office_stock_collection_html_refuses_ambiguous_releases() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        market.parse_voa_office_stock_collection_html(
            b'<a href="https://assets.publishing.service.gov.uk/media/release/'
            b'ndr_stock_of_properties_2026.zip">old</a>'
            b'<a href="https://assets.publishing.service.gov.uk/media/release/'
            b'ndr_stock_of_properties_2027.zip">new</a>'
        )


def test_parse_voa_current_release_page_html_selects_the_newest_release_page() -> None:
    assert market.parse_voa_current_release_page_html(
        b'<a href="/government/statistics/non-domestic-rating-stock-of-properties-2025">'
        b"old</a>"
        b'<a href="/government/statistics/non-domestic-rating-stock-of-properties-march-2026">'
        b"current</a>"
    ) == (
        "https://www.gov.uk/government/statistics/"
        "non-domestic-rating-stock-of-properties-march-2026"
    )


def test_discover_voa_office_stock_url_uses_collection_page(monkeypatch) -> None:
    monkeypatch.setattr(
        market,
        "get_bytes",
        lambda _url, **_kwargs: b'<a href="/files/ndr_stock_of_properties_2027.zip">release</a>',
    )

    assert market.discover_voa_office_stock_url("https://example.test/collection") == (
        "https://example.test/files/ndr_stock_of_properties_2027.zip"
    )


@pytest.mark.network
@pytest.mark.restricted_live_probe
def test_live_bnp_report() -> None:
    result = market.fetch_public_market_report(max_pages=3)

    assert len(result["records"]) == 3
    text = " ".join(record["text"] for record in result["records"])
    assert "Q1 2026" in text
    assert "£175" in text
    assert "£92.50" in text
    assert "Ink Building" in text


@pytest.mark.network
@pytest.mark.legacy_live_probe
def test_live_voa_office_stock() -> None:
    result = market.fetch_voa_office_stock()

    assert result["records"][0]["area_name"] == "London"
    assert result["records"][0]["office_property_count"] == 103_400
