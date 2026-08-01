import json

import pytest

from nan_fung.datasources import news


def test_search_market_news_builds_agent_ready_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_get_bytes(url: str, params: dict[str, object], **_kwargs: object) -> bytes:
        seen.update({"url": url, "params": params})
        return json.dumps(
            {
                "results": [
                    {
                        "title": "Office policy update",
                        "description": "A policy update.",
                        "link": "/government/news/office-policy-update",
                        "public_timestamp": "2026-07-30T09:30:00Z",
                        "format": "news",
                        "organisations": [{"title": "Example department"}],
                    }
                ]
            }
        ).encode()

    monkeypatch.setattr(news, "get_bytes", fake_get_bytes)

    result = news.search_market_news("London office", count=1)

    assert seen["url"] == news.GOV_UK_SEARCH_API_URL
    assert seen["params"]["order"] == "-public_timestamp"
    assert "q=London+office" in result["source_url"]
    assert result["records"][0]["public_timestamp"] == "2026-07-30T09:30:00Z"
    assert (
        result["records"][0]["url"]
        == "https://www.gov.uk/government/news/office-policy-update"
    )
    assert result["records"][0]["content_api_url"].startswith(
        news.GOV_UK_CONTENT_API_BASE
    )
    assert result["records"][0]["base_path"] == "/government/news/office-policy-update"


def test_parse_market_news_search_json_is_a_pure_artifact_parser() -> None:
    records = news.parse_market_news_search_json(
        b'''{
          "results": [{
            "title": "Office policy update",
            "link": "/government/news/office-policy-update",
            "public_timestamp": "2026-07-30T09:30:00Z",
            "organisations": [{"title": "Example department"}]
          }]
        }'''
    )

    assert records == [
        {
            "title": "Office policy update",
            "description": None,
            "public_timestamp": "2026-07-30T09:30:00Z",
            "format": None,
            "organisations": ["Example department"],
            "base_path": "/government/news/office-policy-update",
            "url": "https://www.gov.uk/government/news/office-policy-update",
            "content_api_url": (
                "https://www.gov.uk/api/content/government/news/office-policy-update"
            ),
        }
    ]


@pytest.mark.parametrize(
    ("count", "start"),
    [(-1, 0), (1_501, 0), (1, -1)],
)
def test_search_market_news_bounds_page_requests(count: int, start: int) -> None:
    with pytest.raises(ValueError):
        news.search_market_news("London office", count=count, start=start)


def test_fetch_content_item_returns_structured_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        news,
        "get_bytes",
        lambda _url, **_kwargs: json.dumps(
            {
                "title": "Office policy update",
                "description": "A policy update.",
                "base_path": "/government/news/office-policy-update",
                "document_type": "news_story",
                "schema_name": "news_article",
                "first_published_at": "2026-07-30T09:30:00Z",
                "public_updated_at": "2026-07-30T10:00:00Z",
                "details": {"body": "<p>Policy details.</p>"},
                "links": {"organisations": [{"title": "Example department"}]},
            }
        ).encode(),
    )

    result = news.fetch_content_item(
        "https://www.gov.uk/government/news/office-policy-update"
    )

    assert (
        result["source_url"]
        == "https://www.gov.uk/government/news/office-policy-update"
    )
    assert result["records"][0]["body_html"] == "<p>Policy details.</p>"
    assert result["records"][0]["organisations"] == ["Example department"]
    assert result["published_at"] == "2026-07-30T09:30:00Z"
    assert result["source_updated_at"] == "2026-07-30T10:00:00Z"


def test_parse_content_item_json_is_a_pure_artifact_parser() -> None:
    record = news.parse_content_item_json(
        b'''{
          "title": "Office policy update",
          "base_path": "/government/news/office-policy-update",
          "first_published_at": "2026-07-30T09:30:00Z",
          "public_updated_at": "2026-07-30T10:00:00Z",
          "details": {"body": "<p>Policy details.</p>"},
          "links": {"organisations": [{"title": "Example department"}]}
        }'''
    )

    assert record["base_path"] == "/government/news/office-policy-update"
    assert record["content_api_url"].endswith("/government/news/office-policy-update")
    assert record["public_updated_at"] == "2026-07-30T10:00:00Z"


@pytest.mark.network
@pytest.mark.restricted_live_probe
def test_search_market_news_live() -> None:
    result = news.search_market_news("energy performance buildings", count=2)

    assert result["records"]
    assert len(result["records"]) <= 2
    assert all(
        record["url"].startswith("https://www.gov.uk/") for record in result["records"]
    )
    assert all(record["title"] for record in result["records"])


@pytest.mark.network
@pytest.mark.restricted_live_probe
def test_fetch_content_item_live() -> None:
    result = news.fetch_content_item(
        "/government/statistical-data-sets/"
        "live-tables-on-energy-performance-of-buildings-certificates"
    )

    record = result["records"][0]
    assert (
        record["title"] == "Live tables on Energy Performance of Buildings Certificates"
    )
    assert record["base_path"].startswith("/government/statistical-data-sets/")
    assert record["public_updated_at"]
