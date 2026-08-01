"""Strict, evidence-backed ingestion for internal London submarket mappings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING
import unicodedata

from .canonical import CanonicalizationError, parse_canonical_json
from .parser_runner import ParserExecutionError, ParserLimits, parse_saved_artifact

if TYPE_CHECKING:
    from nan_fung.operational import OperationalStore, PersistedEvidence, RunHandle


SUBMARKET_MAPPING_DATASOURCE_ID = "custom.london_office_submarkets"
SUBMARKET_MAPPING_MEDIA_TYPE = "application/json"
SUBMARKET_MAPPING_PARSER_LIMITS = ParserLimits(
    timeout_seconds=5,
    max_input_bytes=48 * 1024,
    max_output_bytes=64 * 1024,
)
_MAX_MAPPING_NAME_CHARS = 160
_MAX_LOCATION_CHARS = 160
_MAX_VERSION_CHARS = 64
_MAX_ATTESTATION_CHARS = 1_024
_MAX_LOCATIONS = 256


class SubmarketMappingError(ValueError):
    """A manual submarket mapping did not meet its fixed contract."""


@dataclass(frozen=True, slots=True)
class SubmarketMapping:
    """Normalized, intentionally small internal submarket mapping."""

    name: str
    locations: tuple[str, ...]
    version: str | None

    @property
    def key(self) -> str:
        return "-".join(self.name.casefold().split())

    def as_json(self) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "locations": list(self.locations),
        }
        if self.version is not None:
            result["version"] = self.version
        return result


def validate_submarket_mapping_submission(
    *, media_type: str | None, attestation: str | None
) -> str:
    """Validate metadata that must accompany this internal configuration."""

    if media_type != SUBMARKET_MAPPING_MEDIA_TYPE:
        raise SubmarketMappingError("SUBMARKET_MAPPING_MEDIA_TYPE_REQUIRED")
    if not isinstance(attestation, str) or not attestation.strip():
        raise SubmarketMappingError("SUBMARKET_MAPPING_ATTESTATION_REQUIRED")
    return _bounded_text(attestation, maximum=_MAX_ATTESTATION_CHARS)


def parse_submarket_mapping_json(payload: bytes) -> dict[str, object]:
    """Parse a bounded JSON artifact into the sandbox protocol's JSON domain."""

    if not isinstance(payload, bytes):
        raise SubmarketMappingError("SUBMARKET_MAPPING_INVALID")
    if len(payload) > SUBMARKET_MAPPING_PARSER_LIMITS.max_input_bytes:
        raise SubmarketMappingError("SUBMARKET_MAPPING_INPUT_LIMIT")
    try:
        value = parse_canonical_json(payload.decode("utf-8"))
    except (CanonicalizationError, UnicodeDecodeError) as error:
        raise SubmarketMappingError("SUBMARKET_MAPPING_INVALID") from error
    return submarket_mapping_from_value(value).as_json()


def submarket_mapping_from_value(value: object) -> SubmarketMapping:
    """Validate a parser result again before it reaches the writer boundary."""

    if not isinstance(value, Mapping):
        raise SubmarketMappingError("SUBMARKET_MAPPING_INVALID")
    keys = set(value)
    allowed = {"name", "locations", "version"}
    required = {"name", "locations"}
    if keys - allowed or required - keys:
        raise SubmarketMappingError("SUBMARKET_MAPPING_INVALID")

    name = _bounded_text(value.get("name"), maximum=_MAX_MAPPING_NAME_CHARS)
    raw_locations = value.get("locations")
    if not isinstance(raw_locations, list) or not raw_locations:
        raise SubmarketMappingError("SUBMARKET_MAPPING_INVALID")
    if len(raw_locations) > _MAX_LOCATIONS:
        raise SubmarketMappingError("SUBMARKET_MAPPING_TOO_MANY_LOCATIONS")
    locations = tuple(
        _bounded_text(item, maximum=_MAX_LOCATION_CHARS)
        for item in raw_locations
    )
    normalized_locations = {
        unicodedata.normalize("NFC", location.casefold()) for location in locations
    }
    if len(normalized_locations) != len(locations):
        raise SubmarketMappingError("SUBMARKET_MAPPING_DUPLICATE_LOCATION")

    version: str | None = None
    if "version" in value:
        version = _bounded_text(value["version"], maximum=_MAX_VERSION_CHARS)
    return SubmarketMapping(name, locations, version)


def persist_submarket_mapping_observation(
    store: OperationalStore,
    run: RunHandle,
    evidence: PersistedEvidence,
) -> str:
    """Sandbox-parse saved evidence and attach one geography observation."""

    try:
        mapping = parse_saved_artifact(
            store.artifacts,
            evidence.artifact,
            parse_submarket_mapping_json,
            limits=SUBMARKET_MAPPING_PARSER_LIMITS,
            decoder=submarket_mapping_from_value,
        )
    except ParserExecutionError as error:
        raise SubmarketMappingError(str(error)) from error
    payload: dict[str, object] = {
        "geography_code": f"custom-submarket:{mapping.key}",
        "geography_name": mapping.name,
        "locations": list(mapping.locations),
        "mapping_name": mapping.name,
        "mapping_type": "custom_submarket",
    }
    if mapping.version is not None:
        payload["mapping_version"] = mapping.version
    return store.persist_observation(
        run,
        record_key=("custom_submarket", mapping.key),
        payload=payload,
        record_type="geography",
        category="submarket_geography",
        evidence=(evidence,),
        definition_text="Internal London office submarket mapping",
        limitations=(
            "Custom submarket mapping; it is not an official geography boundary.",
        ),
        locator={
            "kind": "json_pointer",
            "pointer": "",
            "schema": "submarket_mapping.v1",
        },
    )


def mapping_import_error_code(error: BaseException) -> str:
    """Return the stable public failure code without exposing artifact content."""

    if str(error) == "PARSER_ISOLATION_UNAVAILABLE":
        return "PARSER_ISOLATION_UNAVAILABLE"
    return "SUBMARKET_MAPPING_INVALID"


def _bounded_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise SubmarketMappingError("SUBMARKET_MAPPING_INVALID")
    normalized = unicodedata.normalize("NFC", value)
    if any(ord(character) < 32 for character in normalized):
        raise SubmarketMappingError("SUBMARKET_MAPPING_INVALID")
    compact = " ".join(normalized.split())
    if not compact or len(compact) > maximum:
        raise SubmarketMappingError("SUBMARKET_MAPPING_INVALID")
    return compact
