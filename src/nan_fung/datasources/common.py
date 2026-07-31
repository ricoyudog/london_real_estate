"""Shared HTTP and result helpers for public datasources."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, TypedDict
from urllib.parse import urlencode
from urllib.request import Request, urlopen

USER_AGENT = "nan-fung-datasource-research/0.1"


class SourceResult(TypedDict):
    """JSON-serializable result returned by every datasource function."""

    category: str
    source: str
    source_url: str
    retrieved_at: str
    published_at: str | None
    source_updated_at: str | None
    records: list[dict[str, Any]]


def get_bytes(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> bytes:
    """Return bytes from a public HTTP endpoint."""

    if params:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urlencode(params, doseq=True)}"
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Return a decoded JSON object from a public HTTP endpoint."""

    return json.loads(get_bytes(url, params, headers, timeout))


def source_result(
    *,
    category: str,
    source: str,
    source_url: str,
    records: list[dict[str, Any]],
    published_at: str | None = None,
    source_updated_at: str | None = None,
) -> SourceResult:
    """Build the common JSON result envelope."""

    return {
        "category": category,
        "source": source,
        "source_url": source_url,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "published_at": published_at,
        "source_updated_at": source_updated_at,
        "records": records,
    }
