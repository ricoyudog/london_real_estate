"""Free official UK macroeconomic datasources."""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from datetime import date
from email.utils import parsedate_to_datetime
from time import strptime
from typing import Any
from urllib.parse import urlencode

from nan_fung.datasources.common import SourceResult, get_bytes, get_json, source_result

BOE_BANK_RATE_URL = (
    "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
)
BOE_NEWS_RSS_URL = "https://www.bankofengland.co.uk/rss/news"
ONS_DATA_URL = "https://api.beta.ons.gov.uk/v1/data"
NOMIS_DATASET_URL = "https://www.nomisweb.co.uk/api/v01/dataset"

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
_ONS_UNIT_FALLBACKS = {
    "AP2Y": "thousand vacancies",
    "ECYX": "%",
}


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
    payload = get_bytes(BOE_BANK_RATE_URL, params=params).decode("utf-8-sig")
    records = [
        {
            "date": _date_iso(row["DATE"]),
            "bank_rate_percent": float(row["IUDBEDR"]),
            "series": "IUDBEDR",
        }
        for row in csv.DictReader(io.StringIO(payload))
        if row.get("DATE") and row.get("IUDBEDR")
    ]
    return source_result(
        category="interest-rates-monetary-policy",
        source="Bank of England IADB",
        source_url=f"{BOE_BANK_RATE_URL}?{urlencode(params)}",
        records=records,
    )


def fetch_latest_mpc_decision() -> SourceResult:
    """Fetch the latest MPC summary-and-minutes item from the BoE News RSS feed."""

    root = ET.fromstring(get_bytes(BOE_NEWS_RSS_URL))
    record: dict[str, Any] | None = None
    for item in root.findall("./channel/item"):
        link = item.findtext("link", "").strip()
        if "/monetary-policy-summary-and-minutes/" not in link:
            continue
        published = item.findtext("pubDate", "").strip()
        record = {
            "title": item.findtext("title", "").strip(),
            "url": link,
            "published_at": _rss_datetime(published),
            "summary": item.findtext("description", "").strip(),
        }
        break
    records = [record] if record else []
    return source_result(
        category="interest-rates-monetary-policy",
        source="Bank of England News RSS",
        source_url=BOE_NEWS_RSS_URL,
        published_at=record["published_at"] if record else None,
        records=records,
    )


def fetch_uk_gdp() -> SourceResult:
    """Fetch the latest ONS monthly growth and quarterly GDP growth observations."""

    return _fetch_ons_series("gdp", _ONS_GDP_SERIES)


def fetch_uk_inflation() -> SourceResult:
    """Fetch the latest ONS CPI, CPIH and RPI annual inflation rates."""

    return _fetch_ons_series("inflation", _ONS_INFLATION_SERIES)


def fetch_uk_labour_market() -> SourceResult:
    """Fetch headline UK employment, unemployment, vacancy and pay observations."""

    return _fetch_ons_series("employment-market", _ONS_LABOUR_SERIES)


def fetch_london_labour_market() -> SourceResult:
    """Fetch the latest London LFS rates and total workforce jobs from Nomis."""

    lfs_params = {
        "geography": "E12000007",
        "time": "latest",
        "sex": "7",
        "economic_activity": "3,7",
        "value_type": "0",
        "measures": "20207",
    }
    jobs_params = {
        "geography": "E12000007",
        "time": "latest",
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
        response = get_json(endpoint, params=params)
        direct_url = f"{endpoint}?{urlencode(params)}"
        source_urls.append(direct_url)
        for observation in response.get("obs", []):
            record = {
                "dataset": dataset,
                "geography": observation["geography"]["description"],
                "geography_code": observation["geography"]["geogcode"],
                "period": observation["time"]["description"],
                "period_code": observation["time"]["value"],
                "value": observation["obs_value"]["value"],
                "status": observation["obs_status"]["description"],
                "source_url": direct_url,
            }
            if dataset == "NM_59_1":
                record["metric"] = observation["economic_activity"]["description"]
                record["unit"] = "percent"
            else:
                record["metric"] = observation["item"]["description"]
                record["unit"] = "jobs"
            records.append(record)
    return source_result(
        category="employment-market",
        source="Nomis (Office for National Statistics)",
        source_url=source_urls[0],
        records=records,
    )


def _fetch_ons_series(
    category: str, series: tuple[tuple[str, str, str], ...]
) -> SourceResult:
    records: list[dict[str, Any]] = []
    release_dates: list[str] = []
    update_dates: list[str] = []
    for code, uri, frequency in series:
        response = get_json(ONS_DATA_URL, params={"uri": uri})
        description = response["description"]
        observations = response.get(frequency, [])
        if not observations:
            continue
        latest = observations[-1]
        release_date = description.get("releaseDate")
        if release_date:
            release_dates.append(release_date)
        update_date = latest.get("updateDate")
        if update_date:
            update_dates.append(update_date)
        records.append(
            {
                "series": code,
                "title": description["title"],
                "release_date": release_date,
                "frequency": frequency.removesuffix("s"),
                "period": latest["label"],
                "period_basis": description.get("monthLabelStyle")
                or description.get("quarterLabelStyle"),
                "value": float(latest["value"]),
                "unit": description.get("unit") or _ONS_UNIT_FALLBACKS.get(code, ""),
                "updated_at": latest.get("updateDate"),
                "source_url": f"{ONS_DATA_URL}?{urlencode({'uri': uri})}",
            }
        )
    return source_result(
        category=category,
        source="Office for National Statistics",
        source_url=ONS_DATA_URL,
        published_at=max(release_dates) if release_dates else None,
        source_updated_at=max(update_dates) if update_dates else None,
        records=records,
    )


def _rss_datetime(value: str) -> str | None:
    if not value:
        return None
    return parsedate_to_datetime(value).isoformat()


def _date_iso(value: str) -> str:
    parsed = strptime(value, "%d %b %Y")
    return date(parsed.tm_year, parsed.tm_mon, parsed.tm_mday).isoformat()
