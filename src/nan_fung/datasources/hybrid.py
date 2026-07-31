"""Free ONS hybrid-working indicators for Great Britain."""

from __future__ import annotations

from io import BytesIO

from openpyxl import load_workbook

from nan_fung.datasources.common import SourceResult, get_bytes, source_result

ONS_WORKING_ARRANGEMENTS_URL = (
    "https://www.ons.gov.uk/peoplepopulationandcommunity/wellbeing/datasets/"
    "publicopinionsandsocialtrendsgreatbritainworkingarrangements"
)
ONS_JUNE_2026_XLSX_URL = (
    "https://www.ons.gov.uk/file?uri=/peoplepopulationandcommunity/wellbeing/"
    "datasets/publicopinionsandsocialtrendsgreatbritainworkingarrangements/"
    "3to28june2026/workingarrangements3to28june2026.xlsx"
)


def fetch_hybrid_working() -> SourceResult:
    """Return the last two numeric ONS hybrid-working estimates from June 2026.

    This is a Great Britain survey proxy, not London office occupancy or
    building-access data.
    """

    workbook = load_workbook(
        BytesIO(get_bytes(ONS_JUNE_2026_XLSX_URL)),
        read_only=True,
        data_only=True,
    )
    worksheet = workbook["Table_6"]
    observations = []
    for period, estimate, lower, upper in worksheet.iter_rows(
        min_row=11,
        max_col=4,
        values_only=True,
    ):
        if not isinstance(estimate, (int, float)):
            continue
        observations.append(
            {
                "period": period,
                "geography": "Great Britain",
                "metric": (
                    "working adults who both travelled to work and worked "
                    "from home in the past seven days"
                ),
                "estimate_percent": estimate,
                "lower_confidence_limit": lower,
                "upper_confidence_limit": upper,
                "indicator_type": "proxy",
                "is_office_occupancy": False,
            }
        )

    return source_result(
        category="hybrid_working",
        source="ONS Opinions and Lifestyle Survey (OPN)",
        source_url=ONS_JUNE_2026_XLSX_URL,
        published_at="2026-07-17",
        records=observations[-2:],
    )
