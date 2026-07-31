from io import BytesIO

import pytest
from odf.opendocument import OpenDocumentSpreadsheet
from odf.table import Table, TableCell, TableRow
from odf.text import P

from nan_fung.datasources import esg


def _add_row(table: Table, values: list[object]) -> None:
    row = TableRow()
    for value in values:
        if isinstance(value, (int, float)):
            cell = TableCell(valuetype="float", value=value)
        else:
            cell = TableCell(valuetype="string")
            cell.addElement(P(text=str(value)))
        row.addElement(cell)
    table.addElement(row)


def _epc_workbook() -> bytes:
    document = OpenDocumentSpreadsheet()
    table = Table(name="A_by_Region")
    for value in ("title", "description", "source", "headers"):
        _add_row(table, [value])
    _add_row(
        table,
        ["London", "2026/1", 3400, 3_100_000, 10, 400, 1500, 1000, 350, 110, 20, 10, 0],
    )
    _add_row(
        table,
        ["London", "2026/2", 3630, 3_102_511, 13, 482, 1621, 1019, 358, 119, 13, 5, 0],
    )
    document.spreadsheet.addElement(table)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def test_fetch_non_domestic_epc_ratings_discovers_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        esg,
        "get_json",
        lambda _url: {
            "public_updated_at": "2026-07-30T09:30:20+01:00",
            "details": {
                "attachments": [
                    {
                        "title": esg._NON_DOMESTIC_TITLE,
                        "url": "https://assets.example/non-domestic.ods",
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(esg, "get_bytes", lambda _url: _epc_workbook())

    result = esg.fetch_non_domestic_epc_ratings()

    record = result["records"][0]
    assert result["published_at"] is None
    assert result["source_updated_at"] == "2026-07-30T09:30:20+01:00"
    assert record["region"] == "London"
    assert record["quarter"] == "2026/2"
    assert record["number_lodgements"] == 3630
    assert record["rating_b"] == 1621
    assert record["indicator_type"] == "proxy"


@pytest.mark.live
def test_fetch_non_domestic_epc_ratings_live() -> None:
    result = esg.fetch_non_domestic_epc_ratings()

    record = result["records"][0]
    ratings = sum(
        record[f"rating_{rating}"]
        for rating in ("a_plus", "a", "b", "c", "d", "e", "f", "g")
    )
    assert record["region"] == "London"
    assert record["quarter"] == "2026/2"
    assert record["number_lodgements"] == ratings + record["not_recorded"]
    assert record["number_lodgements"] > 0
    assert record["scope"] == "all non-domestic properties, not offices only"
