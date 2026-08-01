"""Deterministic daily/weekly snapshot input construction."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json

from nan_fung.read_api import AccessClass

from .models import PROJECTION_SCHEMA_VERSION, ProjectionError, ProjectionRow, projection_access_class


SNAPSHOT_SCHEMA_VERSION = "snapshot.v1"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProjectionError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class MarketSnapshot:
    schema_version: str
    snapshot_id: str
    as_of_at: datetime
    generated_at: datetime
    rows: tuple[ProjectionRow, ...]
    datasource_ids: tuple[str, ...]
    access_class: AccessClass | None
    degraded: bool


def build_snapshot(
    rows: Iterable[ProjectionRow],
    *,
    as_of_at: datetime,
    clock: Callable[[], datetime] = _utc_now,
) -> MarketSnapshot:
    """Build a stable snapshot without querying storage or an agent runtime."""

    as_of_timestamp = _timestamp(as_of_at)
    ordered_rows = tuple(
        sorted(
            rows,
            key=lambda row: (
                row.projection_kind,
                row.datasource_id,
                row.observation_id,
            ),
        )
    )
    if any(not row.canonical or row.lane != "production_ingestion" for row in ordered_rows):
        raise ProjectionError("snapshots accept only canonical production rows")
    if any(row.available_at > as_of_at.astimezone(UTC) for row in ordered_rows):
        raise ProjectionError("snapshot cannot include a row unavailable at its as_of")
    semantic_rows = [
        {
            "kind": row.projection_kind,
            "observation_id": row.observation_id,
            "datasource_id": row.datasource_id,
            "access_class": str(row.access_class),
            "available_at": _timestamp(row.available_at),
            "source_date": row.source_date.isoformat() if row.source_date else None,
            "unit": row.unit,
            "definition": row.definition,
            "period_label": row.period_label,
            "evidence_ids": list(row.evidence_ids),
            "fields": dict(row.fields),
            "degraded": row.degraded,
        }
        for row in ordered_rows
    ]
    snapshot_id = "snap_" + sha256(
        json.dumps(
            {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "projection_schema_version": PROJECTION_SCHEMA_VERSION,
                "as_of_at": as_of_timestamp,
                "rows": semantic_rows,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return MarketSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        snapshot_id=snapshot_id,
        as_of_at=as_of_at.astimezone(UTC),
        generated_at=clock().astimezone(UTC),
        rows=ordered_rows,
        datasource_ids=tuple(sorted({row.datasource_id for row in ordered_rows})),
        access_class=projection_access_class(ordered_rows),
        degraded=any(row.degraded for row in ordered_rows),
    )
