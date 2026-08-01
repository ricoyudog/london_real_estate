"""Deterministic SQLite convenience-projection rebuilds from canonical rows."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any

from nan_fung.ingestion.canonical import canonical_json
from nan_fung.storage.db import connect_database, transaction


@dataclass(frozen=True)
class ProjectionRebuildReport:
    schema_version: str
    metric_count: int
    supply_count: int
    event_count: int
    geography_count: int

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "metric_count": self.metric_count,
            "supply_count": self.supply_count,
            "event_count": self.event_count,
            "geography_count": self.geography_count,
        }


def rebuild_sqlite_projections(
    database_path: str | Path, *, _writer_locked: bool = False
) -> ProjectionRebuildReport:
    """Replace derived indexes using only ``canonical_latest_v1``.

    Discovery/ad-hoc runs never appear in that view, so this function cannot
    accidentally make them canonical by rebuilding a projection.
    """

    path = Path(database_path)
    if not _writer_locked:
        # Keep the public path safe for trusted operators that only have a
        # database path.  OperationalStore uses the same lock file, so a
        # projection rebuild cannot bypass the daemon's single writer lease.
        from nan_fung.operational import OperationalStore

        with OperationalStore(path.parent).writer_session():
            return rebuild_sqlite_projections(path, _writer_locked=True)

    connection = connect_database(path)
    try:
        with transaction(connection):
            rows = connection.execute(
                """
                SELECT observation_id, datasource_id, category, record_type,
                       payload_json, source_date, unit
                FROM canonical_latest_v1
                ORDER BY datasource_id, observation_id
                """
            ).fetchall()
            for table in ("metric_value", "supply_project", "market_event", "geography"):
                connection.execute(f"DELETE FROM {table}")
            counts = {"metric": 0, "supply": 0, "event": 0, "geography": 0}
            for row in rows:
                payload = _object_payload(row["payload_json"])
                record_type = row["record_type"]
                if record_type == "metric":
                    numeric_text, numeric_value = _metric_value(payload)
                    connection.execute(
                        """
                        INSERT INTO metric_value (observation_id, metric_name, numeric_value, numeric_text)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            row["observation_id"],
                            str(payload.get("metric_name") or row["datasource_id"]),
                            numeric_value,
                            numeric_text,
                        ),
                    )
                    counts["metric"] += 1
                elif record_type == "supply":
                    connection.execute(
                        """
                        INSERT INTO supply_project (
                            observation_id, project_name, status, expected_completion_date
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            row["observation_id"],
                            _text(payload.get("project_name") or payload.get("name")),
                            _text(payload.get("status")),
                            _text(
                                payload.get("expected_completion_date")
                                or payload.get("completion_date")
                            ),
                        ),
                    )
                    counts["supply"] += 1
                elif record_type == "event":
                    connection.execute(
                        """
                        INSERT INTO market_event (observation_id, event_type, event_at, relevance_status)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            row["observation_id"],
                            _text(payload.get("event_type")),
                            _text(payload.get("event_at") or row["source_date"]),
                            _text(payload.get("relevance_status")),
                        ),
                    )
                    counts["event"] += 1
                elif record_type == "geography":
                    geometry = payload.get("geometry")
                    connection.execute(
                        """
                        INSERT INTO geography (observation_id, geometry_json, srid)
                        VALUES (?, ?, ?)
                        """,
                        (
                            row["observation_id"],
                            canonical_json(geometry) if geometry is not None else None,
                            _integer(payload.get("srid")),
                        ),
                    )
                    counts["geography"] += 1
        return ProjectionRebuildReport(
            "projection_rebuild.v1",
            counts["metric"],
            counts["supply"],
            counts["event"],
            counts["geography"],
        )
    finally:
        connection.close()


def _object_payload(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("canonical observation payload must be an object")
    return value


def _metric_value(payload: dict[str, Any]) -> tuple[str, float | None]:
    for key in ("numeric_text", "value", "bank_rate_percent", "amount", "rate"):
        value = payload.get(key)
        if value is None:
            continue
        text = str(value)
        try:
            decimal = Decimal(text)
        except InvalidOperation:
            return text, None
        if decimal.is_finite():
            return text, float(decimal)
        return text, None
    return canonical_json(payload), None


def _text(value: object) -> str | None:
    return str(value) if value is not None else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) else None
