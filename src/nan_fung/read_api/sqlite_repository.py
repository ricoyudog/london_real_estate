"""SQLite implementation of the bounded read repository protocol."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from nan_fung.storage.db import connect_database

from .access import ReadContext
from .contracts import ReadPage, ReadQuery, ReadRecord, normalise_utc, utc_timestamp


_RECORD_TYPES = {
    "metrics": "metric",
    "supply": "supply",
    "events": "event",
    "geographies": "geography",
}
_PAYLOAD_FILTERS = {
    "metric_id": "$.metric_id",
    "geography_code": "$.geography_code",
    "provider": "$.provider",
}


class SQLiteReadRepository:
    """Read canonical and run-scoped rows without exposing SQL to callers."""

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)

    def query_canonical(
        self, query: ReadQuery, *, as_of: datetime, context: ReadContext
    ) -> Iterable[ReadRecord]:
        if query.query_kind == "health":
            return self._health_rows(as_of=as_of, context=context)
        records = self._observation_rows(
            as_of=as_of,
            context=context,
            run_id=None,
            canonical=True,
        )
        expected_type = _RECORD_TYPES.get(query.query_kind)
        if expected_type is None:
            return records
        return tuple(record for record in records if record.record_type == expected_type)

    def query_result(
        self,
        result_ref: str,
        query: ReadQuery,
        *,
        as_of: datetime,
        context: ReadContext,
    ) -> Iterable[ReadRecord]:
        if not result_ref.startswith("run_"):
            return ()
        records = self._observation_rows(
            as_of=as_of,
            context=context,
            run_id=result_ref,
            canonical=False,
        )
        expected_type = _RECORD_TYPES.get(query.query_kind)
        if expected_type is None:
            return records
        return tuple(record for record in records if record.record_type == expected_type)

    def query_page(
        self,
        query: ReadQuery,
        *,
        as_of: datetime,
        context: ReadContext,
        after: tuple[str, str] | None,
    ) -> ReadPage:
        """Return one SQL-keyset page and its count before ``after``.

        The service authenticates cursor values; this repository only turns the
        trusted `(available_at, observation_id)` boundary into SQL predicates.
        """

        if query.query_kind == "health":
            records = self._health_rows(as_of=as_of, context=context)
            return ReadPage(records[: query.limit + 1], len(records))
        canonical = query.result_ref is None
        run_id = query.result_ref
        if run_id is not None and not run_id.startswith("run_"):
            return ReadPage((), 0)
        return self._observation_page(
            query,
            as_of=as_of,
            context=context,
            run_id=run_id,
            canonical=canonical,
            after=after,
        )

    def _observation_rows(
        self,
        *,
        as_of: datetime,
        context: ReadContext,
        run_id: str | None,
        canonical: bool,
    ) -> tuple[ReadRecord, ...]:
        rows_sql, parameters = _base_observation_sql(
            as_of=as_of,
            context=context,
            run_id=run_id,
            canonical=canonical,
        )
        connection = connect_database(self._database_path, read_only=True)
        try:
            rows = tuple(connection.execute(rows_sql, parameters).fetchall())
            evidence = _evidence_by_observation(connection, rows)
        finally:
            connection.close()
        return tuple(
            _read_record(
                row,
                evidence.get((row["canonical_run_id"], row["observation_id"]), ()),
                canonical=canonical,
                as_of=as_of,
            )
            for row in rows
        )

    def _observation_page(
        self,
        query: ReadQuery,
        *,
        as_of: datetime,
        context: ReadContext,
        run_id: str | None,
        canonical: bool,
        after: tuple[str, str] | None,
    ) -> ReadPage:
        rows_sql, base_parameters = _base_observation_sql(
            as_of=as_of,
            context=context,
            run_id=run_id,
            canonical=canonical,
        )
        filters_sql, filter_parameters = _filter_sql(query)
        count_sql = (
            "SELECT COUNT(*) FROM (" + rows_sql + ") AS candidate WHERE " + filters_sql
        )
        page_filters = filters_sql
        page_parameters: tuple[object, ...] = (*base_parameters, *filter_parameters)
        if after is not None:
            page_filters += (
                " AND (candidate.available_at < ? OR "
                "(candidate.available_at = ? AND candidate.observation_id < ?))"
            )
            page_parameters = (*page_parameters, after[0], after[0], after[1])
        page_sql = (
            "SELECT * FROM ("
            + rows_sql
            + ") AS candidate WHERE "
            + page_filters
            + " ORDER BY candidate.available_at DESC, candidate.observation_id DESC LIMIT ?"
        )
        page_parameters = (*page_parameters, query.limit + 1)

        connection = connect_database(self._database_path, read_only=True)
        try:
            rows = tuple(connection.execute(page_sql, page_parameters).fetchall())
            total_count = connection.execute(
                count_sql, (*base_parameters, *filter_parameters)
            ).fetchone()[0]
            evidence = _evidence_by_observation(connection, rows)
        finally:
            connection.close()
        return ReadPage(
            tuple(
                _read_record(
                    row,
                    evidence.get((row["canonical_run_id"], row["observation_id"]), ()),
                    canonical=canonical,
                    as_of=as_of,
                )
                for row in rows
            ),
            total_count,
        )

    def _health_rows(
        self, *, as_of: datetime, context: ReadContext
    ) -> tuple[ReadRecord, ...]:
        access_classes = tuple(sorted(str(item) for item in context.allowed_access_classes))
        access_sql = ", ".join("?" for _ in access_classes)
        connection = connect_database(self._database_path, read_only=True)
        try:
            rows = connection.execute(
                _HEALTH_SQL.format(access_sql=access_sql), access_classes
            ).fetchall()
        finally:
            connection.close()
        anchor = normalise_utc(as_of)
        records: list[ReadRecord] = []
        for row in rows:
            last_status = row["last_attempt_status"]
            failed_attempt = bool(
                row["last_attempt_at"]
                and last_status in {"failed", "partial", "cancelled"}
            )
            has_last_retrieval = bool(row["last_retrieval_at"])
            last_retrieval = (
                _parse_timestamp(row["last_retrieval_at"])
                if has_last_retrieval
                else None
            )
            canonical_available = bool(row["last_promoted_run_id"])
            retrieval_freshness = _freshness_for(last_retrieval, row, anchor)
            observation_freshness = _freshness_for(
                _source_timestamp(row), row, anchor
            )
            degraded = failed_attempt or "stale" in {
                retrieval_freshness,
                observation_freshness,
            }
            records.append(
                ReadRecord(
                    observation_id=f"health:{row['datasource_id']}",
                    datasource_id=row["datasource_id"],
                    query_kind="health",
                    category=row["category"],
                    record_type="health",
                    access_class=row["access_class"],
                    available_at=anchor,
                    payload={
                        "definition_version": row["definition_version"],
                        "status": row["status"],
                        "last_attempt_at": row["last_attempt_at"],
                        "last_attempt_status": last_status,
                        "last_success_at": row["last_success_at"],
                        "last_attempt_lane": row["last_attempt_lane"],
                        "last_retrieval_at": row["last_retrieval_at"],
                        "last_retrieval_lane": row["last_retrieval_lane"],
                        "last_promoted_run_id": row["last_promoted_run_id"],
                        "last_promoted_at": row["last_promoted_at"],
                        "latest_observation_date": row["source_date"],
                        "next_due_at": row["next_due_at"],
                    },
                    retrieval_freshness=(
                        retrieval_freshness if has_last_retrieval else "never_ingested"
                    ),
                    observation_freshness=(
                        observation_freshness if canonical_available else "never_ingested"
                    ),
                    degraded=degraded,
                    canonical_available=canonical_available,
                )
            )
        return tuple(records)


def _base_observation_sql(
    *,
    as_of: datetime,
    context: ReadContext,
    run_id: str | None,
    canonical: bool,
) -> tuple[str, tuple[object, ...]]:
    access_classes = tuple(sorted(str(item) for item in context.allowed_access_classes))
    access_sql = ", ".join("?" for _ in access_classes)
    anchor = utc_timestamp(normalise_utc(as_of))
    if canonical:
        return (
            _CANONICAL_AS_OF_SQL.format(access_sql=access_sql),
            (anchor, anchor, anchor, anchor, *access_classes),
        )
    return (
        _RUN_RESULT_SQL.format(access_sql=access_sql),
        (run_id, anchor, *access_classes),
    )


def _filter_sql(query: ReadQuery) -> tuple[str, tuple[object, ...]]:
    conditions: list[str] = []
    parameters: list[object] = []
    expected_type = _RECORD_TYPES.get(query.query_kind)
    if expected_type is not None:
        conditions.append("candidate.record_type = ?")
        parameters.append(expected_type)

    for key, wanted in query.filters.items():
        values = wanted if isinstance(wanted, tuple) else (wanted,)
        placeholders = ", ".join("?" for _ in values)
        if key in {"datasource_id", "category", "record_type", "observation_id"}:
            conditions.append(f"candidate.{key} IN ({placeholders})")
            parameters.extend(values)
        elif key in _PAYLOAD_FILTERS:
            conditions.append(
                f"CAST(json_extract(candidate.payload_json, ?) AS TEXT) IN ({placeholders})"
            )
            parameters.append(_PAYLOAD_FILTERS[key])
            parameters.extend(values)
        elif key == "evidence_id":
            conditions.append(
                "EXISTS ("
                "SELECT 1 FROM observation_evidence AS filtered_evidence "
                "WHERE filtered_evidence.run_id = candidate.canonical_run_id "
                "AND filtered_evidence.observation_id = candidate.observation_id "
                f"AND filtered_evidence.evidence_id IN ({placeholders})"
                ")"
            )
            parameters.extend(values)
        elif key == "source_date_from":
            conditions.append("candidate.source_date >= ?")
            parameters.extend(values)
        elif key == "source_date_to":
            conditions.append("candidate.source_date <= ?")
            parameters.extend(values)
    return " AND ".join(conditions) or "1 = 1", tuple(parameters)


def _evidence_by_observation(
    connection: object, rows: Iterable[object]
) -> Mapping[tuple[str, str], tuple[object, ...]]:
    pairs = tuple(
        dict.fromkeys(
            (row["canonical_run_id"], row["observation_id"])  # type: ignore[index]
            for row in rows
        )
    )
    if not pairs:
        return {}
    pair_conditions = " OR ".join(
        "(oe.run_id = ? AND oe.observation_id = ?)" for _ in pairs
    )
    parameters = tuple(item for pair in pairs for item in pair)
    evidence_rows = connection.execute(  # type: ignore[attr-defined]
        f"""
        SELECT oe.run_id, oe.observation_id, oe.evidence_id, e.retrieved_at
        FROM observation_evidence AS oe
        JOIN evidence_artifact AS e ON e.evidence_id = oe.evidence_id
        WHERE {pair_conditions}
        ORDER BY oe.run_id, oe.observation_id, oe.evidence_id
        """,
        parameters,
    ).fetchall()
    grouped: dict[tuple[str, str], list[object]] = {}
    for row in evidence_rows:
        grouped.setdefault((row["run_id"], row["observation_id"]), []).append(row)
    return {key: tuple(value) for key, value in grouped.items()}


def _read_record(
    row: object,
    evidence_rows: Iterable[object],
    *,
    canonical: bool,
    as_of: datetime,
) -> ReadRecord:
    evidence = tuple(evidence_rows)
    retrieved_values = [item["retrieved_at"] for item in evidence]
    source_date = row["source_date"]  # type: ignore[index]
    retrieved_at = _parse_timestamp(max(retrieved_values)) if retrieved_values else None
    available_at = _parse_timestamp(row["available_at"])  # type: ignore[index]
    retrieval_freshness = _freshness_for(retrieved_at, row, as_of)
    observation_freshness = _freshness_for(_source_timestamp(row), row, as_of)
    return ReadRecord(
        observation_id=row["observation_id"],  # type: ignore[index]
        datasource_id=row["datasource_id"],  # type: ignore[index]
        query_kind=_query_kind_for_type(row["record_type"]),  # type: ignore[index]
        category=row["category"],  # type: ignore[index]
        record_type=row["record_type"],  # type: ignore[index]
        access_class=row["access_class"],  # type: ignore[index]
        available_at=available_at,
        payload=json.loads(row["payload_json"]),  # type: ignore[index]
        evidence_ids=tuple(item["evidence_id"] for item in evidence),
        source_date=date.fromisoformat(source_date) if source_date else None,
        retrieved_at=retrieved_at,
        unit=row["unit"],  # type: ignore[index]
        definition=row["definition"],  # type: ignore[index]
        period_label=row["period_label"],  # type: ignore[index]
        retrieval_freshness=retrieval_freshness,
        observation_freshness=observation_freshness,
        degraded="stale" in {retrieval_freshness, observation_freshness},
        canonical_available=canonical,
        canonical=canonical,
        lane=row["lane"],  # type: ignore[index]
    )


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _source_timestamp(row: object) -> datetime | None:
    """Return upstream observation time, never local promotion time."""

    try:
        value = row["source_date"]  # type: ignore[index]
    except (KeyError, TypeError):
        return None
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.combine(date.fromisoformat(value), datetime.min.time(), UTC)
    except ValueError:
        return None


def _freshness_for(value: datetime | None, row: object, as_of: datetime) -> str:
    """Derive status from immutable schedule metadata and an as-of anchor.

    ``freshness_policy.status == configured`` is the v1 registry profile.  Its
    cadence comes from the definition snapshot that produced the row, rather
    than from mutable host configuration.  Future definition versions may pin
    explicit retrieval/observation windows in ``freshness_policy``.
    """

    if value is None:
        return "unknown"
    windows = _freshness_windows(row)
    if windows is None:
        return "not_applicable"
    fresh_for, stale_after = windows
    age = max(timedelta(), normalise_utc(as_of) - normalise_utc(value))
    if age <= fresh_for:
        return "fresh"
    if age <= stale_after:
        return "aging"
    return "stale"


def _freshness_windows(row: object) -> tuple[timedelta, timedelta] | None:
    try:
        definition = json.loads(row["definition_json"])  # type: ignore[index]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(definition, dict):
        return None
    policy = definition.get("freshness_policy")
    if not isinstance(policy, dict):
        return None
    if policy.get("status") == "not_applicable":
        return None
    explicit = _explicit_freshness_windows(policy)
    if explicit is not None:
        return explicit
    if policy.get("status") != "configured":
        return None
    interval = _schedule_interval(definition)
    if interval is None:
        mode = definition.get("automation_mode")
        interval = timedelta(days=1 if mode == "on_demand" else 30)
    return interval * 3 // 2, interval * 3


def _explicit_freshness_windows(
    policy: Mapping[str, object],
) -> tuple[timedelta, timedelta] | None:
    fresh_seconds = policy.get("fresh_for_seconds")
    stale_seconds = policy.get("stale_after_seconds")
    if isinstance(fresh_seconds, bool) or isinstance(stale_seconds, bool):
        return None
    if not isinstance(fresh_seconds, int) or not isinstance(stale_seconds, int):
        return None
    if fresh_seconds < 0 or stale_seconds <= fresh_seconds:
        return None
    return timedelta(seconds=fresh_seconds), timedelta(seconds=stale_seconds)


def _schedule_interval(definition: Mapping[str, object]) -> timedelta | None:
    schedules = definition.get("schedules")
    if not isinstance(schedules, list):
        return None
    intervals = [_rule_interval(item.get("rule")) for item in schedules if isinstance(item, dict)]
    actual = [interval for interval in intervals if interval is not None]
    return min(actual) if actual else None


def _rule_interval(rule: object) -> timedelta | None:
    if not isinstance(rule, dict):
        return None
    kind = rule.get("kind")
    if kind == "interval" and isinstance(rule.get("seconds"), int):
        seconds = rule["seconds"]
        return timedelta(seconds=seconds) if seconds > 0 else None
    if kind == "daily":
        return timedelta(days=1)
    if kind == "weekly":
        weekdays = rule.get("weekdays")
        if isinstance(weekdays, list) and weekdays:
            values = sorted({day for day in weekdays if isinstance(day, int) and 0 <= day <= 6})
            if values:
                gaps = [
                    (values[(index + 1) % len(values)] - day) % 7 or 7
                    for index, day in enumerate(values)
                ]
                return timedelta(days=max(gaps))
        return timedelta(days=7)
    if kind == "monthly":
        months = rule.get("months")
        if isinstance(months, list) and months:
            values = sorted({month for month in months if isinstance(month, int) and 1 <= month <= 12})
            if values:
                gaps = [
                    (values[(index + 1) % len(values)] - month) % 12 or 12
                    for index, month in enumerate(values)
                ]
                return timedelta(days=31 * max(gaps))
        return timedelta(days=31)
    return None


def _query_kind_for_type(record_type: str) -> str:
    for query_kind, candidate in _RECORD_TYPES.items():
        if candidate == record_type:
            return query_kind
    raise ValueError(f"unsupported canonical record type {record_type!r}")


_CANONICAL_AS_OF_SQL = """
WITH promotion_at_t AS (
    SELECT * FROM (
        SELECT p.*, row_number() OVER (
            PARTITION BY p.run_id ORDER BY p.decision_at DESC, p.promotion_seq DESC
        ) AS decision_rank
        FROM run_promotion AS p
        WHERE p.decision_at <= ?
    ) WHERE decision_rank = 1 AND decision = 'approved'
), eligible AS (
    SELECT ro.run_id AS canonical_run_id,
           CASE WHEN p.decision_at > a.completed_at THEN p.decision_at ELSE a.completed_at END AS available_at,
           r.lane, o.*, d.access_class, d.automation_mode, d.definition_json
    FROM run_observation AS ro
    JOIN ingestion_run AS r ON r.run_id = ro.run_id
    JOIN workflow_attempt AS a ON a.attempt_id = r.attempt_id
    JOIN observation_revision AS o ON o.observation_id = ro.observation_id
    JOIN promotion_at_t AS p ON p.run_id = r.run_id
    JOIN datasource_definition AS d
      ON d.datasource_id = o.datasource_id AND d.definition_version = o.definition_version
    WHERE r.lane = 'production_ingestion'
      AND a.status = 'succeeded'
      AND a.completed_at <= ?
      AND o.lane = 'production_ingestion'
      AND NOT EXISTS (
          SELECT 1 FROM observation_evidence AS oe
          JOIN evidence_artifact AS e ON e.evidence_id = oe.evidence_id
          WHERE oe.run_id = ro.run_id
            AND oe.observation_id = ro.observation_id
            AND e.retrieved_at > ?
      )
), ranked AS (
    SELECT eligible.*, row_number() OVER (
        PARTITION BY datasource_id, record_key_version, record_key_hash
        ORDER BY revision_no DESC, available_at DESC, canonical_run_id DESC
    ) AS record_rank
    FROM eligible
    WHERE available_at <= ?
)
SELECT * FROM ranked
WHERE record_rank = 1
  AND revision_action = 'upsert'
  AND access_class IN ({access_sql})
"""


_RUN_RESULT_SQL = """
SELECT ro.run_id AS canonical_run_id,
       r.lane, a.completed_at AS available_at, o.*, d.access_class,
       d.automation_mode, d.definition_json
FROM run_observation AS ro
JOIN ingestion_run AS r ON r.run_id = ro.run_id
JOIN workflow_attempt AS a ON a.attempt_id = r.attempt_id
JOIN observation_revision AS o ON o.observation_id = ro.observation_id
JOIN datasource_definition AS d
  ON d.datasource_id = o.datasource_id AND d.definition_version = o.definition_version
WHERE ro.run_id = ?
  AND r.lane IN ('source_discovery', 'ad_hoc_research')
  AND a.status IN ('succeeded', 'empty', 'partial')
  AND a.completed_at <= ?
  AND d.access_class IN ({access_sql})
"""


_HEALTH_SQL = """
WITH attempt_rows AS (
    SELECT r.datasource_id, r.definition_version, r.run_id, r.lane,
           a.attempt_id, a.status, a.started_at, a.completed_at,
           row_number() OVER (
               PARTITION BY r.datasource_id, r.definition_version
               ORDER BY a.started_at DESC, a.attempt_id DESC
           ) AS attempt_rank
    FROM ingestion_run AS r
    JOIN workflow_attempt AS a ON a.attempt_id = r.attempt_id
), attempt_summary AS (
    SELECT datasource_id, definition_version,
           MAX(started_at) AS last_attempt_at,
           MAX(CASE WHEN status = 'succeeded' THEN completed_at END) AS last_success_at
    FROM attempt_rows
    GROUP BY datasource_id, definition_version
), latest_attempt AS (
    SELECT datasource_id, definition_version, status AS last_attempt_status,
           lane AS last_attempt_lane
    FROM attempt_rows
    WHERE attempt_rank = 1
), successful_rows AS (
    SELECT datasource_id, definition_version, run_id, lane,
           completed_at AS last_retrieval_at,
           row_number() OVER (
               PARTITION BY datasource_id, definition_version
               ORDER BY completed_at DESC, attempt_id DESC
           ) AS success_rank
    FROM attempt_rows
    WHERE status = 'succeeded'
), latest_success AS (
    SELECT datasource_id, definition_version, run_id, lane, last_retrieval_at
    FROM successful_rows
    WHERE success_rank = 1
), canonical_rows AS (
    SELECT datasource_id, definition_version, canonical_run_id,
           available_at AS last_promoted_at, source_date,
           row_number() OVER (
               PARTITION BY datasource_id, definition_version
               ORDER BY available_at DESC, canonical_run_id DESC
           ) AS canonical_rank
    FROM canonical_event_v1
), latest_canonical AS (
    SELECT datasource_id, definition_version, canonical_run_id,
           last_promoted_at, source_date
    FROM canonical_rows
    WHERE canonical_rank = 1
), schedule_summary AS (
    SELECT datasource_id, definition_version, MIN(next_due_at) AS next_due_at
    FROM workflow_schedule
    WHERE enabled = 1
    GROUP BY datasource_id, definition_version
)
SELECT d.datasource_id, d.definition_version, d.category, d.access_class, d.status,
       d.automation_mode, d.definition_json,
       summary.last_attempt_at, summary.last_success_at,
       latest.last_attempt_status, latest.last_attempt_lane,
       success.last_retrieval_at, success.lane AS last_retrieval_lane,
       canonical.canonical_run_id AS last_promoted_run_id,
       canonical.last_promoted_at, canonical.source_date,
       schedule.next_due_at
FROM datasource_definition AS d
LEFT JOIN attempt_summary AS summary
  ON summary.datasource_id = d.datasource_id
 AND summary.definition_version = d.definition_version
LEFT JOIN latest_attempt AS latest
  ON latest.datasource_id = d.datasource_id
 AND latest.definition_version = d.definition_version
LEFT JOIN latest_success AS success
  ON success.datasource_id = d.datasource_id
 AND success.definition_version = d.definition_version
LEFT JOIN latest_canonical AS canonical
  ON canonical.datasource_id = d.datasource_id
 AND canonical.definition_version = d.definition_version
LEFT JOIN schedule_summary AS schedule
  ON schedule.datasource_id = d.datasource_id
 AND schedule.definition_version = d.definition_version
WHERE d.access_class IN ({access_sql})
"""
