"""Explicit legacy-fetcher compatibility and workflow coverage catalogue.

This is deliberately a static declaration, not a generic connector framework.
It prevents a legacy direct fetcher from being mistaken for a canonical
ingestion workflow while still documenting the available source-specific parser
and review paths.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DatasourceWorkflowCapability:
    datasource_id: str
    state: str
    legacy_adapter: str | None
    degraded_behavior: str


_CAPABILITIES = (
    DatasourceWorkflowCapability(
        "boe.bank_rate.iudbedr", "operational", "fetch_bank_rate", "last_good_preserved"
    ),
    DatasourceWorkflowCapability(
        "boe.mpc_news", "legacy_adapter", "fetch_latest_mpc_decision", "discovery_only_pending_retention"
    ),
    DatasourceWorkflowCapability(
        "boe.mpc_content", "blocked", None, "content_evidence_requires_approval"
    ),
    DatasourceWorkflowCapability("ons.gdp.ecyx", "operational", "fetch_uk_gdp", "current_vintage_backfill_blocked"),
    DatasourceWorkflowCapability("ons.gdp.ihyq", "operational", "fetch_uk_gdp", "current_vintage_backfill_blocked"),
    DatasourceWorkflowCapability("ons.inflation.d7g7", "operational", "fetch_uk_inflation", "current_vintage_backfill_blocked"),
    DatasourceWorkflowCapability("ons.inflation.l55o", "operational", "fetch_uk_inflation", "current_vintage_backfill_blocked"),
    DatasourceWorkflowCapability("ons.inflation.czbh", "operational", "fetch_uk_inflation", "current_vintage_backfill_blocked"),
    DatasourceWorkflowCapability("ons.labour.lf24", "operational", "fetch_uk_labour_market", "current_vintage_backfill_blocked"),
    DatasourceWorkflowCapability("ons.labour.mgsx", "operational", "fetch_uk_labour_market", "current_vintage_backfill_blocked"),
    DatasourceWorkflowCapability("ons.labour.ap2y", "operational", "fetch_uk_labour_market", "current_vintage_backfill_blocked"),
    DatasourceWorkflowCapability("ons.labour.kai9", "operational", "fetch_uk_labour_market", "current_vintage_backfill_blocked"),
    DatasourceWorkflowCapability("nomis.nm_59_1.london_lfs", "operational", "fetch_london_labour_market", "current_vintage_backfill_blocked"),
    DatasourceWorkflowCapability("nomis.nm_130_1.london_workforce_jobs", "operational", "fetch_london_labour_market", "current_vintage_backfill_blocked"),
    DatasourceWorkflowCapability("voa.ndr_office_stock", "operational", "fetch_voa_office_stock", "release_history_backfill_blocked"),
    DatasourceWorkflowCapability("ons.opn.hybrid_working", "operational", "fetch_hybrid_working", "release_history_backfill_blocked"),
    DatasourceWorkflowCapability("mhclg.epc.live_table_a_london", "operational", "fetch_non_domestic_epc_ratings", "all_non_domestic_proxy_scope_preserved"),
    DatasourceWorkflowCapability("pld.applications_search", "blocked", "search_planning_applications", "licence_and_retention_unapproved"),
    DatasourceWorkflowCapability("pld.application", "blocked", "fetch_planning_application", "licence_and_retention_unapproved"),
    DatasourceWorkflowCapability("govuk.search.market_news", "blocked", "search_market_news", "review_and_access_unapproved"),
    DatasourceWorkflowCapability("govuk.content.market_news", "blocked", "fetch_content_item", "review_and_access_unapproved"),
    DatasourceWorkflowCapability("ons.onspd.postcode", "operational", "lookup_postcode", "on_demand_explicit_retention_required"),
    DatasourceWorkflowCapability("gla.town_centre_boundaries", "blocked", "query_town_centres", "source_approval_pending"),
    DatasourceWorkflowCapability("bnp.central_london_office_report", "manual_review", "fetch_public_market_report", "human_review_and_restricted_retention"),
    DatasourceWorkflowCapability("rightmove.commercial_insights_tracker", "manual_review", None, "manual_evidence_only"),
    DatasourceWorkflowCapability("custom.london_office_submarkets", "manual_review", None, "approved_internal_configuration_required"),
)


def datasource_workflow_catalog() -> dict[str, DatasourceWorkflowCapability]:
    """Return one capability declaration for every seeded datasource."""

    return {item.datasource_id: item for item in _CAPABILITIES}
