from io import BytesIO

import pytest
from openpyxl import Workbook

from nan_fung.datasources import hybrid


def _hybrid_workbook() -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Table_6"
    worksheet.cell(row=11, column=1, value="6 to 31 May 2026")
    worksheet.cell(row=11, column=2, value=28)
    worksheet.cell(row=11, column=3, value=26)
    worksheet.cell(row=11, column=4, value=31)
    worksheet.cell(row=12, column=1, value="3 to 28 June 2026")
    worksheet.cell(row=12, column=2, value=25)
    worksheet.cell(row=12, column=3, value=22)
    worksheet.cell(row=12, column=4, value=28)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_fetch_hybrid_working_returns_latest_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hybrid, "get_bytes", lambda _url: _hybrid_workbook())

    result = hybrid.fetch_hybrid_working()

    assert result["category"] == "hybrid_working"
    assert result["published_at"] == "2026-07-17"
    assert [row["period"] for row in result["records"]] == [
        "6 to 31 May 2026",
        "3 to 28 June 2026",
    ]
    assert result["records"][-1]["estimate_percent"] == 25
    assert result["records"][-1]["indicator_type"] == "proxy"
    assert result["records"][-1]["is_office_occupancy"] is False


@pytest.mark.live
def test_fetch_hybrid_working_live() -> None:
    result = hybrid.fetch_hybrid_working()

    assert len(result["records"]) == 2
    record = result["records"][-1]
    assert record["period"] == "3 to 28 June 2026"
    assert record["estimate_percent"] == 25
    assert record["lower_confidence_limit"] <= record["estimate_percent"]
    assert record["estimate_percent"] <= record["upper_confidence_limit"]
    assert record["is_office_occupancy"] is False
