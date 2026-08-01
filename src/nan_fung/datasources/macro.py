"""Free official UK macroeconomic datasources."""

from __future__ import annotations

import csv
import io
from datetime import date
from time import strptime
from typing import Any
from urllib.parse import urlencode

from nan_fung.datasources.common import SourceResult, get_bytes, source_result
from nan_fung.ingestion.official_macro import (
    mpc_rss_artifact_metadata,
    parse_mpc_rss_xml,
    parse_nomis_dataset_json,
    parse_ons_series_json,
    ons_artifact_metadata,
)
from nan_fung.ingestion.policies import SourcePolicy

BOE_BANK_RATE_URL = (
    "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
)
BOE_NEWS_RSS_URL = "https://www.bankofengland.co.uk/rss/news"
ONS_DATA_URL = "https://api.beta.ons.gov.uk/v1/data"
NOMIS_DATASET_URL = "https://www.nomisweb.co.uk/api/v01/dataset"

_BOE_IADB_POLICY = SourcePolicy(
    ("www.bankofengland.co.uk",),
    allowed_query_keys=(
        "csv.x", "Datefrom", "Dateto", "SeriesCodes", "CSVF", "UsingCodes", "VPD", "VFD"
    ),
)
_BOE_RSS_POLICY = SourcePolicy(("www.bankofengland.co.uk",))
_ONS_POLICY = SourcePolicy(("api.beta.ons.gov.uk",), allowed_query_keys=("uri",))
_NOMIS_POLICY = SourcePolicy(
    ("www.nomisweb.co.uk",),
    allowed_query_keys=(
        "geography", "time", "sex", "economic_activity", "value_type", "measures", "industry", "item"
    ),
)

_ONS_GDP_SERIES = (
    (
        "ECYX",
        "/economy/grossdomesticproductgdp/timeseries/ecyx/mgdp",
        "months",
    ),
    (
        "IHYQ",
        "/economy/grossdomesticproductgdp/timeseries/ihyq/qna",
        "quarters",
    ),
)
_ONS_INFLATION_SERIES = (
    (
        "D7G7",
        "/economy/inflationandpriceindices/timeseries/d7g7/mm23",
        "months",
    ),
    (
        "L55O",
        "/economy/inflationandpriceindices/timeseries/l55o/mm23",
        "months",
    ),
    (
        "CZBH",
        "/economy/inflationandpriceindices/timeseries/czbh/mm23",
        "months",
    ),
)
_ONS_LABOUR_SERIES = (
    (
        "LF24",
        (
            "/employmentandlabourmarket/peopleinwork/employmentandemployeetypes/"
            "timeseries/lf24/lms"
        ),
        "months",
    ),
    (
        "MGSX",
        "/employmentandlabourmarket/peoplenotinwork/unemployment/timeseries/mgsx/lms",
        "months",
    ),
    (
        "AP2Y",
        (
            "/employmentandlabourmarket/peopleinwork/employmentandemployeetypes/"
            "timeseries/ap2y/lms"
        ),
        "months",
    ),
    (
        "KAI9",
        (
            "/employmentandlabourmarket/peopleinwork/earningsandworkinghours/"
            "timeseries/kai9/lms"
        ),
        "months",
    ),
)
def fetch_bank_rate(
    date_from: str = "01/Jan/2025", date_to: str = "now"
) -> SourceResult:
    """Fetch the Bank of England official Bank Rate series (IUDBEDR)."""

    params = {
        "csv.x": "yes",
        "Datefrom": date_from,
        "Dateto": date_to,
        "SeriesCodes": "IUDBEDR",
        "CSVF": "TN",
        "UsingCodes": "Y",
        "VPD": "Y",
        "VFD": "N",
    }
    records = parse_bank_rate_csv(
        get_bytes(BOE_BANK_RATE_URL, params=params, policy=_BOE_IADB_POLICY)
    )
    return source_result(
        category="interest-rates-monetary-policy",
        source="Bank of England IADB",
        source_url=f"{BOE_BANK_RATE_URL}?{urlencode(params)}",
        records=records,
    )


def parse_bank_rate_csv(payload: bytes) -> list[dict[str, Any]]:
    """Parse persisted BoE IUDBEDR CSV evidence without performing I/O."""

    return [
        {
            "date": _date_iso(row["DATE"]),
            "bank_rate_percent": float(row["IUDBEDR"]),
            "series": "IUDBEDR",
        }
        for row in csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
        if row.get("DATE") and row.get("IUDBEDR")
    ]


def fetch_latest_mpc_decision() -> SourceResult:
    """Fetch the latest MPC summary-and-minutes item from the BoE News RSS feed."""

    parsed = parse_mpc_rss_xml(
        get_bytes(BOE_NEWS_RSS_URL, policy=_BOE_RSS_POLICY),
        source_url=BOE_NEWS_RSS_URL,
    )
    records = [_legacy_mpc_record(parsed[0])] if parsed else []
    metadata = mpc_rss_artifact_metadata(parsed)
    return source_result(
        category="interest-rates-monetary-policy",
        source="Bank of England News RSS",
        source_url=BOE_NEWS_RSS_URL,
        published_at=metadata["published_at"],
        source_updated_at=metadata["source_updated_at"],
        records=records,
    )


def fetch_uk_gdp(*, include_history: bool = False) -> SourceResult:
    """Fetch the latest ONS monthly growth and quarterly GDP growth observations."""

    return _fetch_ons_series("gdp", _ONS_GDP_SERIES, include_history=include_history)


def fetch_uk_inflation(*, include_history: bool = False) -> SourceResult:
    """Fetch the latest ONS CPI, CPIH and RPI annual inflation rates."""

    return _fetch_ons_series(
        "inflation", _ONS_INFLATION_SERIES, include_history=include_history
    )


def fetch_uk_labour_market(*, include_history: bool = False) -> SourceResult:
    """Fetch headline UK employment, unemployment, vacancy and pay observations."""

    return _fetch_ons_series(
        "employment-market", _ONS_LABOUR_SERIES, include_history=include_history
    )


def fetch_london_labour_market(*, time: str = "latest") -> SourceResult:
    """Fetch the latest London LFS rates and total workforce jobs from Nomis."""

    lfs_params = {
        "geography": "E12000007",
        "time": time,
        "sex": "7",
        "economic_activity": "3,7",
        "value_type": "0",
        "measures": "20207",
    }
    jobs_params = {
        "geography": "E12000007",
        "time": time,
        "industry": "37748736",
        "item": "1",
        "measures": "20100",
    }
    requests = (
        ("NM_59_1", lfs_params),
        ("NM_130_1", jobs_params),
    )
    records: list[dict[str, Any]] = []
    source_urls: list[str] = []
    for dataset, params in requests:
        endpoint = f"{NOMIS_DATASET_URL}/{dataset}.data.json"
        direct_url = f"{endpoint}?{urlencode(params)}"
        source_urls.append(direct_url)
        parsed = parse_nomis_dataset_json(
            get_bytes(endpoint, params=params, policy=_NOMIS_POLICY),
            dataset=dataset,
            source_url=direct_url,
        )
        records.extend(_legacy_nomis_record(record) for record in parsed)
    return source_result(
        category="employment-market",
        source="Nomis (Office for National Statistics)",
        source_url=source_urls[0],
        records=records,
    )


def _fetch_ons_series(
    category: str,
    series: tuple[tuple[str, str, str], ...],
    *,
    include_history: bool = False,
) -> SourceResult:
    records: list[dict[str, Any]] = []
    release_dates: list[str] = []
    update_dates: list[str] = []
    for code, uri, frequency in series:
        source_url = f"{ONS_DATA_URL}?{urlencode({'uri': uri})}"
        parsed = parse_ons_series_json(
            get_bytes(ONS_DATA_URL, params={"uri": uri}, policy=_ONS_POLICY),
            series=code,
            uri=uri,
            frequency=frequency,
            source_url=source_url,
        )
        if not parsed:
            continue
        selected = parsed if include_history else parsed[-1:]
        metadata = ons_artifact_metadata(selected)
        if metadata["published_at"]:
            release_dates.append(metadata["published_at"])
        if metadata["source_updated_at"]:
            update_dates.append(metadata["source_updated_at"])
        records.extend(_legacy_ons_record(record) for record in selected)
    return source_result(
        category=category,
        source="Office for National Statistics",
        source_url=ONS_DATA_URL,
        published_at=max(release_dates) if release_dates else None,
        source_updated_at=max(update_dates) if update_dates else None,
        records=records,
    )


def _legacy_ons_record(record: dict[str, Any]) -> dict[str, Any]:
    """Adapt canonical-safe ONS parser output to the legacy float contract."""

    return {
        "series": record["series"],
        "title": record["title"],
        "release_date": record["release_date"],
        "frequency": record["frequency"],
        "period": record["period"],
        "period_basis": record["period_basis"],
        "value": float(record["value"]),
        "source_value": record["source_value"],
        "unit": record["unit"],
        "updated_at": record["updated_at"],
        "source_url": record["source_url"],
    }


def _legacy_nomis_record(record: dict[str, Any]) -> dict[str, Any]:
    """Adapt canonical-safe Nomis parser output to the legacy numeric contract."""

    value = float(record["value"])
    return {
        "dataset": record["dataset"],
        "geography": record["geography"],
        "geography_code": record["geography_code"],
        "period": record["period"],
        "period_code": record["period_code"],
        "value": int(value) if value.is_integer() else value,
        "status": record["status"],
        "source_url": record["source_url"],
        "metric": record["metric"],
        "unit": record["unit"],
    }


def _legacy_mpc_record(record: dict[str, Any]) -> dict[str, Any]:
    """Adapt parsed MPC RSS metadata to the established fetcher record."""

    return {
        "title": record["title"],
        "url": record["url"],
        "published_at": record["published_at"],
        "summary": record["summary"],
    }


def _date_iso(value: str) -> str:
    parsed = strptime(value, "%d %b %Y")
    return date(parsed.tm_year, parsed.tm_mon, parsed.tm_mday).isoformat()
