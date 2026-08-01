"""Canonical identities and JSON used by the ingestion control plane.

The helpers in this module deliberately accept a smaller domain than normal
``json.dumps``.  A value that participates in an identity hash must not depend
on Python float formatting or an unnormalised Unicode representation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo


REDACTED_VALUE = "<redacted>"
_TIMESTAMP_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<time>\d{2}:\d{2}:\d{2}\.\d{6})Z$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CanonicalizationError(ValueError):
    """Raised when a value is not valid canonical JSON v1."""


def utc_now() -> datetime:
    """Return the current aware UTC time.

    Callers that need reproducible tests should inject a time instead of
    monkeypatching this function.
    """

    return datetime.now(UTC)


def format_timestamp(value: datetime) -> str:
    """Return a UTC RFC 3339 timestamp with exactly microsecond precision."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalizationError("timestamp must be timezone-aware")
    normalized = value.astimezone(UTC)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_timestamp(value: str) -> datetime:
    """Parse the persisted timestamp representation accepted by v1."""

    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        raise CanonicalizationError(
            "timestamp must be UTC RFC 3339 with microseconds and Z suffix"
        )
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as error:
        raise CanonicalizationError(f"invalid timestamp: {value!r}") from error
    return parsed.replace(tzinfo=UTC)


def normalize_timestamp(value: datetime | str) -> str:
    """Normalize an aware datetime or RFC 3339 timestamp to persisted form."""

    if isinstance(value, datetime):
        return format_timestamp(value)
    if not isinstance(value, str):
        raise CanonicalizationError("timestamp must be datetime or string")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise CanonicalizationError(f"invalid timestamp: {value!r}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CanonicalizationError("timestamp must include a timezone")
    return format_timestamp(parsed)


def format_date(value: date) -> str:
    """Return a calendar date without inventing a time-of-day."""

    if isinstance(value, datetime):
        raise CanonicalizationError("calendar date cannot be a datetime")
    if not isinstance(value, date):
        raise CanonicalizationError("value must be a date")
    return value.isoformat()


def parse_date(value: str) -> date:
    """Parse an ISO calendar date."""

    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise CanonicalizationError("date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise CanonicalizationError(f"invalid date: {value!r}") from error


def end_of_local_date(value: date | str, timezone: str = "Europe/London") -> datetime:
    """Translate a date-only caller cutoff to the end of that local day.

    This intentionally exists apart from source dates: a caller-provided
    ``as_of`` date has a timezone interpretation while a source period does
    not.
    """

    parsed = parse_date(value) if isinstance(value, str) else value
    if not isinstance(parsed, date) or isinstance(parsed, datetime):
        raise CanonicalizationError("value must be a calendar date")
    try:
        zone = ZoneInfo(timezone)
    except Exception as error:  # ZoneInfoNotFoundError is not on all Python APIs.
        raise CanonicalizationError(f"unknown timezone: {timezone!r}") from error
    local = datetime.combine(parsed, time.max, tzinfo=zone)
    return local.astimezone(UTC)


def new_id(prefix: str) -> str:
    """Create a non-sortable UUID4 entity identity with a stable prefix."""

    if not isinstance(prefix, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", prefix):
        raise CanonicalizationError("ID prefix must be lower-case ASCII identifier")
    return f"{prefix}_{uuid.uuid4().hex}"


def _normalize_string(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def normalize_json(value: Any) -> Any:
    """Return a JSON-compatible value under the canonical JSON v1 rules.

    Floats are rejected even when finite.  Source numeric values must be
    represented as normalised decimal strings so hash identities do not depend
    on binary floating-point formatting.
    """

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _normalize_string(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("NaN and infinity are not canonical JSON")
        raise CanonicalizationError(
            "floats are not accepted in hash-sensitive canonical JSON; "
            "use a decimal string"
        )
    if isinstance(value, datetime):
        return format_timestamp(value)
    if isinstance(value, date):
        return format_date(value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError("canonical JSON object keys must be strings")
            normalized_key = _normalize_string(key)
            if normalized_key in normalized:
                raise CanonicalizationError(
                    f"duplicate object key after Unicode normalization: {normalized_key!r}"
                )
            normalized[normalized_key] = normalize_json(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [normalize_json(item) for item in value]
    raise CanonicalizationError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize a value using the version-one canonical JSON representation."""

    return json.dumps(
        normalize_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def parse_canonical_json(value: str | bytes) -> Any:
    """Decode JSON while rejecting duplicate keys before they become a dict."""

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            normalized = _normalize_string(key)
            if normalized in result:
                raise CanonicalizationError(
                    f"duplicate JSON object key: {normalized!r}"
                )
            result[normalized] = item
        return result

    try:
        parsed = json.loads(value, object_pairs_hook=object_pairs)
    except (TypeError, json.JSONDecodeError) as error:
        raise CanonicalizationError("invalid JSON") from error
    return normalize_json(parsed)


def freeze_json(value: Any) -> Any:
    """Deep-freeze a canonical JSON value for use in frozen descriptors."""

    normalized = normalize_json(value)

    def freeze(item: Any) -> Any:
        if isinstance(item, dict):
            return MappingProxyType({key: freeze(child) for key, child in item.items()})
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(normalized)


def thaw_json(value: Any) -> Any:
    """Return a mutable JSON-compatible copy of a frozen descriptor value."""

    if isinstance(value, Mapping):
        return {str(key): thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(child) for child in value]
    return value


def content_sha256(value: bytes) -> str:
    """Hash raw immutable content bytes."""

    if not isinstance(value, bytes):
        raise TypeError("content hash requires bytes")
    return hashlib.sha256(value).hexdigest()


def hash_canonical(domain: str, value: Any) -> str:
    """Hash canonical JSON with a domain-separated v1 prefix."""

    if not isinstance(domain, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", domain):
        raise CanonicalizationError("hash domain must be a lower-case identifier")
    prefix = f"nan-fung/{domain}/v1\0".encode("utf-8")
    return hashlib.sha256(prefix + canonical_json(value).encode("utf-8")).hexdigest()


def source_hash(value: Any) -> str:
    return hash_canonical("source", value)


def definition_hash(value: Any) -> str:
    return hash_canonical("definition", value)


def request_hash(value: Any) -> str:
    return hash_canonical("request", value)


def locator_hash(value: Any) -> str:
    return hash_canonical("locator", value)


def schedule_rule_hash(value: Any) -> str:
    return hash_canonical("schedule-rule", value)


def watermark_hash(value: Any) -> str:
    return hash_canonical("watermark", value)


def record_key_hash(
    datasource_id: str,
    record_key_version: str,
    record_key: Sequence[Any],
) -> str:
    """Hash a natural key without relying on an unsafe delimiter encoding."""

    if not isinstance(datasource_id, str) or not datasource_id:
        raise CanonicalizationError("datasource_id is required")
    if not isinstance(record_key_version, str) or not record_key_version:
        raise CanonicalizationError("record_key_version is required")
    if not isinstance(record_key, Sequence) or isinstance(record_key, (str, bytes)):
        raise CanonicalizationError("record key must be a JSON array")
    payload = (
        b"nan-fung/record-key/v1\0"
        + datasource_id.encode("utf-8")
        + b"\0"
        + record_key_version.encode("utf-8")
        + b"\0"
        + canonical_json(list(record_key)).encode("utf-8")
    )
    return hashlib.sha256(payload).hexdigest()


def observation_hash(
    *,
    datasource_id: str,
    record_type: str,
    schema_version: str,
    revision_action: str,
    record_key: Sequence[Any],
    payload: Mapping[str, Any],
    definition_version: int | None = None,
    source_date: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    period_label: str | None = None,
    geography_code: str | None = None,
    geography_name: str | None = None,
    unit: str | None = None,
    data_kind: str | None = None,
    confidence: str | None = None,
    snapshot_scope_hash: str | None = None,
    definition: str | None = None,
    limitations: Sequence[Any] | None = None,
) -> str:
    """Hash the immutable semantic observation envelope specified by the ADR."""

    return hash_canonical(
        "observation",
        {
            "datasource_id": datasource_id,
            "definition_version": definition_version,
            "record_type": record_type,
            "schema_version": schema_version,
            "revision_action": revision_action,
            "record_key": list(record_key),
            "payload": payload,
            "source_date": source_date,
            "period_start": period_start,
            "period_end": period_end,
            "period_label": period_label,
            "geography_code": geography_code,
            "geography_name": geography_name,
            "unit": unit,
            "data_kind": data_kind,
            "confidence": confidence,
            "snapshot_scope_hash": snapshot_scope_hash,
            "definition": definition,
            "limitations": list(limitations or ()),
        },
    )
