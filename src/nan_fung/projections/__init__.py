"""Pure canonical projections, snapshots, alerts, and Wiki rendering."""

from .alerts import COMPARATORS, DeterministicAlert, ThresholdAlertRule, evaluate_alerts
from .delivery import (
    DELIVERY_ARTIFACT_TYPES,
    MAX_ALERT_RULES,
    MAX_DELIVERY_ROWS,
    PROJECTION_DELIVERY_SCHEMA_VERSION,
    DeliveredProjectionArtifact,
    ProjectionDeliveryError,
    ProjectionDeliveryReport,
    deliver_canonical_projections,
)
from .models import (
    PROJECTION_KINDS,
    PROJECTION_SCHEMA_VERSION,
    NonCanonicalProjectionInput,
    ProjectionError,
    ProjectionRow,
    build_event_projections,
    build_geography_projections,
    build_metric_projections,
    build_projection_rows,
    build_supply_projections,
    projection_access_class,
)
from .snapshots import MarketSnapshot, SNAPSHOT_SCHEMA_VERSION, build_snapshot
from .rebuild import ProjectionRebuildReport, rebuild_sqlite_projections
from .wiki import WIKI_RENDER_SCHEMA_VERSION, RenderedMarketWikiPage, render_market_wiki

__all__ = [
    "COMPARATORS",
    "DELIVERY_ARTIFACT_TYPES",
    "MAX_ALERT_RULES",
    "MAX_DELIVERY_ROWS",
    "PROJECTION_DELIVERY_SCHEMA_VERSION",
    "PROJECTION_KINDS",
    "PROJECTION_SCHEMA_VERSION",
    "SNAPSHOT_SCHEMA_VERSION",
    "WIKI_RENDER_SCHEMA_VERSION",
    "DeterministicAlert",
    "DeliveredProjectionArtifact",
    "MarketSnapshot",
    "NonCanonicalProjectionInput",
    "ProjectionError",
    "ProjectionDeliveryError",
    "ProjectionDeliveryReport",
    "ProjectionRow",
    "ProjectionRebuildReport",
    "RenderedMarketWikiPage",
    "ThresholdAlertRule",
    "build_event_projections",
    "build_geography_projections",
    "build_metric_projections",
    "build_projection_rows",
    "build_snapshot",
    "build_supply_projections",
    "deliver_canonical_projections",
    "evaluate_alerts",
    "projection_access_class",
    "render_market_wiki",
    "rebuild_sqlite_projections",
]
