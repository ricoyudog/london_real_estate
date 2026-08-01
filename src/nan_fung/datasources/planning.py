"""Planning London Datahub functions for office-supply research."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .common import SourceResult, get_json, post_json, source_result
from nan_fung.ingestion.policies import SourcePolicy

PLD_BASE_URL = "https://planningdata.london.gov.uk/api-guest/applications"
PLD_HEADERS = {"X-API-AllowRequest": "be2rmRnt&"}
_PLD_POLICY = SourcePolicy(
    ("planningdata.london.gov.uk",),
    allowed_methods=("GET", "POST"),
    allowed_request_headers=("user-agent", "content-type", "x-api-allowrequest"),
)


def fetch_planning_application(application_id: str) -> SourceResult:
    """Fetch one public PLD planning-application record by its normalized id."""

    source_url = f"{PLD_BASE_URL}/_source/{quote(application_id, safe='-_')}"
    record = get_json(source_url, headers=PLD_HEADERS, policy=_PLD_POLICY)
    return source_result(
        category="supply_pipeline",
        source="Planning London Datahub",
        source_url=source_url,
        source_updated_at=record.get("last_updated"),
        records=[record],
    )


def search_planning_applications(
    query: dict[str, Any],
    *,
    source_fields: list[str] | None = None,
    size: int = 10,
) -> SourceResult:
    """Run an Elasticsearch query against the public PLD search endpoint."""

    body: dict[str, Any] = {"query": query, "size": size}
    if source_fields is not None:
        body["_source"] = source_fields
    source_url = f"{PLD_BASE_URL}/_search"
    payload = post_json(source_url, body, headers=PLD_HEADERS, policy=_PLD_POLICY)
    records = []
    for hit in payload.get("hits", {}).get("hits", []):
        record = dict(hit.get("_source", hit))
        if hit.get("_id"):
            record["search_id"] = hit["_id"]
        records.append(record)
    source_updated = max(
        (record["last_updated"] for record in records if record.get("last_updated")),
        default=None,
    )
    return source_result(
        category="supply_pipeline",
        source="Planning London Datahub",
        source_url=source_url,
        source_updated_at=source_updated,
        records=records,
    )
