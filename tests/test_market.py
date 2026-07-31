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
    monkeypatch.setattr(market, "get_bytes", lambda url: b"pdf")
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
    monkeypatch.setattr(market, "get_bytes", lambda url: payload.getvalue())

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


@pytest.mark.live
def test_live_bnp_report() -> None:
    result = market.fetch_public_market_report(max_pages=3)

    assert len(result["records"]) == 3
    text = " ".join(record["text"] for record in result["records"])
    assert "Q1 2026" in text
    assert "£175" in text
    assert "£92.50" in text
    assert "Ink Building" in text


@pytest.mark.live
def test_live_voa_office_stock() -> None:
    result = market.fetch_voa_office_stock()

    assert result["records"][0]["area_name"] == "London"
    assert result["records"][0]["office_property_count"] == 103_400
