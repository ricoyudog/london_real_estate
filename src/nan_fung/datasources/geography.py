"""Official geography proxies for London office-market locations."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlencode

from .common import SourceResult, get_json, source_result

ONSPD_LAYER_URL = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "Online_ONS_Postcode_Directory_Live/FeatureServer/1"
)
ONSPD_QUERY_URL = f"{ONSPD_LAYER_URL}/query"
GLA_TOWN_CENTRE_QUERY_URL = (
    "https://gis.london.gov.uk/arcgis/rest/services/apps/"
    "planning_data_map_02/FeatureServer/104/query"
)


def lookup_postcode(postcode: str) -> SourceResult:
    """Map a live UK postcode to current ONS geography codes and coordinates."""

    compact = "".join(postcode.upper().split())
    normalized = f"{compact[:-3]} {compact[-3:]}"
    metadata = get_json(ONSPD_LAYER_URL, params={"f": "json"})
    params = {
        "where": f"PCDS='{normalized.replace(chr(39), chr(39) * 2)}'",
        "outFields": "PCDS,LAD25CD,WD25CD,OA21CD,LSOA21CD,MSOA21CD,LAT,LONG",
        "returnGeometry": "true",
        "outSR": 4326,
        "f": "json",
    }
    payload = get_json(ONSPD_QUERY_URL, params=params)
    records = [_flatten_feature(feature) for feature in payload.get("features", [])]
    _add_spatial_reference(records, payload)
    return source_result(
        category="postcode_geography",
        source="Office for National Statistics Online Postcode Directory",
        source_url=f"{ONSPD_QUERY_URL}?{urlencode(params)}",
        published_at=None,
        source_updated_at=_arcgis_update_time(metadata),
        records=records,
    )


def query_town_centres(
    name: str | None = None,
    *,
    include_geometry: bool = False,
    limit: int = 50,
) -> SourceResult:
    """Query free GLA town-centre polygons, optionally filtering by site name."""

    where = "1=1"
    if name:
        escaped = name.upper().replace("'", "''")
        where = f"UPPER(sitename) LIKE '%{escaped}%'"
    params = {
        "where": where,
        "outFields": (
            "sitename,borough,planningauthority,designation,classification,"
            "lastupdateddate,source,notes"
        ),
        "returnGeometry": str(include_geometry).lower(),
        "outSR": 4326,
        "resultRecordCount": limit,
        "f": "json",
    }
    payload = get_json(GLA_TOWN_CENTRE_QUERY_URL, params=params)
    records = [_flatten_feature(feature) for feature in payload.get("features", [])]
    _add_spatial_reference(records, payload)
    return source_result(
        category="town_centre_geography",
        source="Greater London Authority Town Centre Boundaries",
        source_url=f"{GLA_TOWN_CENTRE_QUERY_URL}?{urlencode(params)}",
        published_at=None,
        records=records,
    )


def _flatten_feature(feature: dict[str, object]) -> dict[str, object]:
    """Flatten an ArcGIS feature while preserving optional geometry."""

    record = dict(feature.get("attributes", {}))
    if "geometry" in feature:
        record["geometry"] = feature["geometry"]
    return record


def _add_spatial_reference(
    records: list[dict[str, object]], payload: dict[str, object]
) -> None:
    spatial_reference = payload.get("spatialReference")
    if spatial_reference:
        for record in records:
            record["spatial_reference"] = spatial_reference


def _arcgis_update_time(metadata: dict[str, object]) -> str | None:
    editing_info = metadata.get("editingInfo")
    if not isinstance(editing_info, dict):
        return None
    milliseconds = editing_info.get("dataLastEditDate")
    if not isinstance(milliseconds, (int, float)):
        return None
    return datetime.fromtimestamp(milliseconds / 1000, UTC).isoformat()
