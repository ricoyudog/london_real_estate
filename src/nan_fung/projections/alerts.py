"""Pure deterministic threshold-alert evaluation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json

from nan_fung.read_api import AccessClass

from .models import ProjectionError, ProjectionRow
from .snapshots import MarketSnapshot


COMPARATORS = frozenset({"gt", "gte", "lt", "lte", "eq"})


@dataclass(frozen=True)
class ThresholdAlertRule:
    rule_id: str
    field: str
    comparator: str
    threshold: Decimal | str | int
    match: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.rule_id or not self.field:
            raise ValueError("rule_id and field must be non-empty")
        if self.comparator not in COMPARATORS:
            raise ValueError("unsupported alert comparator")
        try:
            threshold = Decimal(str(self.threshold))
        except InvalidOperation as error:
            raise ValueError("threshold must be a decimal value") from error
        if not threshold.is_finite():
            raise ValueError("threshold must be finite")
        object.__setattr__(self, "threshold", threshold)
        object.__setattr__(self, "match", dict(self.match))


@dataclass(frozen=True)
class DeterministicAlert:
    alert_id: str
    rule_id: str
    snapshot_id: str
    observation_id: str
    datasource_id: str
    value: Decimal
    threshold: Decimal
    comparator: str
    access_class: AccessClass
    evidence_ids: tuple[str, ...]
    state: str = "open"


def _default_value(row: ProjectionRow, field: str) -> object | None:
    return row.fields.get(field)


def _matches_rule(row: ProjectionRow, rule: ThresholdAlertRule) -> bool:
    for key, expected in rule.match.items():
        if key == "datasource_id":
            actual = row.datasource_id
        elif key == "projection_kind":
            actual = row.projection_kind
        else:
            actual = row.fields.get(key)
        if str(actual) != expected:
            return False
    return True


def _compare(value: Decimal, comparator: str, threshold: Decimal) -> bool:
    return {
        "gt": value > threshold,
        "gte": value >= threshold,
        "lt": value < threshold,
        "lte": value <= threshold,
        "eq": value == threshold,
    }[comparator]


def evaluate_alerts(
    snapshot: MarketSnapshot,
    rules: Sequence[ThresholdAlertRule],
    *,
    value_getter: Callable[[ProjectionRow, str], object | None] = _default_value,
) -> tuple[DeterministicAlert, ...]:
    """Evaluate fixed threshold rules in a stable order.

    The optional ``value_getter`` is the only dependency-injection point.  It
    makes source-specific typed fields possible without allowing an agent to
    decide alert logic at runtime.
    """

    alerts: list[DeterministicAlert] = []
    for rule in sorted(rules, key=lambda candidate: candidate.rule_id):
        for row in snapshot.rows:
            if not _matches_rule(row, rule):
                continue
            raw_value = value_getter(row, rule.field)
            if raw_value is None:
                continue
            try:
                value = Decimal(str(raw_value))
            except InvalidOperation as error:
                raise ProjectionError(
                    f"alert field {rule.field!r} is not numeric for {row.observation_id}"
                ) from error
            if not value.is_finite():
                raise ProjectionError("alert values must be finite")
            if not _compare(value, rule.comparator, rule.threshold):
                continue
            fingerprint = {
                "rule_id": rule.rule_id,
                "snapshot_id": snapshot.snapshot_id,
                "observation_id": row.observation_id,
                "value": str(value),
                "threshold": str(rule.threshold),
                "comparator": rule.comparator,
            }
            alert_id = "alert_" + sha256(
                json.dumps(
                    fingerprint, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            alerts.append(
                DeterministicAlert(
                    alert_id=alert_id,
                    rule_id=rule.rule_id,
                    snapshot_id=snapshot.snapshot_id,
                    observation_id=row.observation_id,
                    datasource_id=row.datasource_id,
                    value=value,
                    threshold=rule.threshold,
                    comparator=rule.comparator,
                    access_class=row.access_class,
                    evidence_ids=row.evidence_ids,
                )
            )
    return tuple(sorted(alerts, key=lambda alert: (alert.rule_id, alert.observation_id)))
