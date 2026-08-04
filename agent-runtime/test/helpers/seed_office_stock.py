"""Test-only canonical VOA office-stock seeder; not shipped runtime logic."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from nan_fung.ingestion.file_release_workflow import record_metadata_for
from nan_fung.operational import OperationalStore


RETRIEVED_AT = datetime(2026, 8, 1, 12, tzinfo=UTC)
DATASOURCE_ID = "voa.ndr_office_stock"
SOURCE_URL = "https://assets.publishing.service.gov.uk/media/ndr_stock_of_properties_2026.zip"
RECORD = {"area_code": "E12000007", "area_name": "London", "year": 2026, "office_property_count": "103400"}


def _evidence_bytes() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("table_SOP5_1.csv", "geography,area_code,area_name,2026\nREGL,E12000007,London,103400\n")
        archive.writestr("table_SOP5_2.csv", "geography,area_code,area_name,2026\nREGL,E12000007,London,9264908\n")
    return output.getvalue()


def seed_office_stock(data_dir: Path) -> None:
    """Seed the annual London VOA office-property count."""

    store = OperationalStore(data_dir)
    store.sync_registry(now=RETRIEVED_AT)
    metadata = record_metadata_for(DATASOURCE_ID, RECORD)
    queued = store.enqueue(DATASOURCE_ID, scheduled_for=RETRIEVED_AT, request={"fixture": "agent-runtime-office-stock"}, trigger="manual")
    claim = store.claim_job(queued.job_id, "fixture-worker", now=RETRIEVED_AT)
    if claim is None:
        raise RuntimeError("fixture job could not be claimed")
    run = store.start_run(claim, "fixture-worker", now=RETRIEVED_AT)
    evidence = store.persist_evidence(run, _evidence_bytes(), media_type="application/zip", request={"method": "GET", "url": SOURCE_URL}, response={"status": 200, "final_url": SOURCE_URL}, source_id="voa.ndr_stock", retrieved_at=RETRIEVED_AT, now=RETRIEVED_AT)
    store.persist_observation(run, record_key=metadata["record_key"], payload=RECORD, record_type="supply", category="office_stock", evidence=(evidence,), source_date=metadata["source_date"], period_label=metadata["period_label"], unit="properties", definition_text=metadata["definition"], limitations=metadata["limitations"], locator=metadata["locator"], now=RETRIEVED_AT)
    store.finish_run(run, status="succeeded", promote=True, now=RETRIEVED_AT)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a canonical VOA office-stock fixture")
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args()
    seed_office_stock(args.data_dir)


if __name__ == "__main__":
    main()
