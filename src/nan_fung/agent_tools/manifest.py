"""Versioned, packaged product capability and refresh-profile authority."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import json
from importlib import resources
from types import MappingProxyType

from nan_fung.read_api.contracts import ALLOWED_FILTERS, QUERY_KINDS


CAPABILITY_ASSET = "capabilities.v1.json"
REFRESH_PROFILE_ASSET = "refresh-profiles.v1.json"
CAPABILITY_MANIFEST_SCHEMA_VERSION = "agent_tool_capability_manifest.v1"
REFRESH_PROFILE_SCHEMA_VERSION = "agent_tool_refresh_profiles.v1"
CAPABILITY_ENTRY_SCHEMA_VERSION = "agent_tool_capability.v1"
REFRESH_PROFILE_ENTRY_SCHEMA_VERSION = "agent_tool_refresh_profile.v1"


class ManifestError(ValueError):
    """A packaged product authority asset is absent or structurally unsafe."""


@dataclass(frozen=True)
class QueryTemplate:
    """Fixed filter values plus the small model-selectable filter intersection."""

    fixed_filters: Mapping[str, tuple[str, ...]]
    allowed_filters: frozenset[str]


@dataclass(frozen=True)
class Capability:
    schema_version: str
    capability_id: str
    status: str
    datasource_ids: tuple[str, ...]
    metric_ids: tuple[str, ...]
    geography: Mapping[str, object]
    refresh_profiles: tuple[str, ...]
    query_templates: Mapping[str, QueryTemplate]
    query_disabled: bool
    numeric_value_field: str | None
    numeric_value_type: str | None
    limitations: tuple[str, ...]
    blocked_reason: str | None
    owner: str

    @property
    def query_kinds(self) -> tuple[str, ...]:
        return tuple(self.query_templates)

    def safe_projection(self, allowed_profiles: frozenset[str]) -> dict[str, object]:
        """Return only model-safe product coverage—not implementation internals."""

        return {
            "capability_id": self.capability_id,
            "status": self.status,
            "query_kinds": list(self.query_kinds),
            "query_disabled": self.query_disabled,
            "datasource_ids": list(self.datasource_ids),
            "metric_ids": list(self.metric_ids),
            "geography": dict(self.geography),
            "refresh_profiles": [
                profile for profile in self.refresh_profiles if profile in allowed_profiles
            ],
            "limitations": list(self.limitations),
            "blocked_reason": self.blocked_reason,
        }


class CapabilityManifest(Mapping[str, Capability]):
    """A validated, immutable mapping of product capability ID to definition."""

    def __init__(self, *, schema_version: str, version: str, capabilities: Mapping[str, Capability]) -> None:
        if schema_version != CAPABILITY_MANIFEST_SCHEMA_VERSION:
            raise ManifestError("unsupported capability manifest schema")
        if not version:
            raise ManifestError("manifest version is required")
        if not capabilities:
            raise ManifestError("capability manifest is empty")
        if any(key != item.capability_id for key, item in capabilities.items()):
            raise ManifestError("capability mapping key mismatch")
        self.schema_version = schema_version
        self.version = version
        self._capabilities = MappingProxyType(dict(capabilities))

    def __getitem__(self, key: str) -> Capability:
        return self._capabilities[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._capabilities)

    def __len__(self) -> int:
        return len(self._capabilities)


@dataclass(frozen=True)
class AgentRefreshProfile:
    """Facade-owned safe selector, separate from broker implementation details."""

    profile_id: str
    schema_version: str
    datasource_id: str
    capability_ids: tuple[str, ...]
    allowed_scope_keys: frozenset[str]
    required_scope_keys: frozenset[str]
    single_value_scope_keys: frozenset[str]
    max_scope_values: int
    definition_version: int
    effective_lane: str
    promotion_policy: str
    poll_after_seconds: int
    freshness_precheck: str | None


class RefreshProfileCatalog(Mapping[str, AgentRefreshProfile]):
    """Immutable packaged profile mapping."""

    def __init__(self, *, schema_version: str, version: str, profiles: Mapping[str, AgentRefreshProfile]) -> None:
        if schema_version != REFRESH_PROFILE_SCHEMA_VERSION:
            raise ManifestError("unsupported refresh profile schema")
        if not version or not profiles:
            raise ManifestError("refresh profile catalog is incomplete")
        if any(key != profile.profile_id for key, profile in profiles.items()):
            raise ManifestError("refresh profile mapping key mismatch")
        self.schema_version = schema_version
        self.version = version
        self._profiles = MappingProxyType(dict(profiles))

    def __getitem__(self, key: str) -> AgentRefreshProfile:
        return self._profiles[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._profiles)

    def __len__(self) -> int:
        return len(self._profiles)


def load_capability_manifest() -> CapabilityManifest:
    """Load the wheel-packaged, machine-readable product authority."""

    value = _load_asset(CAPABILITY_ASSET)
    schema_version = _text(value.get("schema_version"), "schema_version")
    version = _text(value.get("manifest_version"), "manifest_version")
    entries = _objects(value.get("capabilities"), "capabilities")
    capabilities: dict[str, Capability] = {}
    for entry in entries:
        capability = _parse_capability(entry)
        if capability.capability_id in capabilities:
            raise ManifestError("capability IDs must be unique")
        capabilities[capability.capability_id] = capability
    return CapabilityManifest(
        schema_version=schema_version,
        version=version,
        capabilities=capabilities,
    )


def load_refresh_profiles() -> RefreshProfileCatalog:
    """Load fixed request selectors; no profile comes from model input."""

    value = _load_asset(REFRESH_PROFILE_ASSET)
    schema_version = _text(value.get("schema_version"), "schema_version")
    version = _text(value.get("catalog_version"), "catalog_version")
    entries = _objects(value.get("profiles"), "profiles")
    profiles: dict[str, AgentRefreshProfile] = {}
    for entry in entries:
        profile = _parse_profile(entry)
        if profile.profile_id in profiles:
            raise ManifestError("profile IDs must be unique")
        profiles[profile.profile_id] = profile
    return RefreshProfileCatalog(schema_version=schema_version, version=version, profiles=profiles)


def default_capability_manifest() -> CapabilityManifest:
    """Compatibility-friendly name for the packaged default manifest."""

    return load_capability_manifest()


def default_refresh_profiles() -> RefreshProfileCatalog:
    """Compatibility-friendly name for the packaged profile catalog."""

    return load_refresh_profiles()


def _load_asset(name: str) -> Mapping[str, object]:
    try:
        raw = resources.files("nan_fung.agent_tools").joinpath(name).read_text(encoding="utf-8")
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (FileNotFoundError, ModuleNotFoundError, ValueError, json.JSONDecodeError) as error:
        raise ManifestError("packaged agent-tool asset is unavailable") from error
    if not isinstance(value, Mapping):
        raise ManifestError("asset root must be an object")
    return value


def _parse_capability(value: Mapping[str, object]) -> Capability:
    allowed = {
        "schema_version",
        "capability_id",
        "status",
        "datasource_ids",
        "metric_ids",
        "geography",
        "refresh_profiles",
        "query_templates",
        "query_disabled",
        "numeric_value_field",
        "numeric_value_type",
        "limitations",
        "blocked_reason",
        "owner",
    }
    _no_unknown(value, allowed, "capability")
    if value.get("schema_version") != CAPABILITY_ENTRY_SCHEMA_VERSION:
        raise ManifestError("unsupported capability schema")
    status = _text(value.get("status"), "status")
    if status not in {"supported", "partial", "blocked"}:
        raise ManifestError("capability status is invalid")
    query_disabled = value.get("query_disabled")
    if not isinstance(query_disabled, bool):
        raise ManifestError("query_disabled must be boolean")
    templates_value = value.get("query_templates")
    if not isinstance(templates_value, Mapping):
        raise ManifestError("query_templates must be an object")
    templates = {
        key: _parse_template(template)
        for key, template in templates_value.items()
        if isinstance(key, str) and isinstance(template, Mapping)
    }
    if len(templates) != len(templates_value):
        raise ManifestError("query templates are invalid")
    if any(name not in QUERY_KINDS for name in templates):
        raise ManifestError("query template kind is invalid")
    if query_disabled and templates:
        raise ManifestError("disabled query capability cannot have templates")
    if not query_disabled and not templates:
        raise ManifestError("enabled query capability needs a template")
    numeric_field = _optional_text(value.get("numeric_value_field"), "numeric_value_field")
    numeric_type = _optional_text(value.get("numeric_value_type"), "numeric_value_type")
    if (numeric_field is None) != (numeric_type is None):
        raise ManifestError("numeric selector must include field and type")
    if numeric_type is not None and numeric_type != "decimal_string":
        raise ManifestError("numeric selector type is invalid")
    geography = value.get("geography")
    if not isinstance(geography, Mapping):
        raise ManifestError("geography must be an object")
    return Capability(
        schema_version=_text(value.get("schema_version"), "schema_version"),
        capability_id=_text(value.get("capability_id"), "capability_id"),
        status=status,
        datasource_ids=_strings(value.get("datasource_ids"), "datasource_ids"),
        metric_ids=_strings(value.get("metric_ids"), "metric_ids"),
        geography=MappingProxyType(dict(geography)),
        refresh_profiles=_strings(value.get("refresh_profiles"), "refresh_profiles"),
        query_templates=MappingProxyType(templates),
        query_disabled=query_disabled,
        numeric_value_field=numeric_field,
        numeric_value_type=numeric_type,
        limitations=_strings(value.get("limitations"), "limitations"),
        blocked_reason=_optional_text(value.get("blocked_reason"), "blocked_reason"),
        owner=_text(value.get("owner"), "owner"),
    )


def _parse_template(value: Mapping[str, object]) -> QueryTemplate:
    _no_unknown(value, {"fixed_filters", "allowed_filters"}, "query template")
    fixed = value.get("fixed_filters")
    if not isinstance(fixed, Mapping):
        raise ManifestError("fixed_filters must be an object")
    parsed_fixed = {
        key: _strings(item, "fixed filter")
        for key, item in fixed.items()
        if isinstance(key, str)
    }
    if len(parsed_fixed) != len(fixed):
        raise ManifestError("fixed filter key is invalid")
    allowed_filters = frozenset(_strings(value.get("allowed_filters"), "allowed_filters"))
    if not set(parsed_fixed) <= ALLOWED_FILTERS or not allowed_filters <= ALLOWED_FILTERS:
        raise ManifestError("query template filter is not a ReadQuery allowlist member")
    return QueryTemplate(
        fixed_filters=MappingProxyType(parsed_fixed),
        allowed_filters=allowed_filters,
    )


def _parse_profile(value: Mapping[str, object]) -> AgentRefreshProfile:
    allowed = {
        "schema_version",
        "profile_id",
        "datasource_id",
        "capability_ids",
        "allowed_scope_keys",
        "required_scope_keys",
        "single_value_scope_keys",
        "max_scope_values",
        "definition_version",
        "effective_lane",
        "promotion_policy",
        "poll_after_seconds",
        "freshness_precheck",
    }
    _no_unknown(value, allowed, "refresh profile")
    if value.get("schema_version") != REFRESH_PROFILE_ENTRY_SCHEMA_VERSION:
        raise ManifestError("unsupported refresh profile schema")
    allowed_scope = frozenset(_strings(value.get("allowed_scope_keys"), "allowed_scope_keys"))
    required_scope = frozenset(_strings(value.get("required_scope_keys"), "required_scope_keys"))
    single_value_scope = frozenset(_strings(value.get("single_value_scope_keys"), "single_value_scope_keys"))
    if not required_scope <= allowed_scope or not single_value_scope <= allowed_scope:
        raise ManifestError("refresh profile scope requirements are invalid")
    max_scope_values = value.get("max_scope_values")
    definition_version = value.get("definition_version")
    poll_after = value.get("poll_after_seconds")
    if (
        not isinstance(max_scope_values, int)
        or isinstance(max_scope_values, bool)
        or not 1 <= max_scope_values <= 100
        or not isinstance(definition_version, int)
        or isinstance(definition_version, bool)
        or definition_version < 1
        or not isinstance(poll_after, int)
        or isinstance(poll_after, bool)
        or poll_after < 1
    ):
        raise ManifestError("refresh profile numeric bounds are invalid")
    lane = _text(value.get("effective_lane"), "effective_lane")
    if lane not in {"production_ingestion", "source_discovery", "ad_hoc_research"}:
        raise ManifestError("refresh profile lane is invalid")
    return AgentRefreshProfile(
        profile_id=_text(value.get("profile_id"), "profile_id"),
        schema_version=_text(value.get("schema_version"), "schema_version"),
        datasource_id=_text(value.get("datasource_id"), "datasource_id"),
        capability_ids=_strings(value.get("capability_ids"), "capability_ids"),
        allowed_scope_keys=allowed_scope,
        required_scope_keys=required_scope,
        single_value_scope_keys=single_value_scope,
        max_scope_values=max_scope_values,
        definition_version=definition_version,
        effective_lane=lane,
        promotion_policy=_text(value.get("promotion_policy"), "promotion_policy"),
        poll_after_seconds=poll_after,
        freshness_precheck=_optional_text(value.get("freshness_precheck"), "freshness_precheck"),
    )


def _objects(value: object, name: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, Mapping) for item in value):
        raise ManifestError(f"{name} must be a non-empty array of objects")
    return tuple(value)


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ManifestError(f"{name} must be an array of non-empty strings")
    if len(set(value)) != len(value):
        raise ManifestError(f"{name} must not contain duplicates")
    return tuple(value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{name} must be a non-empty string")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)


def _no_unknown(value: Mapping[str, object], allowed: set[str], name: str) -> None:
    if set(value) - allowed or allowed - set(value):
        raise ManifestError(f"{name} fields do not match schema")


__all__ = [
    "AgentRefreshProfile",
    "CAPABILITY_ASSET",
    "CAPABILITY_MANIFEST_SCHEMA_VERSION",
    "Capability",
    "CapabilityManifest",
    "ManifestError",
    "QueryTemplate",
    "REFRESH_PROFILE_ASSET",
    "REFRESH_PROFILE_SCHEMA_VERSION",
    "RefreshProfileCatalog",
    "default_capability_manifest",
    "default_refresh_profiles",
    "load_capability_manifest",
    "load_refresh_profiles",
]
