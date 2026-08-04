"""Pure normalizers for persisted ONS, Nomis, and BoE MPC artifacts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import json
from typing import Any
from urllib.parse import urlparse
import xml.etree.ElementTree as ET


class OfficialMacroParseError(ValueError):
    """A persisted official-macro artifact does not match its source contract."""


_ONS_DATASOURCE_IDS = {
    "ECYX": "ons.gdp.ecyx",
    "IHYQ": "ons.gdp.ihyq",
    "D7G7": "ons.inflation.d7g7",
    "L55O": "ons.inflation.l55o",
    "CZBH": "ons.inflation.czbh",
    "LF24": "ons.labour.lf24",
    "MGSX": "ons.labour.mgsx",
    "AP2Y": "ons.labour.ap2y",
    "KAI9": "ons.labour.kai9",
}
_ONS_UNIT_FALLBACKS = {
    "AP2Y": "thousand vacancies",
    "ECYX": "%",
}
_NOMIS_CONFIG = {
    "NM_59_1": {
        "datasource_id": "nomis.nm_59_1.london_lfs",
        "metric_dimension": "economic_activity",
        "unit": "percent",
    },
    "NM_130_1": {
        "datasource_id": "nomis.nm_130_1.london_workforce_jobs",
        "metric_dimension": "item",
        "unit": "jobs",
    },
}
MPC_RSS_DATASOURCE_ID = "boe.mpc_news"
_MPC_PATH_PREFIX = "/monetary-policy-summary-and-minutes/"


def ons_datasource_id(series: str) -> str:
    """Return the fixed datasource identity for an approved ONS series."""

    code = _required_text(series, "ONS series").upper()
    try:
        return _ONS_DATASOURCE_IDS[code]
    except KeyError as error:
        raise OfficialMacroParseError(f"unsupported ONS series: {series!r}") from error


def parse_ons_series_json(
    evidence: bytes,
    *,
    series: str,
    uri: str,
    frequency: str,
    source_url: str,
) -> tuple[dict[str, Any], ...]:
    """Normalize one persisted ONS current-vintage response without I/O."""

    code = _required_text(series, "ONS series").upper()
    datasource_id = ons_datasource_id(code)
    normalized_uri = _required_text(uri, "ONS URI")
    normalized_source_url = _required_text(source_url, "ONS source URL")
    if frequency not in {"months", "quarters"}:
        raise OfficialMacroParseError("ONS frequency must be months or quarters")
    payload = _json_object(evidence, "ONS")
    description = _object(payload.get("description"), "ONS description")
    title = _required_text(description.get("title"), "ONS description title")
    release_date = _optional_text(description.get("releaseDate"), "ONS release date")
    unit = _optional_text(description.get("unit"), "ONS unit") or _ONS_UNIT_FALLBACKS.get(
        code, ""
    )
    period_basis = _optional_text(
        description.get("monthLabelStyle") or description.get("quarterLabelStyle"),
        "ONS period basis",
    )
    observations = payload.get(frequency, [])
    if not isinstance(observations, list):
        raise OfficialMacroParseError(f"ONS {frequency} must be a list")

    records: list[dict[str, Any]] = []
    for index, observation in enumerate(observations):
        item = _object(observation, f"ONS {frequency} observation")
        source_value = _source_number_text(item.get("value"), "ONS observation value")
        records.append(
            {
                "datasource_id": datasource_id,
                "series": code,
                "title": title,
                "release_date": release_date,
                "frequency": frequency.removesuffix("s"),
                "period": _required_text(item.get("label"), "ONS observation label"),
                "period_basis": period_basis,
                "value": _decimal_text(source_value, "ONS observation value"),
                "source_value": source_value,
                "unit": unit,
                "updated_at": _optional_text(
                    item.get("updateDate"), "ONS observation update date"
                ),
                "source_url": normalized_source_url,
                "locator": {
                    "kind": "json_pointer",
                    "pointer": f"/{frequency}/{index}",
                    "uri": normalized_uri,
                },
            }
        )
    return tuple(records)


def ons_record_key(record: Mapping[str, Any]) -> tuple[str, str]:
    """Build the ONS natural key: series plus source period label."""

    return (
        _required_text(record.get("series"), "ONS record series").upper(),
        _required_text(record.get("period"), "ONS record period"),
    )


def ons_record_metadata(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return operational metadata and locator for one normalized ONS record."""

    return {
        "datasource_id": _required_text(
            record.get("datasource_id"), "ONS datasource ID"
        ),
        "record_type": "metric",
        "category": "macro",
        "source_date": _ons_source_date(record),
        "period_label": _required_text(record.get("period"), "ONS record period"),
        "unit": _text_or_empty(record.get("unit"), "ONS record unit"),
        "data_kind": "direct",
        "confidence": "high",
        "definition": _required_text(record.get("title"), "ONS record title"),
        "limitations": ["Current-vintage ONS response"],
        "locator": _locator(record, "ONS"),
    }


def ons_artifact_metadata(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Derive source timestamps for a single parsed ONS artifact."""

    materialized = tuple(records)
    return {
        "published_at": _latest_text(record.get("release_date") for record in materialized),
        "source_updated_at": _latest_text(record.get("updated_at") for record in materialized),
        "record_count": len(materialized),
    }


def parse_nomis_dataset_json(
    evidence: bytes,
    *,
    dataset: str,
    source_url: str,
) -> tuple[dict[str, Any], ...]:
    """Normalize one persisted configured Nomis dataset response without I/O."""

    dataset_id = _required_text(dataset, "Nomis dataset").upper()
    try:
        config = _NOMIS_CONFIG[dataset_id]
    except KeyError as error:
        raise OfficialMacroParseError(f"unsupported Nomis dataset: {dataset!r}") from error
    payload = _json_object(evidence, "Nomis")
    observations = payload.get("obs", [])
    if not isinstance(observations, list):
        raise OfficialMacroParseError("Nomis obs must be a list")
    normalized_source_url = _required_text(source_url, "Nomis source URL")
    metric_dimension = config["metric_dimension"]

    records: list[dict[str, Any]] = []
    for index, observation in enumerate(observations):
        item = _object(observation, "Nomis observation")
        geography = _object(item.get("geography"), "Nomis geography")
        period = _object(item.get("time"), "Nomis time")
        metric = _object(item.get(metric_dimension), f"Nomis {metric_dimension}")
        value = _object(item.get("obs_value"), "Nomis observation value")
        status = _object(item.get("obs_status"), "Nomis observation status")
        geography_code = _required_text(geography.get("geogcode"), "Nomis geography code")
        period_code = _required_text(period.get("value"), "Nomis period code")
        metric_name = _required_text(metric.get("description"), "Nomis metric")
        source_value = _source_number_text(value.get("value"), "Nomis observation value")
        records.append(
            {
                "datasource_id": config["datasource_id"],
                "dataset": dataset_id,
                "geography": _required_text(
                    geography.get("description"), "Nomis geography"
                ),
                "geography_code": geography_code,
                "period": _required_text(period.get("description"), "Nomis period"),
                "period_code": period_code,
                "metric": metric_name,
                "value": _decimal_text(source_value, "Nomis observation value"),
                "source_value": source_value,
                "status": _required_text(status.get("description"), "Nomis status"),
                "unit": config["unit"],
                "source_url": normalized_source_url,
                "locator": {
                    "kind": "json_pointer",
                    "pointer": f"/obs/{index}",
                    "dataset": dataset_id,
                    "dimensions": {
                        "geography_code": geography_code,
                        "period_code": period_code,
                        "metric": metric_name,
                    },
                },
            }
        )
    return tuple(records)


def nomis_record_key(record: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Build the configured Nomis natural key including its metric dimension."""

    return (
        _required_text(record.get("dataset"), "Nomis record dataset").upper(),
        _required_text(record.get("geography_code"), "Nomis geography code"),
        _required_text(record.get("period_code"), "Nomis period code"),
        _required_text(record.get("metric"), "Nomis metric"),
    )


def nomis_record_metadata(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return operational metadata and locator for one normalized Nomis record."""

    return {
        "datasource_id": _required_text(
            record.get("datasource_id"), "Nomis datasource ID"
        ),
        "record_type": "metric",
        "category": "employment-market",
        "source_date": _nomis_source_date(record),
        "period_label": _required_text(record.get("period"), "Nomis record period"),
        "unit": _required_text(record.get("unit"), "Nomis record unit"),
        "data_kind": "direct",
        "confidence": "high",
        "definition": _required_text(record.get("metric"), "Nomis metric"),
        "limitations": ["Configured Nomis dimensions only"],
        "locator": _locator(record, "Nomis"),
    }


def nomis_artifact_metadata(
    records: Iterable[Mapping[str, Any]],
    *,
    dataset: str | None = None,
) -> dict[str, Any]:
    """Return completeness metadata for one configured Nomis response."""

    materialized = tuple(records)
    selected_dataset = dataset
    if selected_dataset is None:
        if not materialized:
            raise OfficialMacroParseError("empty Nomis records require a dataset")
        selected_dataset = _required_text(
            materialized[0].get("dataset"), "Nomis record dataset"
        )
    return {
        "dataset": _required_text(selected_dataset, "Nomis dataset").upper(),
        "record_count": len(materialized),
        "period_codes": sorted(
            {
                _required_text(record.get("period_code"), "Nomis period code")
                for record in materialized
            }
        ),
    }


def parse_mpc_rss_xml(
    evidence: bytes,
    *,
    source_url: str,
) -> tuple[dict[str, Any], ...]:
    """Parse captured BoE RSS bytes into canonical MPC release metadata."""

    try:
        root = ET.fromstring(evidence)
    except ET.ParseError as error:
        raise OfficialMacroParseError("MPC RSS is not valid XML") from error
    normalized_source_url = _required_text(source_url, "MPC RSS source URL")
    records: list[dict[str, Any]] = []
    for index, item in enumerate(element for element in root.iter() if _local_name(element.tag) == "item"):
        link = _element_text(item, "link")
        canonical_path = _mpc_canonical_path(link)
        if canonical_path is None:
            continue
        published_at = _rss_timestamp(_element_text(item, "pubDate"))
        records.append(
            {
                "datasource_id": MPC_RSS_DATASOURCE_ID,
                "title": _required_text(_element_text(item, "title"), "MPC title"),
                "url": _required_text(link, "MPC link"),
                "canonical_path": canonical_path,
                "guid": _optional_text(_element_text(item, "guid"), "MPC GUID"),
                "published_at": published_at,
                "summary": _element_text(item, "description"),
                "source_url": normalized_source_url,
                "locator": {
                    "kind": "rss_item",
                    "item_index": index,
                    "guid": _optional_text(_element_text(item, "guid"), "MPC GUID"),
                    "canonical_path": canonical_path,
                },
            }
        )
    return tuple(records)


def mpc_rss_record_key(record: Mapping[str, Any]) -> tuple[str]:
    """Build the MPC metadata natural key from its canonical article path."""

    return (_required_text(record.get("canonical_path"), "MPC canonical path"),)


def mpc_rss_record_metadata(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return operational metadata and locator for one MPC RSS item."""

    published_at = _required_text(record.get("published_at"), "MPC published at")
    return {
        "datasource_id": MPC_RSS_DATASOURCE_ID,
        "record_type": "event",
        "category": "interest-rates-monetary-policy",
        "source_date": published_at.split("T", maxsplit=1)[0],
        "period_label": None,
        "unit": None,
        "data_kind": "direct",
        "confidence": "high",
        "definition": "Bank of England MPC release metadata from RSS",
        "limitations": ["RSS summary does not support vote or rationale claims"],
        "locator": _locator(record, "MPC RSS"),
    }


def mpc_rss_artifact_metadata(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Derive the latest release timestamp from one parsed RSS artifact."""

    materialized = tuple(records)
    latest = _latest_text(record.get("published_at") for record in materialized)
    return {
        "published_at": latest,
        "source_updated_at": latest,
        "record_count": len(materialized),
    }


def _json_object(evidence: bytes, source: str) -> dict[str, Any]:
    if not isinstance(evidence, bytes):
        raise OfficialMacroParseError(f"{source} evidence must be bytes")
    try:
        value = json.loads(evidence.decode("utf-8"), parse_float=str, parse_int=str)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OfficialMacroParseError(f"{source} evidence must be UTF-8 JSON") from error
    return _object(value, f"{source} response")


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OfficialMacroParseError(f"{field} must be an object")
    return value


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfficialMacroParseError(f"{field} is required")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _required_text(value, field)


def _text_or_empty(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise OfficialMacroParseError(f"{field} must be text")
    return value.strip()


def _ons_source_date(record: Mapping[str, Any]) -> str:
    return _optional_text(record.get("release_date"), "ONS release date") or _optional_text(
        record.get("updated_at"), "ONS update date"
    ) or ""


def _nomis_source_date(record: Mapping[str, Any]) -> str:
    return _optional_text(record.get("updated_at"), "Nomis update date") or ""


def _source_number_text(value: object, field: str) -> str:
    if isinstance(value, bool) or value is None:
        raise OfficialMacroParseError(f"{field} must be numeric")
    if isinstance(value, (str, int, float, Decimal)):
        text = str(value).strip()
        if text:
            return text
    raise OfficialMacroParseError(f"{field} must be numeric")


def _decimal_text(value: str, field: str) -> str:
    try:
        decimal = Decimal(value)
    except InvalidOperation as error:
        raise OfficialMacroParseError(f"{field} must be numeric") from error
    if not decimal.is_finite():
        raise OfficialMacroParseError(f"{field} must be finite")
    if decimal.is_zero():
        return "0"
    normalized = format(decimal.normalize(), "f")
    if "e" in normalized.lower() or normalized == "-0":
        raise OfficialMacroParseError(f"{field} is not canonical")
    return normalized


def _latest_text(values: Iterable[object]) -> str | None:
    text_values = [value for value in values if isinstance(value, str) and value]
    return max(text_values) if text_values else None


def _locator(record: Mapping[str, Any], source: str) -> dict[str, Any]:
    locator = record.get("locator")
    if not isinstance(locator, Mapping):
        raise OfficialMacroParseError(f"{source} record has no locator")
    return dict(locator)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]


def _element_text(item: ET.Element, name: str) -> str:
    for child in item:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _mpc_canonical_path(link: str) -> str | None:
    if not link:
        return None
    parsed = urlparse(link)
    path = parsed.path if parsed.scheme or parsed.netloc else link
    normalized = path.rstrip("/")
    if not normalized.startswith(_MPC_PATH_PREFIX) or normalized == _MPC_PATH_PREFIX.rstrip("/"):
        return None
    return normalized


def _rss_timestamp(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(_required_text(value, "MPC pubDate"))
    except (TypeError, ValueError, IndexError) as error:
        raise OfficialMacroParseError("MPC pubDate is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()
