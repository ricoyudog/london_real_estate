"""Exact, access-aware citation metadata projections for canonical reads.

The projection resolves the same as-of canonical selection as ``query_data_v1``
and returns only bounded lineage metadata.  It deliberately has no artifact
URI, content hash, request, response, or raw-evidence field.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from nan_fung.storage.db import connect_database

from .access import AccessClass, ReadContext, most_restrictive_access
from .contracts import InvalidReadRequest, normalise_utc, utc_timestamp


MAX_CITATION_OBSERVATIONS = 20
MAX_LOCATOR_BYTES = 16_384
MAX_LOCATOR_DEPTH = 8
MAX_LOCATOR_ITEMS = 100
MAX_LOCATOR_STRING_CHARS = 512
MAX_METADATA_STRING_CHARS = 512
MAX_PUBLIC_URL_CHARS = 2_048

_UNSAFE_LOCATOR_KEYS = frozenset(
    {
        "artifact",
        "artifact_uri",
        "authorization",
        "body",
        "bytes",
        "content",
        "cookie",
        "cookies",
        "excerpt",
        "header",
        "headers",
        "html",
        "path",
        "payload",
        "raw",
        "request",
        "response",
        "source_url",
        "text",
        "token",
        "uri",
        "url",
    }
)


@dataclass(frozen=True)
class CitationProjection:
    """Safe metadata for one exact canonical observation-evidence locator.

    A facade mints its session-scoped citation handle from the immutable
    ``anchor_as_of``, ``canonical_run_id``, ``observation_id``, ``evidence_id``
    and ``locator_hash`` tuple.  The remaining fields are only presentation
    metadata; they never give callers a raw-evidence retrieval route.
    """

    anchor_as_of: datetime
    canonical_run_id: str
    observation_id: str
    evidence_id: str
    locator_hash: str
    datasource_id: str
    publisher: str
    retrieved_at: datetime
    access_class: AccessClass | str
    data_kind: str
    confidence: str
    limitations: tuple[str, ...] = ()
    locator: Mapping[str, object] = field(default_factory=dict)
    title: str | None = None
    public_url: str | None = None
    published_at: datetime | None = None
    source_updated_at: datetime | None = None
    licence_or_attribution: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("canonical_run_id", self.canonical_run_id),
            ("observation_id", self.observation_id),
            ("evidence_id", self.evidence_id),
            ("locator_hash", self.locator_hash),
            ("datasource_id", self.datasource_id),
            ("publisher", self.publisher),
            ("data_kind", self.data_kind),
            ("confidence", self.confidence),
        ):
            if not isinstance(value, str) or not value:
                raise InvalidReadRequest(f"citation {name} must be non-empty")
        if (
            len(self.locator_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.locator_hash)
        ):
            raise InvalidReadRequest("citation locator_hash must be a SHA-256 hex digest")
        limitations = tuple(self.limitations)
        if any(not isinstance(item, str) or not item for item in limitations):
            raise InvalidReadRequest("citation limitations must contain non-empty strings")
        locator = _safe_locator(self.locator)
        if locator is None:
            raise InvalidReadRequest("citation locator is not safely bounded")
        warnings = tuple(self.warnings)
        if any(not isinstance(item, str) or not item for item in warnings):
            raise InvalidReadRequest("citation warnings must contain non-empty strings")
        object.__setattr__(self, "anchor_as_of", normalise_utc(self.anchor_as_of))
        object.__setattr__(self, "retrieved_at", normalise_utc(self.retrieved_at))
        object.__setattr__(self, "access_class", AccessClass(self.access_class))
        object.__setattr__(self, "limitations", limitations)
        object.__setattr__(self, "locator", MappingProxyType(locator))
        object.__setattr__(self, "warnings", warnings)
        for name in ("published_at", "source_updated_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, normalise_utc(value))


@runtime_checkable
class CitationProjectionRepository(Protocol):
    """Read-only exact-lineage lookup implemented by the data plane."""

    def citation_projection(
        self,
        context: ReadContext,
        *,
        anchor_as_of: datetime,
        observation_ids: Iterable[str],
    ) -> Iterable[CitationProjection]: ...


def citation_projection_v1(
    repository: CitationProjectionRepository,
    context: ReadContext,
    *,
    anchor_as_of: datetime,
    observation_ids: Iterable[str],
) -> tuple[CitationProjection, ...]:
    """Resolve exact, canonical citation lineage through a read-only repository.

    Missing or unreadable lineage is deliberately represented by an absent
    projection, so a caller cannot use this function to probe restricted
    evidence.  A facade treats an absent projection as an unusable citation.
    """

    anchor = normalise_utc(anchor_as_of)
    ids = _normalise_observation_ids(observation_ids)
    return tuple(
        repository.citation_projection(
            context,
            anchor_as_of=anchor,
            observation_ids=ids,
        )
    )


def sqlite_citation_projection(
    database_path: str | Path,
    context: ReadContext,
    *,
    anchor_as_of: datetime,
    observation_ids: Iterable[str],
) -> tuple[CitationProjection, ...]:
    """SQLite implementation used by :class:`SQLiteReadRepository`.

    This is public for trusted in-process adapters that only have a database
    path.  It has the same bounds and access checks as ``citation_projection_v1``.
    """

    anchor = normalise_utc(anchor_as_of)
    ids = _normalise_observation_ids(observation_ids)
    if not ids:
        return ()
    # Import lazily to avoid the repository importing this module at startup.
    # The statement is the canonical Read API's exact as-of selection, so a
    # citation cannot drift to a newer run or evidence item.
    from .sqlite_repository import _CANONICAL_AS_OF_SQL

    access_classes = tuple(sorted(str(value) for value in context.allowed_access_classes))
    access_sql = ", ".join("?" for _ in access_classes)
    selected_sql = _CANONICAL_AS_OF_SQL.format(access_sql=access_sql)
    observation_sql = ", ".join("?" for _ in ids)
    query = f"""
    SELECT selected.canonical_run_id,
           selected.observation_id,
           selected.datasource_id,
           selected.access_class AS datasource_access_class,
           selected.data_kind,
           selected.confidence,
           selected.limitations_json,
           evidence.evidence_id,
           evidence.access_class AS evidence_access_class,
           evidence.retrieved_at,
           evidence.response_json,
           locator.locator_json,
           locator.locator_hash,
           source.publisher,
           source.access_class AS source_access_class,
           source.licence AS source_licence,
           source.allowed_hosts_json,
           definition.licence AS datasource_licence
    FROM ({selected_sql}) AS selected
    JOIN observation_evidence AS locator
      ON locator.run_id = selected.canonical_run_id
     AND locator.observation_id = selected.observation_id
    JOIN evidence_artifact AS evidence ON evidence.evidence_id = locator.evidence_id
    JOIN source_definition AS source
      ON source.source_id = evidence.source_id
     AND source.source_version = evidence.source_version
    JOIN datasource_definition AS definition
      ON definition.datasource_id = selected.datasource_id
     AND definition.definition_version = selected.definition_version
    WHERE selected.observation_id IN ({observation_sql})
      AND evidence.access_class IN ({access_sql})
      AND source.access_class IN ({access_sql})
    ORDER BY selected.observation_id, evidence.evidence_id, locator.locator_hash
    """
    anchor_text = utc_timestamp(anchor)
    parameters: tuple[object, ...] = (
        anchor_text,
        anchor_text,
        anchor_text,
        anchor_text,
        *access_classes,
        *ids,
        *access_classes,
        *access_classes,
    )
    connection = connect_database(database_path, read_only=True)
    try:
        rows = tuple(connection.execute(query, parameters).fetchall())
    finally:
        connection.close()

    projections: list[CitationProjection] = []
    for row in rows:
        projection = _projection_from_row(row, anchor)
        if projection is not None:
            projections.append(projection)
    return tuple(projections)


def _normalise_observation_ids(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise InvalidReadRequest("citation observation IDs must be an iterable of IDs")
    try:
        ids = tuple(dict.fromkeys(values))
    except TypeError as error:
        raise InvalidReadRequest("citation observation IDs must be an iterable of IDs") from error
    if len(ids) > MAX_CITATION_OBSERVATIONS:
        raise InvalidReadRequest(
            f"citation projection accepts at most {MAX_CITATION_OBSERVATIONS} observations"
        )
    if any(not isinstance(value, str) or not value or len(value) > 256 for value in ids):
        raise InvalidReadRequest("citation observation IDs must be non-empty strings")
    return ids


def _projection_from_row(row: object, anchor: datetime) -> CitationProjection | None:
    try:
        locator = _safe_locator(json.loads(row["locator_json"]))  # type: ignore[index]
        limitations = _limitations(json.loads(row["limitations_json"]))  # type: ignore[index]
        retrieved_at = _parse_metadata_timestamp(row["retrieved_at"])  # type: ignore[index]
        response = _response_metadata(row["response_json"], row["allowed_hosts_json"])  # type: ignore[index]
        access_class = most_restrictive_access(
            (
                row["datasource_access_class"],  # type: ignore[index]
                row["evidence_access_class"],  # type: ignore[index]
                row["source_access_class"],  # type: ignore[index]
            )
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if locator is None or limitations is None or retrieved_at is None or access_class is None:
        return None
    title, public_url, published_at, source_updated_at, warnings = response
    licence = _bounded_optional_string(row["source_licence"])  # type: ignore[index]
    if licence is None:
        licence = _bounded_optional_string(row["datasource_licence"])  # type: ignore[index]
    if licence is None:
        warnings = (*warnings, "licence_or_attribution_unavailable")
    try:
        return CitationProjection(
            anchor_as_of=anchor,
            canonical_run_id=row["canonical_run_id"],  # type: ignore[index]
            observation_id=row["observation_id"],  # type: ignore[index]
            evidence_id=row["evidence_id"],  # type: ignore[index]
            locator_hash=row["locator_hash"],  # type: ignore[index]
            datasource_id=row["datasource_id"],  # type: ignore[index]
            publisher=row["publisher"],  # type: ignore[index]
            retrieved_at=retrieved_at,
            access_class=access_class,
            data_kind=row["data_kind"],  # type: ignore[index]
            confidence=row["confidence"],  # type: ignore[index]
            limitations=limitations,
            locator=locator,
            title=title,
            public_url=public_url,
            published_at=published_at,
            source_updated_at=source_updated_at,
            licence_or_attribution=licence,
            warnings=warnings,
        )
    except InvalidReadRequest:
        return None


def _response_metadata(
    response_json: object, allowed_hosts_json: object
) -> tuple[str | None, str | None, datetime | None, datetime | None, tuple[str, ...]]:
    warnings: list[str] = []
    try:
        response = json.loads(response_json)
    except (TypeError, json.JSONDecodeError):
        response = {}
    if not isinstance(response, Mapping):
        response = {}
    try:
        allowed_hosts_data = json.loads(allowed_hosts_json)
    except (TypeError, json.JSONDecodeError):
        allowed_hosts_data = ()
    allowed_hosts = (
        tuple(item.lower() for item in allowed_hosts_data if isinstance(item, str))
        if isinstance(allowed_hosts_data, list)
        else ()
    )

    title = _bounded_optional_string(response.get("title"))
    if title is None:
        warnings.append("title_unavailable")
    public_url = _sanitised_public_url(response.get("final_url"), allowed_hosts)
    if public_url is None:
        warnings.append("public_url_unavailable")
    published_at = _parse_metadata_timestamp(response.get("published_at"))
    if published_at is None:
        warnings.append("published_at_unavailable")
    source_updated_at = _parse_metadata_timestamp(response.get("source_updated_at"))
    if source_updated_at is None:
        warnings.append("source_updated_at_unavailable")
    return title, public_url, published_at, source_updated_at, tuple(warnings)


def _bounded_optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalised = value.strip()
    if not normalised or len(normalised) > MAX_METADATA_STRING_CHARS or "\x00" in normalised:
        return None
    return normalised


def _sanitised_public_url(value: object, allowed_hosts: tuple[str, ...]) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_PUBLIC_URL_CHARS:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"https", "http"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    normalized_host = hostname.lower().rstrip(".")
    if port not in (None, 443) or not _public_host_allowed(normalized_host, allowed_hosts):
        return None
    netloc = normalized_host if port is None else f"{normalized_host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", "", ""))


def _public_host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    """Match the persisted source allowlist without broadening exact hosts."""

    for allowed in allowed_hosts:
        normalized_allowed = allowed.lower().rstrip(".")
        if normalized_allowed.startswith("*."):
            suffix = normalized_allowed[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == normalized_allowed:
            return True
    return False


def _parse_metadata_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return normalise_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _limitations(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or len(value) > MAX_LOCATOR_ITEMS:
        return None
    if any(
        not isinstance(item, str)
        or not item
        or len(item) > MAX_LOCATOR_STRING_CHARS
        or "\x00" in item
        for item in value
    ):
        return None
    return tuple(value)


def _safe_locator(value: object, *, depth: int = 0) -> dict[str, object] | None:
    """Copy a compact structural locator while rejecting evidence-like fields."""

    if depth > MAX_LOCATOR_DEPTH or not isinstance(value, Mapping) or not value:
        return None
    copied = _safe_locator_value(value, depth=depth)
    if not isinstance(copied, dict) or not copied:
        return None
    try:
        encoded = json.dumps(
            copied,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return copied if len(encoded) <= MAX_LOCATOR_BYTES else None


def _safe_locator_value(value: object, *, depth: int) -> object | None:
    if depth > MAX_LOCATOR_DEPTH:
        return None
    if isinstance(value, Mapping):
        if len(value) > MAX_LOCATOR_ITEMS:
            return None
        copied: dict[str, object] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > MAX_LOCATOR_STRING_CHARS
            ):
                return None
            normalized_key = key.lower()
            if (
                normalized_key in _UNSAFE_LOCATOR_KEYS
                or normalized_key.endswith("_url")
            ):
                # A locator may retain its structural position while dropping a
                # raw artifact/network route.  The original locator hash still
                # binds this safe projection to the immutable evidence row.
                continue
            nested = _safe_locator_value(item, depth=depth + 1)
            if nested is None and item is not None:
                return None
            copied[key] = nested
        return copied
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_LOCATOR_ITEMS:
            return None
        copied_items: list[object] = []
        for item in value:
            nested = _safe_locator_value(item, depth=depth + 1)
            if nested is None and item is not None:
                return None
            copied_items.append(nested)
        return copied_items
    if isinstance(value, str):
        if len(value) > MAX_LOCATOR_STRING_CHARS or "\x00" in value:
            return None
        return value
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None
