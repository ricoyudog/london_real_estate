"""Canonical-only typed projection helpers.

Projection functions are pure: persistence and the transactional outbox remain
the responsibility of the data-plane workflow that calls them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from types import MappingProxyType

from nan_fung.read_api import AccessClass, ReadRecord, most_restrictive_access


PROJECTION_SCHEMA_VERSION = "projections.v1"
PROJECTION_KINDS = frozenset({"metrics", "supply", "events", "geographies"})


class ProjectionError(ValueError):
    code = "PROJECTION_ERROR"


class NonCanonicalProjectionInput(ProjectionError):
    code = "NON_CANONICAL_PROJECTION_INPUT"


def _normalise_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProjectionError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class ProjectionRow:
    """A query-shaped canonical fact with its immutable lineage."""

    projection_kind: str
    observation_id: str
    datasource_id: str
    access_class: AccessClass | str
    available_at: datetime
    fields: Mapping[str, object]
    evidence_ids: tuple[str, ...]
    source_date: date | None = None
    unit: str | None = None
    definition: str | None = None
    period_label: str | None = None
    retrieved_at: datetime | None = None
    degraded: bool = False
    canonical: bool = True
    lane: str = "production_ingestion"

    def __post_init__(self) -> None:
        if self.projection_kind not in PROJECTION_KINDS:
            raise ProjectionError(f"unknown projection kind {self.projection_kind!r}")
        if not self.observation_id or not self.datasource_id:
            raise ProjectionError("projection lineage IDs must be non-empty")
        if any(not evidence_id for evidence_id in self.evidence_ids):
            raise ProjectionError("evidence IDs must be non-empty")
        object.__setattr__(self, "access_class", AccessClass(self.access_class))
        object.__setattr__(self, "available_at", _normalise_utc(self.available_at))
        if self.retrieved_at is not None:
            object.__setattr__(self, "retrieved_at", _normalise_utc(self.retrieved_at))
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))


def _require_canonical(records: Iterable[ReadRecord]) -> tuple[ReadRecord, ...]:
    values = tuple(records)
    invalid = next(
        (
            record
            for record in values
            if not record.canonical or record.lane != "production_ingestion"
        ),
        None,
    )
    if invalid is not None:
        raise NonCanonicalProjectionInput(
            "projections accept only promoted production_ingestion records"
        )
    return values


def build_projection_rows(
    records: Iterable[ReadRecord],
    *,
    projection_kind: str,
    transform: Callable[[ReadRecord], Mapping[str, object]] | None = None,
) -> tuple[ProjectionRow, ...]:
    """Project canonical records deterministically with an injected mapper."""

    if projection_kind not in PROJECTION_KINDS:
        raise ProjectionError(f"unknown projection kind {projection_kind!r}")
    canonical_records = _require_canonical(records)
    mapper = transform or (lambda record: record.payload)
    rows = []
    for record in canonical_records:
        if record.query_kind != projection_kind:
            raise ProjectionError(
                f"record {record.observation_id} is {record.query_kind!r}, not {projection_kind!r}"
            )
        fields = mapper(record)
        if not isinstance(fields, Mapping):
            raise ProjectionError("projection transform must return a mapping")
        rows.append(
            ProjectionRow(
                projection_kind=projection_kind,
                observation_id=record.observation_id,
                datasource_id=record.datasource_id,
                access_class=record.access_class,
                available_at=record.available_at,
                fields=fields,
                evidence_ids=record.evidence_ids,
                source_date=record.source_date,
                unit=record.unit,
                definition=record.definition,
                period_label=record.period_label,
                retrieved_at=record.retrieved_at,
                degraded=record.degraded,
            )
        )
    return tuple(sorted(rows, key=lambda row: (row.datasource_id, row.observation_id)))


def build_metric_projections(
    records: Iterable[ReadRecord],
    *,
    transform: Callable[[ReadRecord], Mapping[str, object]] | None = None,
) -> tuple[ProjectionRow, ...]:
    return build_projection_rows(records, projection_kind="metrics", transform=transform)


def build_supply_projections(
    records: Iterable[ReadRecord],
    *,
    transform: Callable[[ReadRecord], Mapping[str, object]] | None = None,
) -> tuple[ProjectionRow, ...]:
    return build_projection_rows(records, projection_kind="supply", transform=transform)


def build_event_projections(
    records: Iterable[ReadRecord],
    *,
    transform: Callable[[ReadRecord], Mapping[str, object]] | None = None,
) -> tuple[ProjectionRow, ...]:
    return build_projection_rows(records, projection_kind="events", transform=transform)


def build_geography_projections(
    records: Iterable[ReadRecord],
    *,
    transform: Callable[[ReadRecord], Mapping[str, object]] | None = None,
) -> tuple[ProjectionRow, ...]:
    return build_projection_rows(records, projection_kind="geographies", transform=transform)


def projection_access_class(rows: Iterable[ProjectionRow]) -> AccessClass | None:
    return most_restrictive_access(row.access_class for row in rows)
