"""Bounded canonical-to-filesystem projection delivery.

This is the operator-facing boundary for the otherwise pure projection
helpers.  It reads only the canonical SQLite read view, rebuilds the derived
SQLite indexes under the existing writer lease, and atomically publishes a
small, fixed set of files below one caller-supplied output directory.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from types import MappingProxyType

from nan_fung.operational import OperationalStore
from nan_fung.read_api import AccessClass, ReadContext, ReadQuery, SQLiteReadRepository
from nan_fung.read_api.contracts import ReadRecord, utc_timestamp

from .alerts import DeterministicAlert, ThresholdAlertRule, evaluate_alerts
from .models import ProjectionRow, build_projection_rows
from .rebuild import ProjectionRebuildReport, rebuild_sqlite_projections
from .snapshots import MarketSnapshot, build_snapshot
from .wiki import RenderedMarketWikiPage, render_market_wiki


PROJECTION_DELIVERY_SCHEMA_VERSION = "projection_delivery.v1"
DELIVERY_ARTIFACT_TYPES = ("wiki", "daily", "weekly", "alerts")
MAX_DELIVERY_ROWS = 10_000
MAX_ALERT_RULES = 100
_QUERY_KINDS = ("metrics", "supply", "events", "geographies")
_PAGE_SIZE = 100


class ProjectionDeliveryError(RuntimeError):
    """The fixed projection-delivery contract could not be satisfied."""


def _normalise_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProjectionDeliveryError("as_of_at must be timezone-aware")
    return value.astimezone(UTC)


def _sha256(content: bytes) -> str:
    return sha256(content).hexdigest()


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _snapshot_source_hash(snapshot: MarketSnapshot) -> str:
    prefix = "snap_"
    if not snapshot.snapshot_id.startswith(prefix):
        raise ProjectionDeliveryError("snapshot ID is not deterministic")
    return snapshot.snapshot_id.removeprefix(prefix)


@dataclass(frozen=True)
class DeliveredProjectionArtifact:
    """One atomically published output with its canonical-input fingerprint."""

    artifact_type: str
    path: Path
    content_sha256: str
    source_hash: str
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.artifact_type not in DELIVERY_ARTIFACT_TYPES:
            raise ProjectionDeliveryError("unknown delivery artifact type")
        for value in (self.content_sha256, self.source_hash):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ProjectionDeliveryError("delivery hashes must be SHA-256 hex")
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    def as_json(self) -> dict[str, object]:
        return {
            "type": self.artifact_type,
            "path": str(self.path),
            "content_sha256": self.content_sha256,
            "source_hash": self.source_hash,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ProjectionDeliveryReport:
    """Deterministic hand-off record for a later durable audit writer."""

    schema_version: str
    delivery_id: str
    as_of_at: datetime
    output_directory: Path
    rebuild: ProjectionRebuildReport
    artifacts: tuple[DeliveredProjectionArtifact, ...]
    canonical_observation_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    alert_count: int
    audit_path: Path
    audit_content_sha256: str
    audit_source_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != PROJECTION_DELIVERY_SCHEMA_VERSION:
            raise ProjectionDeliveryError("unsupported delivery report schema")
        if not self.delivery_id.startswith("delivery_"):
            raise ProjectionDeliveryError("delivery ID must be deterministic")
        object.__setattr__(self, "as_of_at", _normalise_utc(self.as_of_at))
        object.__setattr__(self, "output_directory", Path(self.output_directory))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "canonical_observation_ids", tuple(self.canonical_observation_ids))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "audit_path", Path(self.audit_path))

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "delivery_id": self.delivery_id,
            "as_of_at": utc_timestamp(self.as_of_at),
            "output_directory": str(self.output_directory),
            "canonical_only": True,
            "rebuild": self.rebuild.as_json(),
            "canonical_observation_ids": list(self.canonical_observation_ids),
            "evidence_ids": list(self.evidence_ids),
            "alert_count": self.alert_count,
            "artifacts": [artifact.as_json() for artifact in self.artifacts],
            "audit": {
                "path": str(self.audit_path),
                "content_sha256": self.audit_content_sha256,
                "source_hash": self.audit_source_hash,
            },
        }


def deliver_canonical_projections(
    database_path: str | Path,
    output_directory: str | Path,
    *,
    as_of_at: datetime,
    alert_rules: Sequence[ThresholdAlertRule] = (),
    wiki_page_id: str = "market",
    wiki_title: str = "Market data",
    _writer_locked: bool = False,
) -> ProjectionDeliveryReport:
    """Publish the fixed wiki, daily, weekly, and alert outputs.

    ``as_of_at`` is intentionally required so an operator can replay the same
    canonical state deterministically.  The returned report contains no raw
    evidence and can be persisted by a trusted caller in its own audit ledger.
    """

    anchor = _normalise_utc(as_of_at)
    database = Path(database_path).expanduser().resolve()
    output_root = Path(output_directory).expanduser().resolve()
    rules = _normalise_rules(alert_rules)
    if not wiki_page_id or not wiki_title:
        raise ProjectionDeliveryError("wiki_page_id and wiki_title must be non-empty")

    # The same lease used by ingestion and the SQLite rebuild keeps the
    # canonical read and filesystem publication coherent with one local writer.
    writer_session = (
        nullcontext()
        if _writer_locked
        else OperationalStore(database.parent).writer_session()
    )
    with writer_session:
        rebuild = rebuild_sqlite_projections(database, _writer_locked=True)
        records_by_kind = _load_canonical_records(database, anchor)
        rows = _build_rows(records_by_kind)
        snapshot = build_snapshot(rows, as_of_at=anchor, clock=lambda: anchor)
        rendered_wiki = render_market_wiki(
            rows,
            page_id=wiki_page_id,
            title=wiki_title,
            canonical_anchor=anchor,
        )
        alerts = evaluate_alerts(snapshot, rules)
        artifacts, audit_payload = _render_delivery(
            output_root=output_root,
            anchor=anchor,
            rebuild=rebuild,
            rows=rows,
            snapshot=snapshot,
            rendered_wiki=rendered_wiki,
            alerts=alerts,
            rules=rules,
        )
        audit_path, audit_content = _publish_delivery(
            output_root,
            artifacts,
            audit_payload,
        )

    return ProjectionDeliveryReport(
        schema_version=PROJECTION_DELIVERY_SCHEMA_VERSION,
        delivery_id=str(audit_payload["delivery_id"]),
        as_of_at=anchor,
        output_directory=output_root,
        rebuild=rebuild,
        artifacts=tuple(artifact for artifact, _ in artifacts),
        canonical_observation_ids=tuple(row.observation_id for row in rows),
        evidence_ids=tuple(sorted({evidence_id for row in rows for evidence_id in row.evidence_ids})),
        alert_count=len(alerts),
        audit_path=audit_path,
        audit_content_sha256=_sha256(audit_content),
        audit_source_hash=str(audit_payload["delivery_source_hash"]),
    )


def _normalise_rules(
    alert_rules: Sequence[ThresholdAlertRule],
) -> tuple[ThresholdAlertRule, ...]:
    rules = tuple(alert_rules)
    if len(rules) > MAX_ALERT_RULES:
        raise ProjectionDeliveryError(f"at most {MAX_ALERT_RULES} alert rules are allowed")
    if any(not isinstance(rule, ThresholdAlertRule) for rule in rules):
        raise ProjectionDeliveryError("alert rules must be ThresholdAlertRule values")
    ordered = tuple(sorted(rules, key=lambda rule: rule.rule_id))
    if len({rule.rule_id for rule in ordered}) != len(ordered):
        raise ProjectionDeliveryError("alert rule IDs must be unique")
    return ordered


def _load_canonical_records(
    database_path: Path,
    anchor: datetime,
) -> dict[str, tuple[ReadRecord, ...]]:
    repository = SQLiteReadRepository(database_path)
    context = ReadContext(
        "projection-delivery",
        frozenset(AccessClass),
    )
    records_by_kind: dict[str, tuple[ReadRecord, ...]] = {}
    known_total = 0
    for query_kind in _QUERY_KINDS:
        query = ReadQuery(query_kind=query_kind, as_of=anchor, limit=_PAGE_SIZE)
        after: tuple[str, str] | None = None
        records: list[ReadRecord] = []
        while True:
            page = repository.query_page(
                query,
                as_of=anchor,
                context=context,
                after=after,
            )
            if after is None:
                known_total += page.total_count
                if known_total > MAX_DELIVERY_ROWS:
                    raise ProjectionDeliveryError(
                        f"canonical delivery exceeds {MAX_DELIVERY_ROWS} rows"
                    )
            records.extend(page.records[:_PAGE_SIZE])
            if len(page.records) <= _PAGE_SIZE:
                break
            boundary = page.records[_PAGE_SIZE - 1]
            after = (utc_timestamp(boundary.available_at), boundary.observation_id)
        records_by_kind[query_kind] = tuple(records)
    return records_by_kind


def _build_rows(records_by_kind: Mapping[str, Sequence[ReadRecord]]) -> tuple[ProjectionRow, ...]:
    rows = tuple(
        row
        for projection_kind in _QUERY_KINDS
        for row in build_projection_rows(
            records_by_kind[projection_kind],
            projection_kind=projection_kind,
        )
    )
    return tuple(
        sorted(
            rows,
            key=lambda row: (row.projection_kind, row.datasource_id, row.observation_id),
        )
    )


def _render_delivery(
    *,
    output_root: Path,
    anchor: datetime,
    rebuild: ProjectionRebuildReport,
    rows: tuple[ProjectionRow, ...],
    snapshot: MarketSnapshot,
    rendered_wiki: RenderedMarketWikiPage,
    alerts: tuple[DeterministicAlert, ...],
    rules: tuple[ThresholdAlertRule, ...],
) -> tuple[
    tuple[tuple[DeliveredProjectionArtifact, bytes], ...],
    dict[str, object],
]:
    daily_path = _output_path(output_root, "daily", f"{anchor.date().isoformat()}.json")
    iso_year, iso_week, _ = anchor.date().isocalendar()
    weekly_path = _output_path(output_root, "weekly", f"{iso_year}-W{iso_week:02d}.json")
    artifact_inputs = (
        (
            "wiki",
            _output_path(output_root, "wiki", "market.md"),
            rendered_wiki.content.encode("utf-8"),
            rendered_wiki.source_hash,
            {
                "page_id": rendered_wiki.page_id,
                "observation_ids": list(rendered_wiki.observation_ids),
                "evidence_ids": list(rendered_wiki.evidence_ids),
                "access_class": str(rendered_wiki.access_class) if rendered_wiki.access_class else None,
                "degraded": rendered_wiki.degraded,
            },
        ),
        (
            "daily",
            daily_path,
            _snapshot_bytes(snapshot, cadence="daily"),
            _snapshot_source_hash(snapshot),
            _snapshot_details(snapshot, cadence="daily"),
        ),
        (
            "weekly",
            weekly_path,
            _snapshot_bytes(snapshot, cadence="weekly"),
            _snapshot_source_hash(snapshot),
            _snapshot_details(snapshot, cadence="weekly"),
        ),
        (
            "alerts",
            _output_path(output_root, "alerts", f"{anchor.date().isoformat()}.json"),
            _alerts_bytes(snapshot, anchor=anchor, alerts=alerts, rules=rules),
            _alerts_source_hash(snapshot, alerts=alerts, rules=rules),
            {
                "snapshot_id": snapshot.snapshot_id,
                "alert_count": len(alerts),
                "alert_ids": [alert.alert_id for alert in alerts],
                "observation_ids": [alert.observation_id for alert in alerts],
            },
        ),
    )
    artifacts = tuple(
        (
            DeliveredProjectionArtifact(
                artifact_type=artifact_type,
                path=path,
                content_sha256=_sha256(content),
                source_hash=source_hash,
                details=details,
            ),
            content,
        )
        for artifact_type, path, content, source_hash, details in artifact_inputs
    )
    delivery_source_hash = _delivery_source_hash(
        anchor=anchor,
        artifacts=tuple(artifact for artifact, _ in artifacts),
        rules=rules,
    )
    delivery_id = f"delivery_{delivery_source_hash}"
    audit_payload = {
        "schema_version": PROJECTION_DELIVERY_SCHEMA_VERSION,
        "delivery_id": delivery_id,
        "delivery_source_hash": delivery_source_hash,
        "as_of_at": utc_timestamp(anchor),
        "canonical_only": True,
        "rebuild": rebuild.as_json(),
        "canonical_observation_ids": [row.observation_id for row in rows],
        "evidence_ids": sorted({evidence_id for row in rows for evidence_id in row.evidence_ids}),
        "alert_rule_ids": [rule.rule_id for rule in rules],
        "artifacts": [artifact.as_json() for artifact, _ in artifacts],
    }
    return artifacts, audit_payload


def _snapshot_bytes(snapshot: MarketSnapshot, *, cadence: str) -> bytes:
    return _json_bytes(
        {
            "schema_version": "snapshot_delivery.v1",
            "cadence": cadence,
            "snapshot_id": snapshot.snapshot_id,
            "as_of_at": utc_timestamp(snapshot.as_of_at),
            "generated_at": utc_timestamp(snapshot.generated_at),
            "datasource_ids": list(snapshot.datasource_ids),
            "access_class": str(snapshot.access_class) if snapshot.access_class else None,
            "degraded": snapshot.degraded,
            "rows": [_row_json(row) for row in snapshot.rows],
        }
    )


def _snapshot_details(snapshot: MarketSnapshot, *, cadence: str) -> dict[str, object]:
    return {
        "cadence": cadence,
        "snapshot_id": snapshot.snapshot_id,
        "observation_ids": [row.observation_id for row in snapshot.rows],
        "evidence_ids": sorted(
            {evidence_id for row in snapshot.rows for evidence_id in row.evidence_ids}
        ),
        "access_class": str(snapshot.access_class) if snapshot.access_class else None,
        "degraded": snapshot.degraded,
    }


def _alerts_bytes(
    snapshot: MarketSnapshot,
    *,
    anchor: datetime,
    alerts: tuple[DeterministicAlert, ...],
    rules: tuple[ThresholdAlertRule, ...],
) -> bytes:
    return _json_bytes(
        {
            "schema_version": "alert_delivery.v1",
            "as_of_at": utc_timestamp(anchor),
            "snapshot_id": snapshot.snapshot_id,
            "rule_ids": [rule.rule_id for rule in rules],
            "alerts": [_alert_json(alert) for alert in alerts],
        }
    )


def _alerts_source_hash(
    snapshot: MarketSnapshot,
    *,
    alerts: tuple[DeterministicAlert, ...],
    rules: tuple[ThresholdAlertRule, ...],
) -> str:
    return _sha256(
        _json_bytes(
            {
                "snapshot_id": snapshot.snapshot_id,
                "rules": [_rule_json(rule) for rule in rules],
                "alerts": [_alert_json(alert) for alert in alerts],
            }
        )
    )


def _delivery_source_hash(
    *,
    anchor: datetime,
    artifacts: tuple[DeliveredProjectionArtifact, ...],
    rules: tuple[ThresholdAlertRule, ...],
) -> str:
    return _sha256(
        _json_bytes(
            {
                "schema_version": PROJECTION_DELIVERY_SCHEMA_VERSION,
                "as_of_at": utc_timestamp(anchor),
                "artifacts": [
                    {
                        "type": artifact.artifact_type,
                        "source_hash": artifact.source_hash,
                        "content_sha256": artifact.content_sha256,
                    }
                    for artifact in artifacts
                ],
                "rules": [_rule_json(rule) for rule in rules],
            }
        )
    )


def _row_json(row: ProjectionRow) -> dict[str, object]:
    return {
        "projection_kind": row.projection_kind,
        "observation_id": row.observation_id,
        "datasource_id": row.datasource_id,
        "access_class": str(row.access_class),
        "available_at": utc_timestamp(row.available_at),
        "fields": dict(row.fields),
        "evidence_ids": list(row.evidence_ids),
        "source_date": row.source_date.isoformat() if row.source_date else None,
        "unit": row.unit,
        "definition": row.definition,
        "period_label": row.period_label,
        "retrieved_at": utc_timestamp(row.retrieved_at) if row.retrieved_at else None,
        "degraded": row.degraded,
    }


def _alert_json(alert: DeterministicAlert) -> dict[str, object]:
    return {
        "alert_id": alert.alert_id,
        "rule_id": alert.rule_id,
        "snapshot_id": alert.snapshot_id,
        "observation_id": alert.observation_id,
        "datasource_id": alert.datasource_id,
        "value": str(alert.value),
        "threshold": str(alert.threshold),
        "comparator": alert.comparator,
        "access_class": str(alert.access_class),
        "evidence_ids": list(alert.evidence_ids),
        "state": alert.state,
    }


def _rule_json(rule: ThresholdAlertRule) -> dict[str, object]:
    return {
        "rule_id": rule.rule_id,
        "field": rule.field,
        "comparator": rule.comparator,
        "threshold": str(rule.threshold),
        "match": dict(rule.match),
    }


def _output_path(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts)
    try:
        path.resolve().relative_to(root)
    except ValueError as error:
        raise ProjectionDeliveryError("output path escaped its configured directory") from error
    return path


def _publish_delivery(
    output_root: Path,
    artifacts: tuple[tuple[DeliveredProjectionArtifact, bytes], ...],
    audit_payload: Mapping[str, object],
) -> tuple[Path, bytes]:
    for artifact, content in artifacts:
        _atomic_write(artifact.path, content)
    audit_path = _output_path(output_root, "audit", f"{audit_payload['delivery_id']}.json")
    audit_content = _json_bytes(dict(audit_payload))
    _atomic_write(audit_path, audit_content)
    return audit_path, audit_content


def _atomic_write(path: Path, content: bytes) -> None:
    """Fsync a sibling temporary file before atomically replacing ``path``."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
