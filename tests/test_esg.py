import json
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


def _epc_workbook(*, valid_schema: bool = True) -> bytes:
    document = OpenDocumentSpreadsheet()
    table = Table(name="A_by_Region")
    _add_row(
        table,
        [
            "A- Non-Domestic Properties by Region by Energy Performance Asset "
            "Rating - in each Year/Quarter to 30 June 2026"
        ],
    )
    _add_row(table, ["This worksheet contains one table."])
    _add_row(
        table,
        [
            "Source: Energy Performance Certificates for Buildings Register for "
            "England and Wales"
        ],
    )
    _add_row(
        table,
        (
            ["unexpected"]
            if not valid_schema
            else [
                "Region",
                "Quarter",
                "Number Lodgements",
                "Total Floor Area (m2)",
                "A+",
                "A",
                "B",
                "C",
                "D",
                "E",
                "F",
                "G",
                "Not Recorded",
            ]
        ),
    )
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
    def fake_get_bytes(url: str, **_kwargs: object) -> bytes:
        if url == esg.EPB_CONTENT_API_URL:
            return json.dumps(
                {
                    "public_updated_at": "2026-07-30T09:30:20+01:00",
                    "details": {
                        "attachments": [
                            {
                                "title": esg._NON_DOMESTIC_TITLE,
                                "url": "https://assets.example/non-domestic.ods",
                            }
                        ]
                    },
                }
            ).encode()
        return _epc_workbook()

    monkeypatch.setattr(esg, "get_bytes", fake_get_bytes)

    result = esg.fetch_non_domestic_epc_ratings()

    record = result["records"][0]
    assert result["published_at"] is None
    assert result["source_updated_at"] == "2026-07-30T09:30:20+01:00"
    assert record["region"] == "London"
    assert record["quarter"] == "2026/2"
    assert record["number_lodgements"] == 3630
    assert record["rating_b"] == 1621
    assert record["indicator_type"] == "proxy"
    assert record["scope"] == "all non-domestic properties, not offices only"


def test_parse_non_domestic_epc_ratings_ods_is_a_pure_artifact_parser() -> None:
    records = esg.parse_non_domestic_epc_ratings_ods(_epc_workbook())

    assert [record["quarter"] for record in records] == ["2026/1", "2026/2"]
    assert all(record["indicator_type"] == "proxy" for record in records)
    assert all(
        record["scope"] == "all non-domestic properties, not offices only"
        for record in records
    )


def test_parse_non_domestic_epc_ratings_ods_rejects_an_unrecognized_table_schema() -> None:
    with pytest.raises(ValueError, match="schema is not recognized"):
        esg.parse_non_domestic_epc_ratings_ods(_epc_workbook(valid_schema=False))


def test_parse_non_domestic_epc_attachment_json_is_a_pure_artifact_parser() -> None:
    attachment = esg.parse_non_domestic_epc_attachment_json(
        json.dumps(
            {
                "public_updated_at": "2026-07-30T09:30:20+01:00",
                "details": {
                    "attachments": [
                        {
                            "title": esg._NON_DOMESTIC_TITLE,
                            "url": "https://assets.example/non-domestic.ods",
                        }
                    ]
                },
            }
        ).encode()
    )

    assert attachment == {
        "attachment_url": "https://assets.example/non-domestic.ods",
        "attachment_title": esg._NON_DOMESTIC_TITLE,
        "source_updated_at": "2026-07-30T09:30:20+01:00",
    }


def test_parse_non_domestic_epc_attachment_json_requires_exact_title_when_requested() -> None:
    evidence = json.dumps(
        {
            "details": {
                "attachments": [
                    {
                        "title": "Non-domestic Energy Performance Certificates",
                        "url": "https://assets.example/non-domestic.ods",
                    }
                ]
            }
        }
    ).encode()

    assert esg.parse_non_domestic_epc_attachment_json(evidence)[
        "attachment_title"
    ] == "Non-domestic Energy Performance Certificates"
    with pytest.raises(ValueError, match="exact Table A"):
        esg.parse_non_domestic_epc_attachment_json(
            evidence,
            require_exact_title=True,
        )
    with pytest.raises(ValueError, match="exact Table A"):
        esg.parse_exact_non_domestic_epc_attachment_json(evidence)


def test_parse_non_domestic_epc_attachment_json_requires_one_exact_ods() -> None:
    exact_title = esg._NON_DOMESTIC_TITLE
    with pytest.raises(ValueError, match="exact Table A"):
        esg.parse_non_domestic_epc_attachment_json(
            json.dumps(
                {
                    "details": {
                        "attachments": [
                            {
                                "title": exact_title,
                                "url": "https://assets.example/non-domestic.csv",
                            }
                        ]
                    }
                }
            ).encode(),
            require_exact_title=True,
        )
    with pytest.raises(ValueError, match="ambiguous"):
        esg.parse_non_domestic_epc_attachment_json(
            json.dumps(
                {
                    "details": {
                        "attachments": [
                            {
                                "title": exact_title,
                                "url": "https://assets.example/non-domestic-a.ods",
                            },
                            {
                                "title": exact_title,
                                "url": "https://assets.example/non-domestic-b.ods",
                            },
                        ]
                    }
                }
            ).encode(),
            require_exact_title=True,
        )


@pytest.mark.network
@pytest.mark.legacy_live_probe
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
