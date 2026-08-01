from nan_fung.datasources.catalog import datasource_workflow_catalog
from nan_fung.ingestion.registry import default_registry


def test_every_registry_datasource_has_explicit_workflow_or_blocked_state() -> None:
    catalog = datasource_workflow_catalog()
    registered = {definition.datasource_id for definition in default_registry().definitions}

    assert set(catalog) == registered
    assert catalog["boe.bank_rate.iudbedr"].state == "operational"
    assert catalog["ons.gdp.ecyx"].state == "operational"
    assert catalog["nomis.nm_59_1.london_lfs"].degraded_behavior == "current_vintage_backfill_blocked"
    assert catalog["ons.onspd.postcode"].state == "operational"
    assert (
        catalog["ons.onspd.postcode"].degraded_behavior
        == "on_demand_explicit_retention_required"
    )
    assert catalog["rightmove.commercial_insights_tracker"].state == "manual_review"
    assert catalog["pld.applications_search"].state == "blocked"
