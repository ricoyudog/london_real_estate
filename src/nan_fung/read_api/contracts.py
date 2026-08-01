"""Versioned, transport-free contracts for canonical datasource reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
import json
from types import MappingProxyType
from typing import Iterable, Mapping, Protocol, TypeAlias, runtime_checkable

from .access import AccessClass, ReadContext


READ_SCHEMA_VERSION = "read_api.v1"
QUERY_KINDS = frozenset(
    {"metrics", "supply", "events", "geographies", "health"}
)
ALLOWED_FILTERS = frozenset(
    {
        "datasource_id",
        "category",
        "record_type",
        "metric_id",
        "geography_code",
        "provider",
        "observation_id",
        "evidence_id",
        "source_date_from",
        "source_date_to",
    }
)
FRESHNESS_VALUES = frozenset(
    {"fresh", "aging", "stale", "never_ingested", "unknown", "not_applicable"}
)
MAX_RECORD_PAYLOAD_BYTES = 65_536
MAX_FILTER_VALUES_PER_KEY = 50
MAX_FILTER_VALUES_TOTAL = 100
MAX_FILTER_VALUE_CHARS = 512

FilterValue: TypeAlias = str | tuple[str, ...]


class ReadApiError(ValueError):
    """A versioned read-contract error with a safe, stable code."""

    code = "READ_API_ERROR"


class InvalidReadRequest(ReadApiError):
    code = "INVALID_READ_REQUEST"


class InvalidCursor(ReadApiError):
    code = "INVALID_CURSOR"


class AccessDenied(ReadApiError):
    code = "ACCESS_DENIED"


def normalise_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidReadRequest("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def utc_timestamp(value: datetime) -> str:
    return normalise_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalise_filters(filters: Mapping[str, object]) -> Mapping[str, FilterValue]:
    normalised: dict[str, FilterValue] = {}
    total_values = 0
    for key, value in filters.items():
        if key not in ALLOWED_FILTERS:
            raise InvalidReadRequest(f"filter {key!r} is not allowed")
        if isinstance(value, str):
            values = (value,)
        elif isinstance(value, tuple) and all(isinstance(item, str) for item in value):
            values = value
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            values = tuple(value)
        else:
            raise InvalidReadRequest(f"filter {key!r} must contain strings")
        if not values or any(not item for item in values):
            raise InvalidReadRequest(f"filter {key!r} must not be empty")
        if len(values) > MAX_FILTER_VALUES_PER_KEY:
            raise InvalidReadRequest(
                f"filter {key!r} accepts at most {MAX_FILTER_VALUES_PER_KEY} values"
            )
        if any(len(item) > MAX_FILTER_VALUE_CHARS for item in values):
            raise InvalidReadRequest(
                f"filter {key!r} values must be at most {MAX_FILTER_VALUE_CHARS} characters"
            )
        total_values += len(values)
        if total_values > MAX_FILTER_VALUES_TOTAL:
            raise InvalidReadRequest(
                f"queries accept at most {MAX_FILTER_VALUES_TOTAL} filter values"
            )
        if key in {"source_date_from", "source_date_to"} and len(values) != 1:
            raise InvalidReadRequest(f"filter {key!r} accepts exactly one value")
        normalised[key] = values[0] if len(values) == 1 else values
    return MappingProxyType(normalised)


@dataclass(frozen=True)
class ReadQuery:
    """A bounded query.  It deliberately has no SQL, URL, or table fields."""

    query_kind: str
    filters: Mapping[str, object] = field(default_factory=dict)
    as_of: datetime | None = None
    cursor: str | None = None
    limit: int = 100
    result_ref: str | None = None

    def __post_init__(self) -> None:
        if self.query_kind not in QUERY_KINDS:
            raise InvalidReadRequest(f"unknown query kind {self.query_kind!r}")
        if not 1 <= self.limit <= 100:
            raise InvalidReadRequest("limit must be between 1 and 100")
        if self.as_of is not None:
            object.__setattr__(self, "as_of", normalise_utc(self.as_of))
        if self.cursor is not None and (not self.cursor or len(self.cursor) > 4_096):
            raise InvalidReadRequest("cursor must be non-empty when supplied")
        if self.result_ref is not None and (not self.result_ref or len(self.result_ref) > 512):
            raise InvalidReadRequest("result_ref must be non-empty when supplied")
        object.__setattr__(self, "filters", _normalise_filters(self.filters))


@dataclass(frozen=True)
class ReadRecord:
    """A typed, already-normalised row returned by a read repository.

    The contract has only metadata and bounded normalised facts.  It has no
    artifact path, raw bytes, SQL expression, or arbitrary text-extraction
    field, so it cannot be used to turn the read API into evidence storage.
    """

    observation_id: str
    datasource_id: str
    query_kind: str
    category: str
    record_type: str
    access_class: AccessClass | str
    available_at: datetime
    payload: Mapping[str, object] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    source_date: date | None = None
    retrieved_at: datetime | None = None
    unit: str | None = None
    definition: str | None = None
    period_label: str | None = None
    retrieval_freshness: str = "unknown"
    observation_freshness: str = "unknown"
    degraded: bool = False
    canonical_available: bool = True
    canonical: bool = True
    lane: str = "production_ingestion"

    def __post_init__(self) -> None:
        for name, value in (
            ("observation_id", self.observation_id),
            ("datasource_id", self.datasource_id),
            ("category", self.category),
            ("record_type", self.record_type),
        ):
            if not value:
                raise InvalidReadRequest(f"{name} must be non-empty")
        if self.query_kind not in QUERY_KINDS:
            raise InvalidReadRequest(f"unknown query kind {self.query_kind!r}")
        if self.retrieval_freshness not in FRESHNESS_VALUES:
            raise InvalidReadRequest("invalid retrieval freshness")
        if self.observation_freshness not in FRESHNESS_VALUES:
            raise InvalidReadRequest("invalid observation freshness")
        evidence_ids = tuple(self.evidence_ids)
        if any(not evidence_id for evidence_id in evidence_ids):
            raise InvalidReadRequest("evidence IDs must be non-empty")
        object.__setattr__(self, "access_class", AccessClass(self.access_class))
        object.__setattr__(self, "available_at", normalise_utc(self.available_at))
        if self.retrieved_at is not None:
            object.__setattr__(self, "retrieved_at", normalise_utc(self.retrieved_at))
        object.__setattr__(self, "evidence_ids", evidence_ids)
        try:
            payload = json.dumps(
                dict(self.payload),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise InvalidReadRequest("record payload must be bounded JSON-compatible data") from error
        if len(payload.encode("utf-8")) > MAX_RECORD_PAYLOAD_BYTES:
            raise InvalidReadRequest("record payload exceeds the read response bound")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class ReadResponse:
    schema_version: str
    query_kind: str
    anchor_as_of: datetime
    records: tuple[ReadRecord, ...]
    next_cursor: str | None
    total_count: int
    canonical: bool
    access_class: AccessClass | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReadPage:
    """One bounded repository page plus the count before its cursor boundary."""

    records: tuple[ReadRecord, ...]
    total_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        if self.total_count < 0:
            raise InvalidReadRequest("page total_count must not be negative")


@runtime_checkable
class ReadRepository(Protocol):
    """Read-only repository boundary implemented by the data plane."""

    def query_canonical(
        self, query: ReadQuery, *, as_of: datetime, context: ReadContext
    ) -> Iterable[ReadRecord]: ...


@runtime_checkable
class PagedReadRepository(Protocol):
    """Optional cursor-aware fast path for repositories that can page in storage."""

    def query_page(
        self,
        query: ReadQuery,
        *,
        as_of: datetime,
        context: ReadContext,
        after: tuple[str, str] | None,
    ) -> ReadPage: ...

    def query_result(
        self,
        result_ref: str,
        query: ReadQuery,
        *,
        as_of: datetime,
        context: ReadContext,
    ) -> Iterable[ReadRecord]: ...


class InMemoryReadRepository:
    """Small deterministic repository useful for tests and local adapters."""

    def __init__(
        self,
        canonical_records: Iterable[ReadRecord] = (),
        result_records: Mapping[str, Iterable[ReadRecord]] | None = None,
    ) -> None:
        self._canonical_records = tuple(canonical_records)
        self._result_records = {
            result_ref: tuple(records)
            for result_ref, records in (result_records or {}).items()
        }

    def query_canonical(
        self, query: ReadQuery, *, as_of: datetime, context: ReadContext
    ) -> Iterable[ReadRecord]:
        return tuple(
            record
            for record in self._canonical_records
            if record.available_at <= as_of and context.allows(record.access_class)
        )

    def query_result(
        self,
        result_ref: str,
        query: ReadQuery,
        *,
        as_of: datetime,
        context: ReadContext,
    ) -> Iterable[ReadRecord]:
        return tuple(
            record
            for record in self._result_records.get(result_ref, ())
            if record.available_at <= as_of and context.allows(record.access_class)
        )
