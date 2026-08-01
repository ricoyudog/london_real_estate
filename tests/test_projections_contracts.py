from datetime import UTC, date, datetime, timedelta

import pytest

from nan_fung.projections import (
    NonCanonicalProjectionInput,
    ProjectionError,
    ProjectionRow,
    ThresholdAlertRule,
    build_metric_projections,
    build_snapshot,
    evaluate_alerts,
    render_market_wiki,
)
from nan_fung.read_api import AccessClass, ReadRecord


NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


def record(
    observation_id: str,
    *,
    value: str = "5.00",
    access_class: AccessClass = AccessClass.OPEN,
    canonical: bool = True,
    lane: str = "production_ingestion",
) -> ReadRecord:
    return ReadRecord(
        observation_id=observation_id,
        datasource_id="boe.bank_rate.iudbedr",
        query_kind="metrics",
        category="macro",
        record_type="bank_rate",
        access_class=access_class,
        available_at=NOW - timedelta(minutes=1),
        payload={"metric_id": "bank_rate", "value": value, "note": "<script>alert(1)</script>"},
        evidence_ids=(f"ev_{observation_id}",),
        source_date=date(2026, 8, 1),
        unit="percent",
        definition="Bank Rate",
        canonical=canonical,
        lane=lane,
    )


def test_projection_rows_are_canonical_only_and_deterministically_sorted() -> None:
    rows = build_metric_projections([record("obs_b"), record("obs_a")])

    assert [row.observation_id for row in rows] == ["obs_a", "obs_b"]
    assert rows[0].fields["metric_id"] == "bank_rate"
    with pytest.raises(NonCanonicalProjectionInput):
        build_metric_projections([record("obs_discovery", canonical=False, lane="source_discovery")])


def test_snapshot_is_rebuildable_and_exposes_no_downgrade_access_class() -> None:
    rows = build_metric_projections(
        [record("obs_open"), record("obs_restricted", access_class=AccessClass.RESTRICTED)]
    )
    first = build_snapshot(rows, as_of_at=NOW, clock=lambda: NOW)
    second = build_snapshot(tuple(reversed(rows)), as_of_at=NOW, clock=lambda: NOW)

    assert first.snapshot_id == second.snapshot_id
    assert first.access_class is AccessClass.RESTRICTED
    assert first.datasource_ids == ("boe.bank_rate.iudbedr",)
    assert first.generated_at == NOW


def test_wiki_render_is_canonical_only_escaped_and_deterministic() -> None:
    rows = build_metric_projections([record("obs_1")])
    first = render_market_wiki(
        rows,
        page_id="macro/bank-rate",
        title="Bank Rate",
        canonical_anchor=NOW,
    )
    second = render_market_wiki(
        rows,
        page_id="macro/bank-rate",
        title="Bank Rate",
        canonical_anchor=NOW,
    )

    assert first.content == second.content
    assert first.source_hash == second.source_hash
    assert "&lt;script&gt;" in first.content
    assert first.observation_ids == ("obs_1",)
    noncanonical_row = ProjectionRow(
        projection_kind="metrics",
        observation_id="obs_bad",
        datasource_id="test",
        access_class=AccessClass.OPEN,
        available_at=NOW,
        fields={},
        evidence_ids=(),
        canonical=False,
        lane="ad_hoc_research",
    )
    with pytest.raises(ProjectionError):
        render_market_wiki(
            [noncanonical_row], page_id="bad", title="bad", canonical_anchor=NOW
        )


def test_snapshot_and_alert_evaluation_are_deterministic() -> None:
    rows = build_metric_projections([record("obs_1", value="5.00")])
    snapshot = build_snapshot(rows, as_of_at=NOW, clock=lambda: NOW)
    rule = ThresholdAlertRule(
        rule_id="bank-rate-high",
        field="value",
        comparator="gte",
        threshold="5",
        match={"metric_id": "bank_rate"},
    )

    alerts = evaluate_alerts(snapshot, [rule])

    assert len(alerts) == 1
    assert alerts[0].observation_id == "obs_1"
    assert alerts[0].evidence_ids == ("ev_obs_1",)
    assert alerts == evaluate_alerts(snapshot, [rule])


def test_snapshot_rejects_future_unavailable_rows() -> None:
    row = ProjectionRow(
        projection_kind="metrics",
        observation_id="obs_future",
        datasource_id="test",
        access_class=AccessClass.OPEN,
        available_at=NOW + timedelta(seconds=1),
        fields={"value": "1"},
        evidence_ids=("ev_future",),
    )
    with pytest.raises(ProjectionError):
        build_snapshot([row], as_of_at=NOW, clock=lambda: NOW)
