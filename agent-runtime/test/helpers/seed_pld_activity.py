"""Test-only canonical PLD planning-activity seeder; not shipped runtime logic.

Run from the repository root:
    uv run python agent-runtime/test/helpers/seed_pld_activity.py <data_dir>
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from nan_fung.operational import OperationalStore


RETRIEVED_AT = datetime.now(UTC)
PLD_URL = "https://files.planning.data.gov.uk/dataset/planning-application.csv"

# Tiny fixture CSV: one month of Camden + City of London decided applications.
# Mirrors the real Crown-copyright planning-application schema; only
# organisation-entity and decision-date are populated (the rest are empty in
# source data per the T1 probe).
FIXTURE_CSV = (
    "dataset,end-date,entity,entry-date,geojson,geometry,name,organisation-entity,point,prefix,reference,start-date,typology,address-text,decision-date,description,development-classification,documentation-url,ground-area,notes,organisation,planning-application-status,planning-application-type,planning-decision,planning-decision-type,uprn\n"
    "planning-application,,10000000001,2026-07-15,,,,90,,planning-application,CAM-001,,geography,,2026-07-10,Camden test application 1,,,,,,,,,\n"
    "planning-application,,10000000002,2026-07-15,,,,90,,planning-application,CAM-002,,geography,,2026-07-12,Camden test application 2,,,,,,,,,\n"
    "planning-application,,10000000003,2026-07-15,,,,90,,planning-application,CAM-003,,geography,,2026-07-15,Camden test application 3,,,,,,,,,\n"
    "planning-application,,10000000004,2026-07-15,,,,203,,planning-application,COL-001,,geography,,2026-07-08,City of London test application 1,,,,,,,,,\n"
    "planning-application,,10000000005,2026-07-15,,,,203,,planning-application,COL-002,,geography,,2026-07-20,City of London test application 2,,,,,,,,,\n"
)


def seed_pld_activity(data_dir: Path) -> None:
    """Seed a tiny Camden + City of London planning-activity fixture."""

    store = OperationalStore(data_dir)
    store.sync_registry(now=RETRIEVED_AT)
    queued = store.enqueue(
        "pld.applications_search",
        scheduled_for=RETRIEVED_AT,
        request={"fixture": "pi-agent-runtime-phase-2-pld"},
        trigger="manual",
    )
    claim = store.claim_job(queued.job_id, "fixture-worker", now=RETRIEVED_AT)
    if claim is None:
        raise RuntimeError("fixture job could not be claimed")
    run = store.start_run(claim, "fixture-worker", now=RETRIEVED_AT)
    evidence = store.persist_evidence(
        run,
        FIXTURE_CSV.encode(),
        media_type="text/csv",
        request={"method": "GET", "url": PLD_URL},
        response={
            "status": 200,
            "final_url": PLD_URL,
            "published_at": "2026-07-15T10:00:00Z",
            "source_updated_at": "2026-07-15T10:00:00Z",
        },
        retrieved_at=RETRIEVED_AT,
        source_id="pld.api",
        now=RETRIEVED_AT,
    )
    # Two canonical observations: Camden July 2026 = 3, City of London July 2026 = 2.
    for entity, borough, count in (("90", "Camden", "3"), ("203", "City of London", "2")):
        store.persist_observation(
            run,
            record_key=(entity, "2026-07"),
            payload={
                "organisation_entity": entity,
                "geography_code": entity,
                "borough": borough,
                "period_year": "2026",
                "period_month": "07",
                "planning_application_count": count,
                "metric_id": "planning_application_count",
            },
            record_type="metric",
            category="planning_activity",
            evidence=(evidence,),
            source_date="2026-07-31",
            unit="count",
            definition_text=(
                "Monthly count of decided planning applications per London "
                "planning authority (fixture seed)."
            ),
            limitations=(
                "Borough-level granularity only.",
                "Includes all use classes, not office-specific.",
                "Test fixture, not live data.",
            ),
            now=RETRIEVED_AT,
        )
    store.finish_run(run, status="succeeded", promote=True, now=RETRIEVED_AT)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a canonical PLD planning-activity fixture")
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args()
    seed_pld_activity(args.data_dir)


if __name__ == "__main__":
    main()
