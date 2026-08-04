"""Test-only canonical ONS GDP seeder; this is not shipped runtime logic.

Run from the repository root:
    uv run python agent-runtime/test/helpers/seed_gdp.py <data_dir>
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from nan_fung.ingestion.official_macro import parse_ons_series_json
from nan_fung.operational import OperationalStore


RETRIEVED_AT = datetime(2026, 8, 1, 12, tzinfo=UTC)
ONS_BASE_URL = "https://api.beta.ons.gov.uk/v1/data"
FIXTURES = (
    (
        "ECYX",
        "quarters",
        "/economy/grossdomesticproductgdp/timeseries/ecyx/mgdp",
        {
            "description": {
                "title": "Monthly gross domestic product: Index",
                "unit": "%",
                "releaseDate": "2026-07-21T23:00:00.000Z",
                "monthLabelStyle": "three month average",
            },
            "quarters": [
                {
                    "label": "2026 Q2",
                    "value": "0.20",
                    "updateDate": "2026-07-21T23:00:00.000Z",
                }
            ],
        },
    ),
    (
        "IHYQ",
        "quarters",
        "/economy/grossdomesticproductgdp/timeseries/ihyq/qna",
        {
            "description": {
                "title": "Quarterly gross domestic product",
                "unit": "%",
                "releaseDate": "2026-07-21T23:00:00.000Z",
                "quarterLabelStyle": "quarterly",
            },
            "quarters": [
                {
                    "label": "2026 Q1",
                    "value": "0.70",
                    "updateDate": "2026-07-21T23:00:00.000Z",
                }
            ],
        },
    ),
)


def seed_gdp(data_dir: Path) -> None:
    store = OperationalStore(data_dir)
    store.sync_registry(now=RETRIEVED_AT)
    for series, frequency, uri, payload in FIXTURES:
        datasource_id = f"ons.gdp.{series.lower()}"
        source_url = f"{ONS_BASE_URL}?uri={series.lower()}"
        evidence_bytes = json.dumps(payload, separators=(",", ":")).encode()
        record = parse_ons_series_json(
            evidence_bytes,
            series=series,
            uri=uri,
            frequency=frequency,
            source_url=source_url,
        )[0]
        queued = store.enqueue(
            datasource_id,
            scheduled_for=RETRIEVED_AT,
            request={"fixture": "pi-agent-runtime-gdp", "series": series},
        )
        claim = store.claim_job(queued.job_id, "fixture-worker", now=RETRIEVED_AT)
        if claim is None:
            raise RuntimeError("fixture job could not be claimed")
        run = store.start_run(claim, "fixture-worker", now=RETRIEVED_AT)
        evidence = store.persist_evidence(
            run,
            evidence_bytes,
            media_type="application/json",
            request={"method": "GET", "url": source_url},
            response={
                "status": 200,
                "final_url": source_url,
                "published_at": record["release_date"],
                "source_updated_at": record["updated_at"],
                "title": record["title"],
            },
            retrieved_at=RETRIEVED_AT,
            now=RETRIEVED_AT,
        )
        store.persist_observation(
            run,
            record_key=(series, record["period"]),
            payload=record,
            record_type="metric",
            category="macro",
            evidence=(evidence,),
            locator=record["locator"],
            source_date=record["release_date"][:10],
            unit=record["unit"],
            definition_text=record["title"],
            limitations=("Test fixture, not live data.",),
            now=RETRIEVED_AT,
        )
        store.finish_run(run, status="succeeded", promote=True, now=RETRIEVED_AT)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a canonical ONS GDP fixture store")
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args()
    seed_gdp(args.data_dir)


if __name__ == "__main__":
    main()
