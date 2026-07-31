"""Free GOV.UK market-news and policy discovery functions."""

from __future__ import annotations

from urllib.parse import urlencode, urlparse

from nan_fung.datasources.common import SourceResult, get_json, source_result

GOV_UK_SEARCH_API_URL = "https://www.gov.uk/api/search.json"
GOV_UK_CONTENT_API_BASE = "https://www.gov.uk/api/content"


def search_market_news(query: str, count: int = 10) -> SourceResult:
    """Search GOV.UK by public timestamp, newest first, without authentication."""

    params = {
        "q": query,
        "count": count,
        "order": "-public_timestamp",
        "fields": "title,description,link,public_timestamp,format,organisations",
    }
    payload = get_json(GOV_UK_SEARCH_API_URL, params=params)
    records = []
    for item in payload.get("results", []):
        path = item["link"]
        organisations = item.get("organisations") or []
        records.append(
            {
                "title": item["title"],
                "description": item.get("description"),
                "public_timestamp": item.get("public_timestamp"),
                "format": item.get("format"),
                "organisations": [
                    org.get("title") for org in organisations if org.get("title")
                ],
                "url": f"https://www.gov.uk{path}",
                "content_api_url": f"{GOV_UK_CONTENT_API_BASE}{path}",
            }
        )

    return source_result(
        category="market_news_events",
        source="GOV.UK Search API",
        source_url=f"{GOV_UK_SEARCH_API_URL}?{urlencode(params)}",
        records=records,
    )


def fetch_content_item(path_or_url: str) -> SourceResult:
    """Fetch one GOV.UK page as structured Content API data."""

    path = urlparse(path_or_url).path if "://" in path_or_url else path_or_url
    if not path.startswith("/"):
        path = f"/{path}"
    content_api_url = f"{GOV_UK_CONTENT_API_BASE}{path}"
    item = get_json(content_api_url)
    details = item.get("details") or {}
    organisations = (item.get("links") or {}).get("organisations") or []
    record = {
        "title": item["title"],
        "description": item.get("description"),
        "base_path": item.get("base_path", path),
        "document_type": item.get("document_type"),
        "schema_name": item.get("schema_name"),
        "first_published_at": item.get("first_published_at"),
        "public_updated_at": item.get("public_updated_at"),
        "organisations": [
            org.get("title") for org in organisations if org.get("title")
        ],
        "body_html": details.get("body"),
        "content_api_url": content_api_url,
    }

    return source_result(
        category="market_news_events",
        source="GOV.UK Content API",
        source_url=f"https://www.gov.uk{record['base_path']}",
        published_at=item.get("first_published_at"),
        source_updated_at=item.get("public_updated_at"),
        records=[record],
    )
