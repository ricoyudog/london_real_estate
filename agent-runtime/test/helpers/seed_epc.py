"""Test-only canonical EPC seeder; not shipped runtime logic."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from odf.opendocument import OpenDocumentSpreadsheet
from odf.table import Table, TableCell, TableRow
from odf.text import P

from nan_fung.ingestion.file_release_workflow import record_metadata_for
from nan_fung.operational import OperationalStore


RETRIEVED_AT = datetime(2026, 8, 1, 12, tzinfo=UTC)
DATASOURCE_ID = "mhclg.epc.live_table_a_london"
SOURCE_URL = "https://assets.publishing.service.gov.uk/media/table-a.ods"
RECORD = {"region": "London", "quarter": "2026/2", "number_lodgements": "3630", "total_floor_area_m2": "3102511", "source_row": 5, "indicator_type": "proxy", "scope": "all non-domestic properties, not offices only"}


def _evidence_bytes() -> bytes:
    document = OpenDocumentSpreadsheet()
    table = Table(name="A_by_Region")
    for values in (
        ["A- Non-Domestic Properties by Region by Energy Performance Asset Rating"],
        ["This worksheet contains one table."],
        ["Source: Energy Performance Certificates for Buildings Register"],
        ["Region", "Quarter", "Number Lodgements", "Total Floor Area (m2)", "A+", "A", "B", "C", "D", "E", "F", "G", "Not Recorded"],
        ["London", "2026/2", 3630, 3102511, 13, 482, 1621, 1019, 358, 119, 13, 5, 0],
    ):
        row = TableRow()
        for value in values:
            cell = TableCell(valuetype="float", value=value) if isinstance(value, int) else TableCell(valuetype="string")
            if isinstance(value, str):
                cell.addElement(P(text=value))
            row.addElement(cell)
        table.addElement(row)
    document.spreadsheet.addElement(table)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def seed_epc(data_dir: Path) -> None:
    """Seed the London MHCLG non-domestic EPC lodgements proxy."""

    store = OperationalStore(data_dir)
    store.sync_registry(now=RETRIEVED_AT)
    metadata = record_metadata_for(DATASOURCE_ID, RECORD)
    queued = store.enqueue(DATASOURCE_ID, scheduled_for=RETRIEVED_AT, request={"fixture": "agent-runtime-epc"}, trigger="manual")
    claim = store.claim_job(queued.job_id, "fixture-worker", now=RETRIEVED_AT)
    if claim is None:
        raise RuntimeError("fixture job could not be claimed")
    run = store.start_run(claim, "fixture-worker", now=RETRIEVED_AT)
    evidence = store.persist_evidence(run, _evidence_bytes(), media_type="application/vnd.oasis.opendocument.spreadsheet", request={"method": "GET", "url": SOURCE_URL}, response={"status": 200, "final_url": SOURCE_URL}, source_id="mhclg.epc_attachment", retrieved_at=RETRIEVED_AT, now=RETRIEVED_AT)
    store.persist_observation(run, record_key=metadata["record_key"], payload=RECORD, record_type=metadata["record_type"], category="esg_energy_efficiency", evidence=(evidence,), source_date=metadata["source_date"], period_label=metadata["period_label"], unit="certificates", definition_text=metadata["definition"], limitations=metadata["limitations"], locator=metadata["locator"], now=RETRIEVED_AT)
    store.finish_run(run, status="succeeded", promote=True, now=RETRIEVED_AT)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a canonical London EPC fixture")
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args()
    seed_epc(args.data_dir)


if __name__ == "__main__":
    main()
