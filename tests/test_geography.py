import pytest

from nan_fung.datasources import geography


def test_lookup_postcode_normalizes_and_flattens(monkeypatch) -> None:
    captured = {}

    def fake_get_json(url, params):
        captured.update(params)
        if url == geography.ONSPD_LAYER_URL:
            return {"editingInfo": {"dataLastEditDate": 1_781_277_038_010}}
        return {
            "features": [{"attributes": {"PCDS": "EC2Y 5AS"}, "geometry": {"x": -0.09}}]
        }

    monkeypatch.setattr(geography, "get_json", fake_get_json)

    result = geography.lookup_postcode("ec2y5as")

    assert captured["where"] == "PCDS='EC2Y 5AS'"
    assert result["source_updated_at"] == "2026-06-12T15:10:38.010000+00:00"
    assert result["records"] == [{"PCDS": "EC2Y 5AS", "geometry": {"x": -0.09}}]


def test_query_town_centres_filters_name(monkeypatch) -> None:
    captured = {}

    def fake_get_json(url, params):
        captured.update(params)
        return {"features": [{"attributes": {"sitename": "Canary Wharf"}}]}

    monkeypatch.setattr(geography, "get_json", fake_get_json)

    result = geography.query_town_centres("Canary Wharf")

    assert "CANARY WHARF" in captured["where"]
    assert result["records"] == [{"sitename": "Canary Wharf"}]


@pytest.mark.live
def test_live_ons_postcode() -> None:
    result = geography.lookup_postcode("EC2Y 5AS")

    assert result["records"][0]["LAD25CD"] == "E09000001"
    assert result["records"][0]["spatial_reference"]["wkid"] == 4326
    assert result["source_updated_at"]


@pytest.mark.live
def test_live_gla_town_centre() -> None:
    result = geography.query_town_centres("Canary Wharf", include_geometry=True)

    assert result["records"][0]["borough"] == "Tower Hamlets"
    assert result["records"][0]["planningauthority"] == "Tower Hamlets"
    assert "GLA has not designated" in result["records"][0]["notes"]
    assert result["records"][0]["spatial_reference"]["wkid"] == 4326
