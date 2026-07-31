import json

import pytest

from nan_fung.datasources import planning


def test_fetch_planning_application_wraps_record(monkeypatch) -> None:
    monkeypatch.setattr(
        planning,
        "get_json",
        lambda url, headers: {"id": "Newham-701491", "last_updated": "2025-11-13"},
    )

    result = planning.fetch_planning_application("Newham-701491")

    assert result["published_at"] is None
    assert result["source_updated_at"] == "2025-11-13"
    assert result["records"][0]["id"] == "Newham-701491"


def test_search_planning_applications_sends_query(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(
                {"hits": {"hits": [{"_source": {"id": "Lambeth-1"}}]}}
            ).encode()

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr(planning, "urlopen", fake_urlopen)

    result = planning.search_planning_applications({"match": {"description": "office"}})

    assert captured["body"]["query"] == {"match": {"description": "office"}}
    assert result["records"] == [{"id": "Lambeth-1"}]


@pytest.mark.live
def test_live_planning_application() -> None:
    result = planning.fetch_planning_application("Newham-701491")

    assert result["records"][0]["id"] == "Newham-701491"
    assert result["records"][0]["borough"] == "Newham"


@pytest.mark.live
def test_live_search_planning_applications() -> None:
    result = planning.search_planning_applications(
        {"match_phrase": {"description": "office"}},
        source_fields=["id", "borough", "description", "last_updated"],
        size=1,
    )

    assert len(result["records"]) == 1
    assert "office" in result["records"][0]["description"].lower()
    assert result["records"][0]["id"]
