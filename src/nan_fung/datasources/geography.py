"""Official geography proxies for London office-market locations."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Mapping
from urllib.parse import urlencode

from .common import SourceResult, get_bytes, source_result
from nan_fung.ingestion.policies import ArtifactPolicy, SourcePolicy

ONSPD_LAYER_URL = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "Online_ONS_Postcode_Directory_Live/FeatureServer/1"
)
ONSPD_QUERY_URL = f"{ONSPD_LAYER_URL}/query"
GLA_TOWN_CENTRE_LAYER_URL = (
    "https://gis.london.gov.uk/arcgis/rest/services/apps/"
    "planning_data_map_02/FeatureServer/104"
)
GLA_TOWN_CENTRE_QUERY_URL = f"{GLA_TOWN_CENTRE_LAYER_URL}/query"
MAX_TOWN_CENTRE_PAGE_SIZE = 1_000
ONSPD_SOURCE_POLICY = SourcePolicy(
    ("services1.arcgis.com",),
    allowed_query_keys=("f", "where", "outFields", "returnGeometry", "outSR"),
    artifact=ArtifactPolicy(
        max_bytes=4 * 1024 * 1024,
        allowed_media_types=("application/json", "text/json"),
    ),
)
_GLA_POLICY = SourcePolicy(
    ("gis.london.gov.uk",),
    allowed_query_keys=(
        "f", "where", "outFields", "returnGeometry", "outSR", "resultRecordCount", "resultOffset"
    ),
)


def lookup_postcode(postcode: str) -> SourceResult:
    """Map a live UK postcode to current ONS geography codes and coordinates."""

    normalized = normalize_postcode(postcode)
    metadata = parse_arcgis_layer_metadata_json(
        get_bytes(
            ONSPD_LAYER_URL,
            params=onspd_layer_metadata_params(),
            policy=ONSPD_SOURCE_POLICY,
        )
    )
    object_id_field = arcgis_object_id_field(metadata)
    params = onspd_postcode_query_params(normalized, object_id_field=object_id_field)
    records = parse_arcgis_feature_page_json(
        get_bytes(ONSPD_QUERY_URL, params=params, policy=ONSPD_SOURCE_POLICY),
        feature_id_field=object_id_field,
    )
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
    offset: int = 0,
) -> SourceResult:
    """Query one bounded GLA town-centre result page.

    The layer metadata is acquired first to retain the advertised object ID,
    which is required for a stable boundary locator.  ``offset`` makes page
    traversal explicit for a complete-snapshot workflow.
    """

    _validate_town_centre_page(limit, offset)
    metadata = fetch_town_centre_layer_metadata()
    object_id_field = arcgis_object_id_field(metadata)

    where = "1=1"
    if name:
        escaped = name.upper().replace("'", "''")
        where = f"UPPER(sitename) LIKE '%{escaped}%'"
    fields = [
        "sitename",
        "borough",
        "planningauthority",
        "designation",
        "classification",
        "lastupdateddate",
        "source",
        "notes",
    ]
    if object_id_field and object_id_field not in fields:
        fields.insert(0, object_id_field)
    params: dict[str, object] = {
        "where": where,
        "outFields": ",".join(fields),
        "returnGeometry": str(include_geometry).lower(),
        "outSR": 4326,
        "resultRecordCount": limit,
        "f": "json",
    }
    if offset:
        params["resultOffset"] = offset
    records = parse_arcgis_feature_page_json(
        get_bytes(GLA_TOWN_CENTRE_QUERY_URL, params=params, policy=_GLA_POLICY),
        feature_id_field=object_id_field,
    )
    return source_result(
        category="town_centre_geography",
        source="Greater London Authority Town Centre Boundaries",
        source_url=f"{GLA_TOWN_CENTRE_QUERY_URL}?{urlencode(params)}",
        published_at=None,
        source_updated_at=_arcgis_update_time(metadata),
        records=records,
    )


def fetch_town_centre_layer_metadata() -> dict[str, Any]:
    """Return captured GLA layer metadata needed to freeze feature identity."""

    return parse_arcgis_layer_metadata_json(
        get_bytes(
            GLA_TOWN_CENTRE_LAYER_URL, params={"f": "json"}, policy=_GLA_POLICY
        )
    )


def parse_arcgis_layer_metadata_json(evidence: bytes) -> dict[str, Any]:
    """Parse captured ArcGIS layer metadata without network access."""

    return _json_object(evidence)


def parse_arcgis_feature_page_json(
    evidence: bytes,
    *,
    feature_id_field: str | None = None,
) -> list[dict[str, object]]:
    """Parse one captured ArcGIS feature page and retain stable feature IDs."""

    payload = _json_object(evidence)
    features = payload.get("features", [])
    if not isinstance(features, list):
        raise ValueError("ArcGIS feature response must contain a feature list")
    object_id_field = feature_id_field or arcgis_object_id_field(payload)
    records = []
    for feature in features:
        if not isinstance(feature, Mapping):
            raise ValueError("ArcGIS feature must be an object")
        record = _flatten_feature(feature)
        if object_id_field and record.get(object_id_field) is not None:
            record["source_feature_id"] = record[object_id_field]
        records.append(record)
    _add_spatial_reference(records, payload)
    return records


def normalize_postcode(postcode: str) -> str:
    """Return an ONSPD-compatible postcode form without querying the network."""

    if not isinstance(postcode, str):
        raise ValueError("postcode must be a string")
    compact = "".join(postcode.upper().split())
    if not compact.isalnum() or not 4 <= len(compact) <= 8:
        raise ValueError("postcode must contain a valid compact postcode")
    return f"{compact[:-3]} {compact[-3:]}"


def onspd_layer_metadata_params() -> dict[str, str]:
    """Return the sole supported ONSPD layer-metadata query."""

    return {"f": "json"}


def onspd_postcode_query_params(
    postcode: str, *, object_id_field: str | None
) -> dict[str, object]:
    """Build the fixed, one-postcode ONSPD ArcGIS query.

    The object ID advertised by the captured layer metadata is included in
    ``outFields`` whenever it is available, so a response has a durable
    feature locator rather than only a postcode-derived identity.
    """

    normalized = normalize_postcode(postcode)
    fields = [
        "PCDS",
        "LAD25CD",
        "WD25CD",
        "OA21CD",
        "LSOA21CD",
        "MSOA21CD",
        "LAT",
        "LONG",
    ]
    if object_id_field is not None:
        if not _ARCGIS_FIELD_NAME.fullmatch(object_id_field):
            raise ValueError("ArcGIS object ID field is invalid")
        if object_id_field not in fields:
            fields.insert(0, object_id_field)
    return {
        "where": f"PCDS='{normalized.replace(chr(39), chr(39) * 2)}'",
        "outFields": ",".join(fields),
        "returnGeometry": "true",
        "outSR": 4326,
        "f": "json",
    }


def arcgis_object_id_field(metadata: Mapping[str, object]) -> str | None:
    """Return ArcGIS's advertised feature identity field, if available."""

    for key in ("objectIdField", "objectIdFieldName"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
    return None


_ARCGIS_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _flatten_feature(feature: Mapping[str, object]) -> dict[str, object]:
    """Flatten an ArcGIS feature while preserving optional geometry."""

    attributes = feature.get("attributes")
    record = dict(attributes) if isinstance(attributes, Mapping) else {}
    if "geometry" in feature:
        record["geometry"] = feature["geometry"]
    return record


def _add_spatial_reference(
    records: list[dict[str, object]], payload: Mapping[str, object]
) -> None:
    spatial_reference = payload.get("spatialReference")
    if spatial_reference:
        for record in records:
            record["spatial_reference"] = spatial_reference


def _arcgis_update_time(metadata: Mapping[str, object]) -> str | None:
    editing_info = metadata.get("editingInfo")
    if not isinstance(editing_info, dict):
        return None
    milliseconds = editing_info.get("dataLastEditDate")
    if not isinstance(milliseconds, (int, float)):
        return None
    return datetime.fromtimestamp(milliseconds / 1000, UTC).isoformat()


def arcgis_layer_vintage(metadata: Mapping[str, object]) -> str | None:
    """Return the captured ArcGIS layer's advertised data-vintage timestamp."""

    return _arcgis_update_time(metadata)


def _validate_town_centre_page(limit: int, offset: int) -> None:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_TOWN_CENTRE_PAGE_SIZE
    ):
        raise ValueError(
            f"limit must be between 1 and {MAX_TOWN_CENTRE_PAGE_SIZE}"
        )
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")


def _json_object(evidence: bytes) -> dict[str, Any]:
    value = json.loads(evidence)
    if not isinstance(value, dict):
        raise ValueError("ArcGIS evidence must contain a JSON object")
    return value
