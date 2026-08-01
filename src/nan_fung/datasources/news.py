"""Free GOV.UK market-news and policy discovery functions."""

from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import urlencode, urlparse

from nan_fung.datasources.common import SourceResult, get_bytes, source_result
from nan_fung.ingestion.policies import SourcePolicy

GOV_UK_SEARCH_API_URL = "https://www.gov.uk/api/search.json"
GOV_UK_CONTENT_API_BASE = "https://www.gov.uk/api/content"
MAX_SEARCH_PAGE_SIZE = 1_500

_GOVUK_SEARCH_POLICY = SourcePolicy(
    ("www.gov.uk",),
    allowed_query_keys=("q", "count", "order", "fields", "start"),
)
_GOVUK_CONTENT_POLICY = SourcePolicy(("www.gov.uk",))


def search_market_news(
    query: str,
    count: int = 10,
    *,
    start: int = 0,
) -> SourceResult:
    """Search one bounded GOV.UK result page, newest first.

    ``start`` is an explicit result offset so a scheduler can persist a
    deterministic pagination watermark instead of relying on an unbounded
    search response.  Search records remain discovery candidates; their
    ``public_timestamp`` is intentionally retained as the change signal.
    """

    _validate_search_page(query, count, start)

    params = {
        "q": query,
        "count": count,
        "order": "-public_timestamp",
        "fields": "title,description,link,public_timestamp,format,organisations",
    }
    if start:
        params["start"] = start
    records = parse_market_news_search_json(
        get_bytes(GOV_UK_SEARCH_API_URL, params=params, policy=_GOVUK_SEARCH_POLICY)
    )

    return source_result(
        category="market_news_events",
        source="GOV.UK Search API",
        source_url=f"{GOV_UK_SEARCH_API_URL}?{urlencode(params)}",
        records=records,
    )


def fetch_content_item(path_or_url: str) -> SourceResult:
    """Fetch one GOV.UK page as structured Content API data."""

    path = _normalise_govuk_path(path_or_url)
    content_api_url = f"{GOV_UK_CONTENT_API_BASE}{path}"
    record = parse_content_item_json(
        get_bytes(content_api_url, policy=_GOVUK_CONTENT_POLICY), fallback_path=path
    )

    return source_result(
        category="market_news_events",
        source="GOV.UK Content API",
        source_url=f"https://www.gov.uk{record['base_path']}",
        published_at=record["first_published_at"],
        source_updated_at=record["public_updated_at"],
        records=[record],
    )


def parse_market_news_search_json(evidence: bytes) -> list[dict[str, Any]]:
    """Parse captured GOV.UK Search API JSON without network access."""

    payload = _json_object(evidence)
    items = payload.get("results", [])
    if not isinstance(items, list):
        raise ValueError("GOV.UK Search API results must be a list")

    records: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("GOV.UK Search API result must be an object")
        path = _normalise_govuk_path(item.get("link"))
        title = item.get("title")
        if not isinstance(title, str) or not title:
            raise ValueError("GOV.UK Search API result has no title")
        records.append(
            {
                "title": title,
                "description": item.get("description"),
                "public_timestamp": item.get("public_timestamp"),
                "format": item.get("format"),
                "organisations": _organisation_titles(item.get("organisations")),
                "base_path": path,
                "url": f"https://www.gov.uk{path}",
                "content_api_url": f"{GOV_UK_CONTENT_API_BASE}{path}",
            }
        )
    return records


def parse_content_item_json(
    evidence: bytes,
    *,
    fallback_path: str | None = None,
) -> dict[str, Any]:
    """Parse captured GOV.UK Content API JSON without network access."""

    item = _json_object(evidence)
    title = item.get("title")
    if not isinstance(title, str) or not title:
        raise ValueError("GOV.UK Content API item has no title")
    base_path = _normalise_govuk_path(item.get("base_path") or fallback_path)
    details = item.get("details")
    links = item.get("links")
    return {
        "title": title,
        "description": item.get("description"),
        "base_path": base_path,
        "document_type": item.get("document_type"),
        "schema_name": item.get("schema_name"),
        "first_published_at": item.get("first_published_at"),
        "public_updated_at": item.get("public_updated_at"),
        "organisations": _organisation_titles(
            links.get("organisations") if isinstance(links, Mapping) else None
        ),
        "body_html": details.get("body") if isinstance(details, Mapping) else None,
        "content_api_url": f"{GOV_UK_CONTENT_API_BASE}{base_path}",
    }


def _validate_search_page(query: str, count: int, start: int) -> None:
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 0 <= count <= MAX_SEARCH_PAGE_SIZE
    ):
        raise ValueError(f"count must be between 0 and {MAX_SEARCH_PAGE_SIZE}")
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise ValueError("start must be a non-negative integer")


def _json_object(evidence: bytes) -> dict[str, Any]:
    value = json.loads(evidence)
    if not isinstance(value, dict):
        raise ValueError("GOV.UK evidence must contain a JSON object")
    return value


def _normalise_govuk_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("GOV.UK item has no path")
    parsed = urlparse(value)
    path = parsed.path if parsed.scheme or parsed.netloc else value
    if not path:
        raise ValueError("GOV.UK item has no path")
    return path if path.startswith("/") else f"/{path}"


def _organisation_titles(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        title
        for item in value
        if isinstance(item, Mapping)
        and isinstance(title := item.get("title"), str)
        and title
    ]
