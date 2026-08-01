import json

import pytest

from nan_fung.datasources import geography


def test_lookup_postcode_normalizes_and_flattens(monkeypatch) -> None:
    captured = {}

    def fake_get_bytes(url, params, **_kwargs):
        captured.update(params)
        if url == geography.ONSPD_LAYER_URL:
            return b'{"editingInfo": {"dataLastEditDate": 1781277038010}}'
        return (
            b'{"features": [{"attributes": {"PCDS": "EC2Y 5AS"}, '
            b'"geometry": {"x": -0.09}}]}'
        )

    monkeypatch.setattr(geography, "get_bytes", fake_get_bytes)

    result = geography.lookup_postcode("ec2y5as")

    assert captured["where"] == "PCDS='EC2Y 5AS'"
    assert result["source_updated_at"] == "2026-06-12T15:10:38.010000+00:00"
    assert result["records"] == [{"PCDS": "EC2Y 5AS", "geometry": {"x": -0.09}}]


def test_query_town_centres_filters_name(monkeypatch) -> None:
    captured = {}

    def fake_get_bytes(url, params, **_kwargs):
        captured.update(params)
        if url == geography.GLA_TOWN_CENTRE_LAYER_URL:
            return b'{"objectIdField": "OBJECTID"}'
        return b'{"features": [{"attributes": {"sitename": "Canary Wharf", "OBJECTID": 7}}]}'

    monkeypatch.setattr(geography, "get_bytes", fake_get_bytes)

    result = geography.query_town_centres("Canary Wharf", limit=5, offset=10)

    assert "CANARY WHARF" in captured["where"]
    assert captured["resultRecordCount"] == 5
    assert captured["resultOffset"] == 10
    assert "OBJECTID" in captured["outFields"]
    assert result["records"] == [
        {"sitename": "Canary Wharf", "OBJECTID": 7, "source_feature_id": 7}
    ]


def test_arcgis_json_parsers_are_pure_artifact_parsers() -> None:
    metadata = geography.parse_arcgis_layer_metadata_json(
        b'{"objectIdField": "OBJECTID", "editingInfo": {"dataLastEditDate": 1781277038010}}'
    )
    records = geography.parse_arcgis_feature_page_json(
        json.dumps(
            {
                "objectIdFieldName": "OBJECTID",
                "spatialReference": {"wkid": 4326},
                "features": [
                    {"attributes": {"OBJECTID": 7, "sitename": "Canary Wharf"}}
                ],
            }
        ).encode()
    )

    assert metadata["objectIdField"] == "OBJECTID"
    assert geography.arcgis_object_id_field(metadata) == "OBJECTID"
    assert geography._arcgis_update_time(metadata) == "2026-06-12T15:10:38.010000+00:00"
    assert records == [
        {
            "OBJECTID": 7,
            "sitename": "Canary Wharf",
            "source_feature_id": 7,
            "spatial_reference": {"wkid": 4326},
        }
    ]


@pytest.mark.parametrize("limit", [0, 1001])
def test_query_town_centres_bounds_page_size(limit: int) -> None:
    with pytest.raises(ValueError):
        geography.query_town_centres(limit=limit)


@pytest.mark.network
@pytest.mark.legacy_live_probe
def test_live_ons_postcode() -> None:
    result = geography.lookup_postcode("EC2Y 5AS")

    assert result["records"][0]["LAD25CD"] == "E09000001"
    assert result["records"][0]["spatial_reference"]["wkid"] == 4326
    assert result["source_updated_at"]


@pytest.mark.network
@pytest.mark.restricted_live_probe
def test_live_gla_town_centre() -> None:
    result = geography.query_town_centres("Canary Wharf", include_geometry=True)

    assert result["records"][0]["borough"] == "Tower Hamlets"
    assert result["records"][0]["planningauthority"] == "Tower Hamlets"
    assert "GLA has not designated" in result["records"][0]["notes"]
    assert result["records"][0]["spatial_reference"]["wkid"] == 4326
