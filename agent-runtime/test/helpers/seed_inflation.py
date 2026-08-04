"""Test-only canonical inflation seeder; not shipped runtime logic."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from nan_fung.ingestion.official_macro import (
    ons_record_key,
    ons_record_metadata,
    parse_ons_series_json,
)
from nan_fung.operational import OperationalStore


RETRIEVED_AT = datetime(2026, 8, 1, 12, tzinfo=UTC)
ONS_URL = "https://api.beta.ons.gov.uk/v1/data"
_SERIES = ("D7G7", "L55O", "CZBH")


def seed_inflation(data_dir: Path) -> None:
    """Seed one canonical ONS inflation observation per approved series."""

    store = OperationalStore(data_dir)
    store.sync_registry(now=RETRIEVED_AT)
    for series in _SERIES:
        evidence_bytes = json.dumps(
            {
                "description": {
                    "title": f"ONS inflation series {series}",
                    "unit": "percent",
                    "releaseDate": "2026-07-16T07:00:00Z",
                    "monthLabelStyle": "monthly",
                },
                "months": [{"label": "2026 JUN", "value": "3.4"}],
            }
        ).encode()
        record = parse_ons_series_json(
            evidence_bytes,
            series=series,
            uri=f"/economy/inflationandpriceindices/timeseries/{series.lower()}/mm23",
            frequency="months",
            source_url=ONS_URL,
        )[0]
        metadata = ons_record_metadata(record)
        queued = store.enqueue(
            metadata["datasource_id"],
            scheduled_for=RETRIEVED_AT,
            request={"fixture": "agent-runtime-inflation"},
            trigger="manual",
        )
        claim = store.claim_job(queued.job_id, "fixture-worker", now=RETRIEVED_AT)
        if claim is None:
            raise RuntimeError("fixture job could not be claimed")
        run = store.start_run(claim, "fixture-worker", now=RETRIEVED_AT)
        evidence = store.persist_evidence(
            run,
            evidence_bytes,
            media_type="application/json",
            request={"method": "GET", "url": ONS_URL},
            response={"status": 200, "final_url": ONS_URL},
            source_id="ons.data_api",
            retrieved_at=RETRIEVED_AT,
            now=RETRIEVED_AT,
        )
        store.persist_observation(
            run,
            record_key=ons_record_key(record),
            payload=record,
            record_type=metadata["record_type"],
            category="macro",
            evidence=(evidence,),
            source_date=metadata["source_date"],
            period_label=metadata["period_label"],
            unit=metadata["unit"],
            definition_text=metadata["definition"],
            limitations=metadata["limitations"],
            locator=metadata["locator"],
            now=RETRIEVED_AT,
        )
        store.finish_run(run, status="succeeded", promote=True, now=RETRIEVED_AT)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed canonical ONS inflation fixtures")
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args()
    seed_inflation(args.data_dir)


if __name__ == "__main__":
    main()
