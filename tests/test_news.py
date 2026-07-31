import pytest

from nan_fung.datasources import news


def test_search_market_news_builds_agent_ready_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_get_json(url: str, params: dict[str, object]) -> dict[str, object]:
        seen.update({"url": url, "params": params})
        return {
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

    monkeypatch.setattr(news, "get_json", fake_get_json)

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


def test_fetch_content_item_returns_structured_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        news,
        "get_json",
        lambda _url: {
            "title": "Office policy update",
            "description": "A policy update.",
            "base_path": "/government/news/office-policy-update",
            "document_type": "news_story",
            "schema_name": "news_article",
            "first_published_at": "2026-07-30T09:30:00Z",
            "public_updated_at": "2026-07-30T10:00:00Z",
            "details": {"body": "<p>Policy details.</p>"},
            "links": {"organisations": [{"title": "Example department"}]},
        },
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


@pytest.mark.live
def test_search_market_news_live() -> None:
    result = news.search_market_news("energy performance buildings", count=2)

    assert result["records"]
    assert len(result["records"]) <= 2
    assert all(
        record["url"].startswith("https://www.gov.uk/") for record in result["records"]
    )
    assert all(record["title"] for record in result["records"])


@pytest.mark.live
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
