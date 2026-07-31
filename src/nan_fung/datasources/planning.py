"""Planning London Datahub functions for office-supply research."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from .common import USER_AGENT, SourceResult, get_json, source_result

PLD_BASE_URL = "https://planningdata.london.gov.uk/api-guest/applications"
PLD_HEADERS = {"X-API-AllowRequest": "be2rmRnt&"}


def fetch_planning_application(application_id: str) -> SourceResult:
    """Fetch one public PLD planning-application record by its normalized id."""

    source_url = f"{PLD_BASE_URL}/_source/{quote(application_id, safe='-_')}"
    record = get_json(source_url, headers=PLD_HEADERS)
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
    request = Request(
        source_url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            **PLD_HEADERS,
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        payload = json.load(response)

    records = [
        hit.get("_source", hit) for hit in payload.get("hits", {}).get("hits", [])
    ]
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
