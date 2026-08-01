"""Frozen versioned datasource definitions and runtime binding checks.

Definitions contain serialisable policy and transformation identities only.
Callables live in a separate runtime registry so a deployed callable cannot
silently alter the meaning of a persisted definition snapshot.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Literal

from .canonical import (
    CanonicalizationError,
    definition_hash,
    freeze_json,
    normalize_timestamp,
    source_hash,
    thaw_json,
)


DescriptorStatus = Literal["draft", "discovery", "production", "retired"]
Lane = Literal["production_ingestion", "source_discovery", "ad_hoc_research"]

_STATUSES = frozenset({"draft", "discovery", "production", "retired"})
_LANES = frozenset({"production_ingestion", "source_discovery", "ad_hoc_research"})
_SOURCE_KINDS = frozenset(
    {"structured_api", "feed", "file_release", "report", "manual_web", "reference"}
)
_AUTOMATION_MODES = frozenset({"automatic", "assisted", "manual", "on_demand", "fanout"})
_SNAPSHOT_MODES = frozenset(
    {"append_only", "incremental", "full_snapshot", "point_lookup"}
)
_PROMOTION_POLICIES = frozenset({"automatic", "manual_review", "never_canonical"})
_DATA_KINDS = frozenset({"direct", "proxy", "report-derived"})
_CONFIDENCES = frozenset({"high", "medium", "low"})
_ACCESS_CLASSES = frozenset({"open", "internal", "restricted", "reference_only"})
_SURFACE_KINDS = frozenset(
    {"api", "feed", "landing_page", "attachment", "dataset", "manual_submission"}
)
_BINDING_KINDS = frozenset({"collector", "parser", "record_key", "validator"})


class RegistryError(ValueError):
    """Raised for malformed, duplicate, or unavailable registry entries."""


class MissingRuntimeBindingError(RegistryError):
    """Raised when an executable operation lacks a versioned binding."""


@dataclass(frozen=True, slots=True, order=True)
class BindingDescriptor:
    """A serialisable callable identity, never the callable itself."""

    kind: str
    name: str
    version: str

    def __post_init__(self) -> None:
        if self.kind not in _BINDING_KINDS:
            raise RegistryError(f"unsupported binding kind: {self.kind!r}")
        if not self.name or not self.version:
            raise RegistryError("binding name and version are required")

    def as_json(self) -> dict[str, str]:
        return {"kind": self.kind, "name": self.name, "version": self.version}


@dataclass(frozen=True, slots=True)
class SourceBinding:
    """An upstream surface used by a datasource definition."""

    source_id: str
    source_version: int = 1
    role: str = "primary"
    required: bool = True

    def __post_init__(self) -> None:
        if not self.source_id or self.source_version < 1:
            raise RegistryError("source binding requires id and positive version")
        if self.role not in {
            "primary",
            "discovery",
            "attachment",
            "supporting",
            "manual_submission",
        }:
            raise RegistryError(f"unsupported source binding role: {self.role!r}")

    def as_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "role": self.role,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class ScheduleDescriptor:
    """A versioned schedule rule attached to a definition.

    The scheduler interprets ``rule``; descriptors intentionally preserve it as
    canonical JSON so changing schedule semantics requires a new definition.
    """

    name: str
    rule: Mapping[str, Any]
    timezone: str = "Europe/London"
    catchup_policy: str = "latest_only"
    max_catchup_jobs: int = 1
    max_catchup_horizon_seconds: int = 0
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise RegistryError("schedule name is required")
        if self.catchup_policy not in {"latest_only", "windowed", "all_slots", "manual"}:
            raise RegistryError(f"unsupported catch-up policy: {self.catchup_policy!r}")
        if not 1 <= self.max_catchup_jobs <= 1_000:
            raise RegistryError("max_catchup_jobs must be in [1, 1000]")
        if self.max_catchup_horizon_seconds < 0:
            raise RegistryError("max_catchup_horizon_seconds cannot be negative")
        object.__setattr__(self, "rule", freeze_json(self.rule))

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rule": thaw_json(self.rule),
            "timezone": self.timezone,
            "catchup_policy": self.catchup_policy,
            "max_catchup_jobs": self.max_catchup_jobs,
            "max_catchup_horizon_seconds": self.max_catchup_horizon_seconds,
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True)
class SourceDefinitionDescriptor:
    """Frozen versioned representation of an upstream source surface."""

    source_id: str
    source_version: int
    display_name: str
    publisher: str
    surface_kind: str
    allowed_hosts: tuple[str, ...]
    retention_profile: str
    access_class: str = "open"
    licence: str | None = None
    base_origin_redacted: str | None = None
    status: DescriptorStatus = "draft"
    approved_by: str | None = None
    approved_at: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_status(self.status, self.approved_by, self.approved_at)
        if not self.source_id or self.source_version < 1:
            raise RegistryError("source definition requires id and positive version")
        if not self.display_name or not self.publisher:
            raise RegistryError("source display_name and publisher are required")
        if self.surface_kind not in _SURFACE_KINDS:
            raise RegistryError(f"unsupported source surface: {self.surface_kind!r}")
        if self.access_class not in _ACCESS_CLASSES:
            raise RegistryError(f"unsupported access class: {self.access_class!r}")
        if not self.retention_profile:
            raise RegistryError("source retention profile is required")
        if not self.allowed_hosts and self.surface_kind != "manual_submission":
            raise RegistryError("network source must declare allowed hosts")
        if self.approved_at is not None:
            object.__setattr__(self, "approved_at", normalize_timestamp(self.approved_at))
        object.__setattr__(self, "allowed_hosts", tuple(host.lower() for host in self.allowed_hosts))
        object.__setattr__(self, "details", freeze_json(self.details))

    def as_json(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "display_name": self.display_name,
            "publisher": self.publisher,
            "surface_kind": self.surface_kind,
            "base_origin_redacted": self.base_origin_redacted,
            "allowed_hosts": list(self.allowed_hosts),
            "licence": self.licence,
            "access_class": self.access_class,
            "retention_profile": self.retention_profile,
            "details": thaw_json(self.details),
            "status": self.status,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }

    @property
    def source_hash(self) -> str:
        return source_hash(self.as_json())


@dataclass(frozen=True, slots=True)
class DatasourceDefinitionDescriptor:
    """Frozen, serialisable contract for one datasource version."""

    datasource_id: str
    definition_version: int
    display_name: str
    publisher: str
    category: str
    source_kind: str
    automation_mode: str
    collector_name: str
    collector_version: str
    parser_name: str
    parser_version: str
    schema_version: str
    locator_version: str
    record_key_builder_name: str
    record_key_version: str
    source_bindings: tuple[SourceBinding, ...]
    allowed_hosts: tuple[str, ...]
    retention_policy: str
    validator_bindings: tuple[BindingDescriptor, ...] = ()
    default_request: Mapping[str, Any] = field(default_factory=dict)
    schedules: tuple[ScheduleDescriptor, ...] = ()
    catchup_policy: str = "latest_only"
    snapshot_mode: str = "incremental"
    default_lane: Lane = "production_ingestion"
    validation_policy: Mapping[str, Any] = field(default_factory=dict)
    retry_policy: Mapping[str, Any] = field(default_factory=dict)
    timeout_policy: Mapping[str, Any] = field(default_factory=dict)
    artifact_policy: Mapping[str, Any] = field(default_factory=dict)
    review_policy: Mapping[str, Any] = field(default_factory=dict)
    freshness_policy: Mapping[str, Any] = field(default_factory=dict)
    promotion_policy: str = "automatic"
    data_kind: str = "direct"
    default_confidence: str = "high"
    licence: str | None = None
    access_class: str = "open"
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    status: DescriptorStatus = "draft"
    approved_by: str | None = None
    approved_at: str | None = None

    def __post_init__(self) -> None:
        _validate_status(self.status, self.approved_by, self.approved_at)
        if not self.datasource_id or self.definition_version < 1:
            raise RegistryError("datasource definition requires id and positive version")
        for name in (
            "display_name",
            "publisher",
            "category",
            "collector_name",
            "collector_version",
            "parser_name",
            "parser_version",
            "schema_version",
            "locator_version",
            "record_key_builder_name",
            "record_key_version",
            "retention_policy",
        ):
            if not getattr(self, name):
                raise RegistryError(f"{name} is required")
        if not self.source_bindings:
            raise RegistryError("datasource definition requires at least one source binding")
        if self.source_kind not in _SOURCE_KINDS:
            raise RegistryError(f"unsupported source kind: {self.source_kind!r}")
        if self.automation_mode not in _AUTOMATION_MODES:
            raise RegistryError(f"unsupported automation mode: {self.automation_mode!r}")
        if self.catchup_policy not in {"latest_only", "windowed", "all_slots", "manual"}:
            raise RegistryError(f"unsupported catch-up policy: {self.catchup_policy!r}")
        if self.snapshot_mode not in _SNAPSHOT_MODES:
            raise RegistryError(f"unsupported snapshot mode: {self.snapshot_mode!r}")
        if self.default_lane not in _LANES:
            raise RegistryError(f"unsupported lane: {self.default_lane!r}")
        if self.promotion_policy not in _PROMOTION_POLICIES:
            raise RegistryError(f"unsupported promotion policy: {self.promotion_policy!r}")
        if self.data_kind not in _DATA_KINDS:
            raise RegistryError(f"unsupported data kind: {self.data_kind!r}")
        if self.default_confidence not in _CONFIDENCES:
            raise RegistryError(f"unsupported confidence: {self.default_confidence!r}")
        if self.access_class not in _ACCESS_CLASSES:
            raise RegistryError(f"unsupported access class: {self.access_class!r}")
        if not self.allowed_hosts and self.automation_mode != "manual":
            raise RegistryError("network datasource must declare allowed hosts")
        validators = tuple(self.validator_bindings)
        if any(binding.kind != "validator" for binding in validators):
            raise RegistryError("validator_bindings must contain validator identities")
        object.__setattr__(self, "source_bindings", tuple(self.source_bindings))
        object.__setattr__(self, "validator_bindings", validators)
        object.__setattr__(self, "schedules", tuple(self.schedules))
        object.__setattr__(self, "allowed_hosts", tuple(host.lower() for host in self.allowed_hosts))
        for name in (
            "default_request",
            "validation_policy",
            "retry_policy",
            "timeout_policy",
            "artifact_policy",
            "review_policy",
            "freshness_policy",
            "capabilities",
        ):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))
        if self.approved_at is not None:
            object.__setattr__(self, "approved_at", normalize_timestamp(self.approved_at))

    @property
    def collector_binding(self) -> BindingDescriptor:
        return BindingDescriptor("collector", self.collector_name, self.collector_version)

    @property
    def parser_binding(self) -> BindingDescriptor:
        return BindingDescriptor("parser", self.parser_name, self.parser_version)

    @property
    def record_key_binding(self) -> BindingDescriptor:
        return BindingDescriptor(
            "record_key", self.record_key_builder_name, self.record_key_version
        )

    @property
    def executable_bindings(self) -> tuple[BindingDescriptor, ...]:
        return (
            self.collector_binding,
            self.parser_binding,
            self.record_key_binding,
            *self.validator_bindings,
        )

    def as_json(self) -> dict[str, Any]:
        """Return the complete immutable semantic definition snapshot."""

        return {
            "datasource_id": self.datasource_id,
            "definition_version": self.definition_version,
            "display_name": self.display_name,
            "publisher": self.publisher,
            "category": self.category,
            "source_kind": self.source_kind,
            "automation_mode": self.automation_mode,
            "collector_name": self.collector_name,
            "collector_version": self.collector_version,
            "source_bindings": [binding.as_json() for binding in self.source_bindings],
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "schema_version": self.schema_version,
            "locator_version": self.locator_version,
            "record_key_builder_name": self.record_key_builder_name,
            "record_key_version": self.record_key_version,
            "validator_bindings": [binding.as_json() for binding in self.validator_bindings],
            "default_request": thaw_json(self.default_request),
            "schedules": [schedule.as_json() for schedule in self.schedules],
            "catchup_policy": self.catchup_policy,
            "snapshot_mode": self.snapshot_mode,
            "default_lane": self.default_lane,
            "allowed_hosts": list(self.allowed_hosts),
            "validation_policy": thaw_json(self.validation_policy),
            "retry_policy": thaw_json(self.retry_policy),
            "timeout_policy": thaw_json(self.timeout_policy),
            "artifact_policy": thaw_json(self.artifact_policy),
            "review_policy": thaw_json(self.review_policy),
            "freshness_policy": thaw_json(self.freshness_policy),
            "promotion_policy": self.promotion_policy,
            "data_kind": self.data_kind,
            "default_confidence": self.default_confidence,
            "licence": self.licence,
            "access_class": self.access_class,
            "retention_policy": self.retention_policy,
            "capabilities": thaw_json(self.capabilities),
            "status": self.status,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }

    @property
    def definition_hash(self) -> str:
        return definition_hash(self.as_json())


def _validate_status(
    status: str, approved_by: str | None, approved_at: str | None
) -> None:
    if status not in _STATUSES:
        raise RegistryError(f"unsupported descriptor status: {status!r}")
    if status == "production" and (not approved_by or not approved_at):
        raise RegistryError("production definition requires approval identity and timestamp")


@dataclass(frozen=True, slots=True)
class RuntimeValidation:
    """A non-throwing report for operator status and health output."""

    descriptor: DatasourceDefinitionDescriptor
    operation: str
    missing: tuple[BindingDescriptor, ...]

    @property
    def ready(self) -> bool:
        return not self.missing

    def require(self) -> None:
        if not self.missing:
            return
        labels = ", ".join(
            f"{item.kind}:{item.name}@{item.version}" for item in self.missing
        )
        raise MissingRuntimeBindingError(
            f"{self.descriptor.datasource_id}@{self.descriptor.definition_version} "
            f"cannot {self.operation}; missing runtime binding(s): {labels}"
        )


class RuntimeBindings:
    """Mutable deployment-local map of versioned callable identities."""

    def __init__(self) -> None:
        self._bindings: dict[BindingDescriptor, Callable[..., Any]] = {}

    def register(
        self,
        kind: str,
        name: str,
        version: str,
        callable_: Callable[..., Any],
    ) -> BindingDescriptor:
        descriptor = BindingDescriptor(kind, name, version)
        if not callable(callable_):
            raise RegistryError("runtime binding must be callable")
        if descriptor in self._bindings:
            raise RegistryError(
                f"duplicate runtime binding: {kind}:{name}@{version}"
            )
        self._bindings[descriptor] = callable_
        return descriptor

    def resolve(self, binding: BindingDescriptor) -> Callable[..., Any]:
        try:
            return self._bindings[binding]
        except KeyError as error:
            raise MissingRuntimeBindingError(
                f"missing runtime binding: {binding.kind}:{binding.name}@{binding.version}"
            ) from error

    def contains(self, binding: BindingDescriptor) -> bool:
        return binding in self._bindings

    def validate(
        self,
        descriptor: DatasourceDefinitionDescriptor,
        *,
        operation: str = "ingest",
    ) -> RuntimeValidation:
        if operation not in {"read", "ingest", "reparse"}:
            raise RegistryError(f"unsupported runtime operation: {operation!r}")
        if operation == "read":
            return RuntimeValidation(descriptor, operation, ())
        required = descriptor.executable_bindings
        missing = tuple(binding for binding in required if binding not in self._bindings)
        return RuntimeValidation(descriptor, operation, missing)


class DatasourceRegistry:
    """Read-only descriptor registry with deterministic version lookup."""

    def __init__(
        self,
        definitions: Iterable[DatasourceDefinitionDescriptor],
        sources: Iterable[SourceDefinitionDescriptor] = (),
    ) -> None:
        definition_map: dict[tuple[str, int], DatasourceDefinitionDescriptor] = {}
        source_map: dict[tuple[str, int], SourceDefinitionDescriptor] = {}
        hash_map: dict[str, DatasourceDefinitionDescriptor] = {}
        for source in sources:
            key = (source.source_id, source.source_version)
            if key in source_map:
                raise RegistryError(f"duplicate source definition: {key!r}")
            source_map[key] = source
        for definition in definitions:
            key = (definition.datasource_id, definition.definition_version)
            if key in definition_map:
                raise RegistryError(f"duplicate datasource definition: {key!r}")
            for source_binding in definition.source_bindings:
                if (source_binding.source_id, source_binding.source_version) not in source_map:
                    raise RegistryError(
                        f"unknown source binding for {definition.datasource_id}: "
                        f"{source_binding.source_id}@{source_binding.source_version}"
                    )
            hashed = definition.definition_hash
            if hashed in hash_map:
                raise RegistryError(
                    "duplicate datasource semantic definition hash for "
                    f"{definition.datasource_id}@{definition.definition_version}"
                )
            definition_map[key] = definition
            hash_map[hashed] = definition
        self._definitions = MappingProxyType(definition_map)
        self._sources = MappingProxyType(source_map)
        self._by_hash = MappingProxyType(hash_map)

    @property
    def definitions(self) -> tuple[DatasourceDefinitionDescriptor, ...]:
        return tuple(
            self._definitions[key]
            for key in sorted(self._definitions, key=lambda item: (item[0], item[1]))
        )

    @property
    def sources(self) -> tuple[SourceDefinitionDescriptor, ...]:
        return tuple(
            self._sources[key]
            for key in sorted(self._sources, key=lambda item: (item[0], item[1]))
        )

    def lookup(
        self, datasource_id: str, definition_version: int | None = None
    ) -> DatasourceDefinitionDescriptor:
        if definition_version is None:
            versions = [
                version
                for identifier, version in self._definitions
                if identifier == datasource_id
            ]
            if not versions:
                raise RegistryError(f"unknown datasource: {datasource_id!r}")
            definition_version = max(versions)
        try:
            return self._definitions[(datasource_id, definition_version)]
        except KeyError as error:
            raise RegistryError(
                f"unknown datasource version: {datasource_id!r}@{definition_version}"
            ) from error

    def lookup_source(
        self, source_id: str, source_version: int | None = None
    ) -> SourceDefinitionDescriptor:
        if source_version is None:
            versions = [version for identifier, version in self._sources if identifier == source_id]
            if not versions:
                raise RegistryError(f"unknown source: {source_id!r}")
            source_version = max(versions)
        try:
            return self._sources[(source_id, source_version)]
        except KeyError as error:
            raise RegistryError(
                f"unknown source version: {source_id!r}@{source_version}"
            ) from error

    def lookup_hash(self, value: str) -> DatasourceDefinitionDescriptor:
        try:
            return self._by_hash[value]
        except KeyError as error:
            raise RegistryError("unknown datasource definition hash") from error

    def runtime_status(
        self,
        bindings: RuntimeBindings,
        *,
        operation: str = "ingest",
    ) -> dict[str, RuntimeValidation]:
        return {
            f"{definition.datasource_id}@{definition.definition_version}": bindings.validate(
                definition, operation=operation
            )
            for definition in self.definitions
        }


_APPROVAL_TIME = "2026-07-31T00:00:00.000000Z"
_OFFICIAL_MACRO_CAPABILITIES = {
    "runtime_migration": "bound",
    "offline_reparse": True,
    "backfill": "unsupported_current_vintage",
}
_FILE_RELEASE_CAPABILITIES = {
    "runtime_migration": "bound",
    "offline_reparse": True,
    "backfill": "unsupported_release_history",
}
_ONSPD_CAPABILITIES = {
    "runtime_migration": "bound",
    "offline_reparse": True,
    "backfill": "unsupported_point_lookup_history",
    "retention": "operator_approved_deadline_required",
}


def _source(
    source_id: str,
    display_name: str,
    publisher: str,
    surface_kind: str,
    hosts: Sequence[str],
    retention: str,
    *,
    status: DescriptorStatus,
    access_class: str = "open",
    licence: str | None = None,
) -> SourceDefinitionDescriptor:
    approved = status == "production"
    return SourceDefinitionDescriptor(
        source_id=source_id,
        source_version=1,
        display_name=display_name,
        publisher=publisher,
        surface_kind=surface_kind,
        allowed_hosts=tuple(hosts),
        retention_profile=retention,
        access_class=access_class,
        licence=licence,
        status=status,
        approved_by="architecture-decision" if approved else None,
        approved_at=_APPROVAL_TIME if approved else None,
    )


def _schedule(name: str, rule: Mapping[str, Any], *, catchup: str = "latest_only") -> ScheduleDescriptor:
    return ScheduleDescriptor(name=name, rule=rule, catchup_policy=catchup)


def _definition(
    datasource_id: str,
    display_name: str,
    publisher: str,
    category: str,
    source_kind: str,
    automation_mode: str,
    source_id: str,
    hosts: Sequence[str],
    retention: str,
    *,
    status: DescriptorStatus,
    schedule: ScheduleDescriptor | None = None,
    collector_name: str | None = None,
    parser_name: str | None = None,
    record_key_name: str | None = None,
    default_lane: Lane | None = None,
    promotion_policy: str | None = None,
    data_kind: str = "direct",
    confidence: str = "high",
    access_class: str = "open",
    licence: str | None = None,
    default_request: Mapping[str, Any] | None = None,
    capabilities: Mapping[str, Any] | None = None,
    validators: Sequence[BindingDescriptor] = (),
    source_ids: Sequence[str] | None = None,
) -> DatasourceDefinitionDescriptor:
    approved = status == "production"
    lane = default_lane or (
        "production_ingestion" if status == "production" else "source_discovery"
    )
    promotion = promotion_policy or (
        "automatic" if status == "production" else "never_canonical"
    )
    max_bytes = (
        100 * 1024 * 1024
        if source_kind == "report"
        else 250 * 1024 * 1024
        if source_kind == "file_release"
        else 25 * 1024 * 1024
    )
    stem = datasource_id.replace(".", "_")
    return DatasourceDefinitionDescriptor(
        datasource_id=datasource_id,
        definition_version=1,
        display_name=display_name,
        publisher=publisher,
        category=category,
        source_kind=source_kind,
        automation_mode=automation_mode,
        collector_name=collector_name or f"{stem}.collector",
        collector_version="v1",
        parser_name=parser_name or f"{stem}.parser",
        parser_version="v1",
        schema_version="v1",
        locator_version="v1",
        record_key_builder_name=record_key_name or f"{stem}.record_key",
        record_key_version="v1",
        source_bindings=tuple(
            SourceBinding(identifier) for identifier in (source_ids or (source_id,))
        ),
        allowed_hosts=tuple(hosts),
        retention_policy=retention,
        validator_bindings=tuple(validators),
        default_request=default_request or {},
        schedules=(schedule,) if schedule else (),
        catchup_policy=schedule.catchup_policy if schedule else "manual",
        snapshot_mode="point_lookup" if automation_mode == "on_demand" else "incremental",
        default_lane=lane,
        validation_policy={"schema": "v1"},
        retry_policy={"max_attempts": 3, "base_delay_seconds": 60},
        timeout_policy={"request_seconds": 30},
        artifact_policy={
            "capture_before_parse": True,
            "max_bytes": max_bytes,
            "max_archive_members": 1_000,
            "max_expanded_bytes": 1_024 * 1024 * 1024,
            "max_compression_ratio": 100,
        },
        review_policy={"required": promotion == "manual_review"},
        freshness_policy={"status": "configured"},
        promotion_policy=promotion,
        data_kind=data_kind,
        default_confidence=confidence,
        licence=licence,
        access_class=access_class,
        capabilities=capabilities or {"runtime_migration": "unbound"},
        status=status,
        approved_by="architecture-decision" if approved else None,
        approved_at=_APPROVAL_TIME if approved else None,
    )


@lru_cache(maxsize=1)
def default_registry() -> DatasourceRegistry:
    """Return the immutable seed registry for every ADR source family.

    A production approval is intentionally separate from a runtime binding.
    Definitions not yet migrated remain visible to health/CLI as ``unbound``
    through :meth:`RuntimeBindings.validate`; they cannot execute by accident.
    """

    sources = (
        _source("boe.iadb", "Bank of England IADB", "Bank of England", "api", ("www.bankofengland.co.uk",), "open_official", status="production", licence="Bank of England open data terms"),
        _source("boe.rss", "Bank of England RSS", "Bank of England", "feed", ("www.bankofengland.co.uk",), "restricted_report", status="discovery", access_class="restricted"),
        _source("boe.mpc_content", "Bank of England MPC content", "Bank of England", "landing_page", ("www.bankofengland.co.uk",), "per_artifact", status="discovery", access_class="restricted"),
        _source("ons.data_api", "ONS data API", "Office for National Statistics", "api", ("api.beta.ons.gov.uk",), "open_official", status="production", licence="Open Government Licence"),
        _source("nomis.api", "Nomis API", "Office for National Statistics", "api", ("www.nomisweb.co.uk",), "open_official", status="production", licence="Open Government Licence"),
        _source("govuk.voa_collection", "GOV.UK VOA NDR release collection", "GOV.UK", "landing_page", ("www.gov.uk",), "open_official", status="production", licence="Open Government Licence"),
        _source("voa.ndr_stock", "VOA NDR stock release", "Valuation Office Agency", "dataset", ("assets.publishing.service.gov.uk",), "open_official", status="production", licence="Open Government Licence"),
        _source("pld.api", "Planning London Datahub", "Greater London Authority", "api", ("planningdata.london.gov.uk",), "unapproved", status="discovery", access_class="restricted"),
        _source("bnp.report", "BNP Paribas market report", "BNP Paribas Real Estate", "attachment", ("www.realestate.bnpparibas.co.uk",), "restricted_report", status="discovery", access_class="restricted"),
        _source("rightmove.manual", "Rightmove commercial tracker", "Rightmove", "manual_submission", (), "reference_only", status="discovery", access_class="reference_only"),
        _source("ons.opn", "ONS Opinions and Lifestyle Survey", "Office for National Statistics", "attachment", ("www.ons.gov.uk",), "open_official", status="production", licence="Open Government Licence"),
        _source("mhclg.epc", "MHCLG EPC live tables", "MHCLG", "attachment", ("www.gov.uk",), "open_official", status="production", licence="Open Government Licence"),
        _source("mhclg.epc_attachment", "MHCLG EPC live-table attachment", "MHCLG", "attachment", ("assets.publishing.service.gov.uk",), "open_official", status="production", licence="Open Government Licence"),
        _source("govuk.search", "GOV.UK Search API", "GOV.UK", "api", ("www.gov.uk",), "unapproved", status="discovery"),
        _source("govuk.content", "GOV.UK Content API", "GOV.UK", "api", ("www.gov.uk",), "per_artifact", status="discovery", access_class="restricted"),
        _source("ons.onspd", "ONS Postcode Directory", "Office for National Statistics", "dataset", ("services1.arcgis.com",), "composite_geodata", status="production", licence="ONS/OS/Royal Mail attribution"),
        _source("gla.town_centres", "GLA town centre boundaries", "Greater London Authority", "dataset", ("gis.london.gov.uk",), "composite_geodata", status="discovery"),
        _source("custom.submarkets", "Internal commercial submarket rules", "Nan Fung", "manual_submission", (), "internal_config", status="production", access_class="internal", licence="internal configuration"),
    )
    validator = BindingDescriptor("validator", "bank_rate.validate", "v1")
    definitions = (
        _definition("bnp.central_london_office_report", "Central London office report", "BNP Paribas Real Estate", "office_market_report", "report", "assisted", "bnp.report", ("www.realestate.bnpparibas.co.uk",), "restricted_report", status="discovery", schedule=_schedule("weekly_discovery", {"kind": "weekly", "weekday": 0, "hour": 10, "minute": 0}), data_kind="report-derived", confidence="medium", access_class="restricted", promotion_policy="manual_review"),
        _definition("voa.ndr_office_stock", "VOA office stock", "Valuation Office Agency", "office_stock", "file_release", "automatic", "voa.ndr_stock", ("www.gov.uk", "assets.publishing.service.gov.uk"), "open_official", status="production", schedule=_schedule("quarterly_check", {"kind": "monthly", "months": [1, 4, 7, 10], "day": 15, "hour": 10, "minute": 0}), default_request={"area_code": "E12000007"}, source_ids=("govuk.voa_collection", "voa.ndr_stock"), capabilities=_FILE_RELEASE_CAPABILITIES),
        _definition("pld.applications_search", "PLD supply candidates", "Greater London Authority", "supply_pipeline", "structured_api", "automatic", "pld.api", ("planningdata.london.gov.uk",), "unapproved", status="discovery", schedule=_schedule("nightly_discovery", {"kind": "daily", "hour": 2, "minute": 30}), promotion_policy="never_canonical", access_class="restricted"),
        _definition("pld.application", "PLD application detail", "Greater London Authority", "supply_pipeline", "structured_api", "fanout", "pld.api", ("planningdata.london.gov.uk",), "unapproved", status="discovery", schedule=_schedule("active_daily", {"kind": "daily", "hour": 3, "minute": 30}), promotion_policy="never_canonical", access_class="restricted"),
        _definition("boe.bank_rate.iudbedr", "Bank Rate (IUDBEDR)", "Bank of England", "interest-rates-monetary-policy", "structured_api", "automatic", "boe.iadb", ("www.bankofengland.co.uk",), "open_official", status="production", schedule=_schedule("weekday_bank_rate", {"kind": "weekly", "weekdays": [0, 1, 2, 3, 4], "hour": 19, "minute": 0}), collector_name="bank_rate.collect", parser_name="bank_rate.csv", record_key_name="bank_rate.record_key", default_request={"series": "IUDBEDR"}, validators=(validator,), capabilities={"runtime_migration": "bound", "offline_reparse": True}),
        _definition("boe.mpc_news", "MPC release metadata", "Bank of England", "feed", "feed", "automatic", "boe.rss", ("www.bankofengland.co.uk",), "restricted_report", status="discovery", schedule=_schedule("two_hour_poll", {"kind": "interval", "seconds": 7200}), access_class="restricted", promotion_policy="never_canonical"),
        _definition("boe.mpc_content", "MPC linked content", "Bank of England", "interest-rates-monetary-policy", "report", "fanout", "boe.mpc_content", ("www.bankofengland.co.uk",), "per_artifact", status="discovery", access_class="restricted", promotion_policy="never_canonical"),
        _definition("ons.gdp.ecyx", "ONS monthly GVA growth", "Office for National Statistics", "gdp", "structured_api", "automatic", "ons.data_api", ("api.beta.ons.gov.uk",), "open_official", status="production", schedule=_schedule("weekday_poll", {"kind": "weekly", "weekdays": [0, 1, 2, 3, 4], "hour": 8, "minute": 30}), default_request={"series": "ECYX"}, capabilities=_OFFICIAL_MACRO_CAPABILITIES),
        _definition("ons.gdp.ihyq", "ONS quarterly GDP growth", "Office for National Statistics", "gdp", "structured_api", "automatic", "ons.data_api", ("api.beta.ons.gov.uk",), "open_official", status="production", schedule=_schedule("weekday_poll", {"kind": "weekly", "weekdays": [0, 1, 2, 3, 4], "hour": 8, "minute": 30}), default_request={"series": "IHYQ"}, capabilities=_OFFICIAL_MACRO_CAPABILITIES),
        _definition("ons.inflation.d7g7", "ONS CPI inflation", "Office for National Statistics", "inflation", "structured_api", "automatic", "ons.data_api", ("api.beta.ons.gov.uk",), "open_official", status="production", schedule=_schedule("weekday_poll", {"kind": "weekly", "weekdays": [0, 1, 2, 3, 4], "hour": 8, "minute": 35}), default_request={"series": "D7G7"}, capabilities=_OFFICIAL_MACRO_CAPABILITIES),
        _definition("ons.inflation.l55o", "ONS CPIH inflation", "Office for National Statistics", "inflation", "structured_api", "automatic", "ons.data_api", ("api.beta.ons.gov.uk",), "open_official", status="production", schedule=_schedule("weekday_poll", {"kind": "weekly", "weekdays": [0, 1, 2, 3, 4], "hour": 8, "minute": 35}), default_request={"series": "L55O"}, capabilities=_OFFICIAL_MACRO_CAPABILITIES),
        _definition("ons.inflation.czbh", "ONS RPI inflation", "Office for National Statistics", "inflation", "structured_api", "automatic", "ons.data_api", ("api.beta.ons.gov.uk",), "open_official", status="production", schedule=_schedule("weekday_poll", {"kind": "weekly", "weekdays": [0, 1, 2, 3, 4], "hour": 8, "minute": 35}), default_request={"series": "CZBH"}, capabilities=_OFFICIAL_MACRO_CAPABILITIES),
        _definition("ons.labour.lf24", "ONS employment rate", "Office for National Statistics", "employment-market", "structured_api", "automatic", "ons.data_api", ("api.beta.ons.gov.uk",), "open_official", status="production", schedule=_schedule("weekday_poll", {"kind": "weekly", "weekdays": [0, 1, 2, 3, 4], "hour": 8, "minute": 40}), default_request={"series": "LF24"}, capabilities=_OFFICIAL_MACRO_CAPABILITIES),
        _definition("ons.labour.mgsx", "ONS unemployment rate", "Office for National Statistics", "employment-market", "structured_api", "automatic", "ons.data_api", ("api.beta.ons.gov.uk",), "open_official", status="production", schedule=_schedule("weekday_poll", {"kind": "weekly", "weekdays": [0, 1, 2, 3, 4], "hour": 8, "minute": 40}), default_request={"series": "MGSX"}, capabilities=_OFFICIAL_MACRO_CAPABILITIES),
        _definition("ons.labour.ap2y", "ONS vacancies", "Office for National Statistics", "employment-market", "structured_api", "automatic", "ons.data_api", ("api.beta.ons.gov.uk",), "open_official", status="production", schedule=_schedule("weekday_poll", {"kind": "weekly", "weekdays": [0, 1, 2, 3, 4], "hour": 8, "minute": 40}), default_request={"series": "AP2Y"}, capabilities=_OFFICIAL_MACRO_CAPABILITIES),
        _definition("ons.labour.kai9", "ONS earnings", "Office for National Statistics", "employment-market", "structured_api", "automatic", "ons.data_api", ("api.beta.ons.gov.uk",), "open_official", status="production", schedule=_schedule("weekday_poll", {"kind": "weekly", "weekdays": [0, 1, 2, 3, 4], "hour": 8, "minute": 40}), default_request={"series": "KAI9"}, capabilities=_OFFICIAL_MACRO_CAPABILITIES),
        _definition("nomis.nm_59_1.london_lfs", "Nomis London LFS", "Office for National Statistics", "employment-market", "structured_api", "automatic", "nomis.api", ("www.nomisweb.co.uk",), "open_official", status="production", schedule=_schedule("weekday_poll", {"kind": "weekly", "weekdays": [0, 1, 2, 3, 4], "hour": 9, "minute": 30}), default_request={"dataset": "NM_59_1", "geography": "E12000007"}, capabilities=_OFFICIAL_MACRO_CAPABILITIES),
        _definition("nomis.nm_130_1.london_workforce_jobs", "Nomis London workforce jobs", "Office for National Statistics", "employment-market", "structured_api", "automatic", "nomis.api", ("www.nomisweb.co.uk",), "open_official", status="production", schedule=_schedule("weekly_poll", {"kind": "weekly", "weekday": 1, "hour": 9, "minute": 35}), default_request={"dataset": "NM_130_1", "geography": "E12000007"}, capabilities=_OFFICIAL_MACRO_CAPABILITIES),
        _definition("rightmove.commercial_insights_tracker", "Rightmove commercial enquiry proxy", "Rightmove", "office_demand", "manual_web", "manual", "rightmove.manual", (), "reference_only", status="discovery", schedule=_schedule("weekly_review", {"kind": "weekly", "weekday": 0, "hour": 10, "minute": 30}), data_kind="proxy", confidence="medium", access_class="reference_only", promotion_policy="manual_review"),
        _definition("ons.opn.hybrid_working", "ONS hybrid working proxy", "Office for National Statistics", "hybrid_working", "file_release", "automatic", "ons.opn", ("www.ons.gov.uk",), "open_official", status="production", schedule=_schedule("weekly_poll", {"kind": "weekly", "weekday": 4, "hour": 10, "minute": 0}), data_kind="proxy", confidence="medium", capabilities=_FILE_RELEASE_CAPABILITIES),
        _definition("mhclg.epc.live_table_a_london", "London non-domestic EPC proxy", "MHCLG", "esg_energy_efficiency", "file_release", "automatic", "mhclg.epc", ("www.gov.uk", "assets.publishing.service.gov.uk"), "open_official", status="production", schedule=_schedule("weekly_poll", {"kind": "weekly", "weekday": 4, "hour": 10, "minute": 30}), data_kind="proxy", confidence="medium", source_ids=("mhclg.epc", "mhclg.epc_attachment"), capabilities=_FILE_RELEASE_CAPABILITIES),
        _definition("govuk.search.market_news", "GOV.UK market-news candidates", "GOV.UK", "market_news_events", "structured_api", "automatic", "govuk.search", ("www.gov.uk",), "unapproved", status="discovery", schedule=_schedule("six_hour_poll", {"kind": "interval", "seconds": 21600}), promotion_policy="never_canonical"),
        _definition("govuk.content.market_news", "GOV.UK market-news detail", "GOV.UK", "market_news_events", "structured_api", "fanout", "govuk.content", ("www.gov.uk",), "per_artifact", status="discovery", access_class="restricted", promotion_policy="never_canonical"),
        _definition("ons.onspd.postcode", "ONS postcode geography", "Office for National Statistics", "postcode_geography", "structured_api", "on_demand", "ons.onspd", ("services1.arcgis.com",), "composite_geodata", status="production", access_class="open", validators=(BindingDescriptor("validator", "ons_onspd_postcode.validate", "v1"),), capabilities=_ONSPD_CAPABILITIES),
        _definition("gla.town_centre_boundaries", "GLA town centre boundaries", "Greater London Authority", "town_centre_geography", "structured_api", "automatic", "gla.town_centres", ("gis.london.gov.uk",), "composite_geodata", status="discovery", schedule=_schedule("monthly_snapshot", {"kind": "monthly", "weekday": 0, "week": 1, "hour": 3, "minute": 30}), promotion_policy="never_canonical", confidence="medium"),
        _definition("custom.london_office_submarkets", "London office submarket mapping", "Nan Fung", "submarket_geography", "reference", "manual", "custom.submarkets", (), "internal_config", status="production", default_lane="production_ingestion", promotion_policy="manual_review", access_class="internal", capabilities={"runtime_migration": "manual_configuration", "network": False}),
    )
    return DatasourceRegistry(definitions, sources)


def default_runtime_bindings() -> RuntimeBindings:
    """Return bindings for the explicitly migrated core workflows."""

    from .bank_rate import (
        collect_bank_rate,
        parse_bank_rate_csv,
        bank_rate_record_key,
        validate_bank_rate_record,
    )

    bindings = RuntimeBindings()
    bindings.register("collector", "bank_rate.collect", "v1", collect_bank_rate)
    bindings.register("parser", "bank_rate.csv", "v1", parse_bank_rate_csv)
    bindings.register("record_key", "bank_rate.record_key", "v1", bank_rate_record_key)
    bindings.register("validator", "bank_rate.validate", "v1", validate_bank_rate_record)

    from .official_macro import nomis_record_key, ons_record_key
    from .official_macro_workflow import (
        collect_nomis_nm_130_1,
        collect_nomis_nm_59_1,
        collect_ons_ap2y,
        collect_ons_czbh,
        collect_ons_d7g7,
        collect_ons_ecyx,
        collect_ons_ihyq,
        collect_ons_kai9,
        collect_ons_l55o,
        collect_ons_lf24,
        collect_ons_mgsx,
        parse_nomis_nm_130_1_artifact,
        parse_nomis_nm_59_1_artifact,
        parse_ons_ap2y_artifact,
        parse_ons_czbh_artifact,
        parse_ons_d7g7_artifact,
        parse_ons_ecyx_artifact,
        parse_ons_ihyq_artifact,
        parse_ons_kai9_artifact,
        parse_ons_l55o_artifact,
        parse_ons_lf24_artifact,
        parse_ons_mgsx_artifact,
    )

    official_macro_bindings = (
        ("ons.gdp.ecyx", collect_ons_ecyx, parse_ons_ecyx_artifact, ons_record_key),
        ("ons.gdp.ihyq", collect_ons_ihyq, parse_ons_ihyq_artifact, ons_record_key),
        ("ons.inflation.d7g7", collect_ons_d7g7, parse_ons_d7g7_artifact, ons_record_key),
        ("ons.inflation.l55o", collect_ons_l55o, parse_ons_l55o_artifact, ons_record_key),
        ("ons.inflation.czbh", collect_ons_czbh, parse_ons_czbh_artifact, ons_record_key),
        ("ons.labour.lf24", collect_ons_lf24, parse_ons_lf24_artifact, ons_record_key),
        ("ons.labour.mgsx", collect_ons_mgsx, parse_ons_mgsx_artifact, ons_record_key),
        ("ons.labour.ap2y", collect_ons_ap2y, parse_ons_ap2y_artifact, ons_record_key),
        ("ons.labour.kai9", collect_ons_kai9, parse_ons_kai9_artifact, ons_record_key),
        (
            "nomis.nm_59_1.london_lfs",
            collect_nomis_nm_59_1,
            parse_nomis_nm_59_1_artifact,
            nomis_record_key,
        ),
        (
            "nomis.nm_130_1.london_workforce_jobs",
            collect_nomis_nm_130_1,
            parse_nomis_nm_130_1_artifact,
            nomis_record_key,
        ),
    )
    for datasource_id, collector, parser, record_key in official_macro_bindings:
        stem = datasource_id.replace(".", "_")
        bindings.register("collector", f"{stem}.collector", "v1", collector)
        bindings.register("parser", f"{stem}.parser", "v1", parser)
        bindings.register("record_key", f"{stem}.record_key", "v1", record_key)

    from .file_release_workflow import (
        collect_mhclg_epc_live_table_a_london,
        collect_ons_hybrid_working,
        collect_voa_ndr_office_stock,
        mhclg_epc_live_table_a_london_record_key,
        ons_hybrid_working_record_key,
        parse_current_voa_london_office_stock_zip,
        parse_hybrid_working_xlsx,
        parse_non_domestic_epc_ratings_ods,
        voa_ndr_office_stock_record_key,
    )

    file_release_bindings = (
        (
            "voa.ndr_office_stock",
            collect_voa_ndr_office_stock,
            parse_current_voa_london_office_stock_zip,
            voa_ndr_office_stock_record_key,
        ),
        (
            "ons.opn.hybrid_working",
            collect_ons_hybrid_working,
            parse_hybrid_working_xlsx,
            ons_hybrid_working_record_key,
        ),
        (
            "mhclg.epc.live_table_a_london",
            collect_mhclg_epc_live_table_a_london,
            parse_non_domestic_epc_ratings_ods,
            mhclg_epc_live_table_a_london_record_key,
        ),
    )
    for datasource_id, collector, parser, record_key in file_release_bindings:
        stem = datasource_id.replace(".", "_")
        bindings.register("collector", f"{stem}.collector", "v1", collector)
        bindings.register("parser", f"{stem}.parser", "v1", parser)
        bindings.register("record_key", f"{stem}.record_key", "v1", record_key)

    from .onspd_lifecycle import (
        acquire_live_onspd_postcode,
        onspd_postcode_record_key,
        parse_onspd_feature_page_json,
        validate_onspd_postcode_record,
    )

    bindings.register(
        "collector",
        "ons_onspd_postcode.collector",
        "v1",
        acquire_live_onspd_postcode,
    )
    bindings.register(
        "parser",
        "ons_onspd_postcode.parser",
        "v1",
        parse_onspd_feature_page_json,
    )
    bindings.register(
        "record_key",
        "ons_onspd_postcode.record_key",
        "v1",
        onspd_postcode_record_key,
    )
    bindings.register(
        "validator",
        "ons_onspd_postcode.validate",
        "v1",
        validate_onspd_postcode_record,
    )
    return bindings
