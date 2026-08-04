"""Test-only canonical London employment seeder; not shipped runtime logic."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from nan_fung.ingestion.official_macro import (
    nomis_record_key,
    nomis_record_metadata,
    parse_nomis_dataset_json,
)
from nan_fung.operational import OperationalStore


RETRIEVED_AT = datetime(2026, 8, 1, 12, tzinfo=UTC)
NOMIS_URL = "https://www.nomisweb.co.uk/api/v01/dataset"
_DATASETS = (("NM_59_1", "economic_activity", "Employment rate", "73.8"), ("NM_130_1", "item", "total workforce jobs", "6466474"))


def seed_employment(data_dir: Path) -> None:
    """Seed one canonical London observation per approved Nomis dataset."""

    store = OperationalStore(data_dir)
    store.sync_registry(now=RETRIEVED_AT)
    for dataset, dimension, metric, value in _DATASETS:
        evidence_bytes = json.dumps({"obs": [{"geography": {"description": "London", "geogcode": "E12000007"}, "time": {"description": "May 2026", "value": "2026-05"}, dimension: {"description": metric}, "obs_value": {"value": value}, "obs_status": {"description": "Normal Value"}}]}).encode()
        record = parse_nomis_dataset_json(evidence_bytes, dataset=dataset, source_url=NOMIS_URL)[0]
        metadata = nomis_record_metadata(record)
        queued = store.enqueue(metadata["datasource_id"], scheduled_for=RETRIEVED_AT, request={"fixture": "agent-runtime-employment"}, trigger="manual")
        claim = store.claim_job(queued.job_id, "fixture-worker", now=RETRIEVED_AT)
        if claim is None:
            raise RuntimeError("fixture job could not be claimed")
        run = store.start_run(claim, "fixture-worker", now=RETRIEVED_AT)
        evidence = store.persist_evidence(run, evidence_bytes, media_type="application/json", request={"method": "GET", "url": NOMIS_URL}, response={"status": 200, "final_url": NOMIS_URL}, source_id="nomis.api", retrieved_at=RETRIEVED_AT, now=RETRIEVED_AT)
        store.persist_observation(run, record_key=nomis_record_key(record), payload=record, record_type=metadata["record_type"], category="employment-market", evidence=(evidence,), source_date="2026-05-31", period_label=metadata["period_label"], unit=metadata["unit"], definition_text=metadata["definition"], limitations=metadata["limitations"], locator=metadata["locator"], now=RETRIEVED_AT)
        store.finish_run(run, status="succeeded", promote=True, now=RETRIEVED_AT)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed canonical Nomis London employment fixtures")
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args()
    seed_employment(args.data_dir)


if __name__ == "__main__":
    main()
