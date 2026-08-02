"""Test-only canonical Bank Rate seeder; this is not shipped runtime logic.

Run from the repository root:
    uv run python agent-runtime/test/helpers/seed_bank_rate.py <data_dir> <bank_rate_decimal>
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from nan_fung.operational import OperationalStore


RETRIEVED_AT = datetime(2026, 8, 1, 12, tzinfo=UTC)
STALE_RETRIEVED_AT = datetime(2026, 1, 1, 12, tzinfo=UTC)
BANK_RATE_URL = "https://www.bankofengland.co.uk/boeapps/database/offline.csv"


def seed_bank_rate(
    data_dir: Path, value: str, *, published_null: bool = False, stale: bool = False
) -> None:
    retrieved_at = STALE_RETRIEVED_AT if stale else RETRIEVED_AT
    store = OperationalStore(data_dir)
    store.sync_registry(now=retrieved_at)
    queued = store.enqueue(
        "boe.bank_rate.iudbedr",
        scheduled_for=retrieved_at,
        request={"fixture": "pi-agent-runtime-phase-2"},
    )
    claim = store.claim_job(queued.job_id, "fixture-worker", now=retrieved_at)
    if claim is None:
        raise RuntimeError("fixture job could not be claimed")
    run = store.start_run(claim, "fixture-worker", now=retrieved_at)
    response: dict[str, int | str] = {
        "status": 200,
        "final_url": BANK_RATE_URL,
        "title": "Bank Rate history and data",
        "source_updated_at": "2026-08-01T10:00:00Z",
    }
    if not published_null:
        response["published_at"] = "2026-08-01T09:00:00Z"
    evidence = store.persist_evidence(
        run,
        f"DATE,IUDBEDR\n31 Jul 2026,{value}\n".encode(),
        media_type="text/csv",
        request={"method": "GET", "url": BANK_RATE_URL, "series": "IUDBEDR"},
        response=response,
        retrieved_at=retrieved_at,
        now=retrieved_at,
    )
    store.persist_observation(
        run,
        record_key=("IUDBEDR", "2026-07-31"),
        payload={"date": "2026-07-31", "bank_rate_percent": value},
        record_type="metric",
        category="interest-rates-monetary-policy",
        evidence=(evidence,),
        locator={"kind": "csv_row", "row_key": "2026-07-31"},
        source_date="2026-07-31",
        unit="percent",
        definition_text="Official Bank of England Bank Rate series IUDBEDR",
        limitations=("Current-vintage official series",),
        now=retrieved_at,
    )
    store.finish_run(run, status="succeeded", promote=True, now=retrieved_at)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a canonical Bank Rate fixture store")
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("bank_rate_decimal")
    parser.add_argument("--published-null", action="store_true")
    parser.add_argument("--stale", action="store_true")
    args = parser.parse_args()
    seed_bank_rate(
        args.data_dir,
        args.bank_rate_decimal,
        published_null=args.published_null,
        stale=args.stale,
    )


if __name__ == "__main__":
    main()
