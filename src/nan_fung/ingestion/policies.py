"""Source policy, request redaction, and artifact safety helpers.

These functions are deliberately side-effect free except for optional DNS
resolution.  Acquisition code can use them before making a request and before
handing an artifact to a parser.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import stat
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import ParseResult, parse_qsl, urlencode, urlparse, urlunparse
from zipfile import BadZipFile, ZipFile, ZipInfo
from io import BytesIO

from .canonical import REDACTED_VALUE, freeze_json, thaw_json


class PolicyError(ValueError):
    """Raised when a request or artifact violates a source policy."""


_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"token|secret|signature|password|passwd|credential|proxy-authorization)",
    re.IGNORECASE,
)
_SAFE_PERSISTED_HEADERS = frozenset(
    {
        "accept",
        "content-length",
        "content-type",
        "etag",
        "if-modified-since",
        "if-none-match",
        "last-modified",
        "location",
        "range",
        "retry-after",
        "user-agent",
    }
)


@dataclass(frozen=True, slots=True)
class ArtifactPolicy:
    """Bounds that must be applied before parsing an untrusted artifact."""

    max_bytes: int = 25 * 1024 * 1024
    allowed_media_types: tuple[str, ...] = ()
    max_archive_members: int = 1_000
    max_expanded_bytes: int = 1_024 * 1024 * 1024
    max_compression_ratio: int = 100

    def __post_init__(self) -> None:
        if self.max_bytes < 0:
            raise PolicyError("max_bytes must be non-negative")
        if self.max_archive_members < 1:
            raise PolicyError("max_archive_members must be at least one")
        if self.max_expanded_bytes < 0:
            raise PolicyError("max_expanded_bytes must be non-negative")
        if self.max_compression_ratio < 1:
            raise PolicyError("max_compression_ratio must be at least one")

    def as_json(self) -> dict[str, Any]:
        return {
            "max_bytes": self.max_bytes,
            "allowed_media_types": list(self.allowed_media_types),
            "max_archive_members": self.max_archive_members,
            "max_expanded_bytes": self.max_expanded_bytes,
            "max_compression_ratio": self.max_compression_ratio,
        }


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """Executable request boundary for one versioned datasource definition."""

    allowed_hosts: tuple[str, ...]
    allowed_methods: tuple[str, ...] = ("GET",)
    allowed_query_keys: tuple[str, ...] = ()
    allowed_request_headers: tuple[str, ...] = ("accept", "user-agent")
    require_https: bool = True
    max_redirects: int = 5
    artifact: ArtifactPolicy = field(default_factory=ArtifactPolicy)

    def __post_init__(self) -> None:
        hosts = tuple(_normalize_host(host) for host in self.allowed_hosts)
        if not hosts:
            raise PolicyError("at least one allowed host is required")
        if any(not host for host in hosts):
            raise PolicyError("allowed hosts cannot be empty")
        methods = tuple(method.upper() for method in self.allowed_methods)
        if not methods:
            raise PolicyError("at least one allowed method is required")
        if self.max_redirects < 0 or self.max_redirects > 5:
            raise PolicyError("max_redirects must be between 0 and 5")
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "allowed_methods", methods)
        object.__setattr__(
            self,
            "allowed_query_keys",
            tuple(key.lower() for key in self.allowed_query_keys),
        )
        object.__setattr__(
            self,
            "allowed_request_headers",
            tuple(header.lower() for header in self.allowed_request_headers),
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "allowed_hosts": list(self.allowed_hosts),
            "allowed_methods": list(self.allowed_methods),
            "allowed_query_keys": list(self.allowed_query_keys),
            "allowed_request_headers": list(self.allowed_request_headers),
            "require_https": self.require_https,
            "max_redirects": self.max_redirects,
            "artifact": self.artifact.as_json(),
        }


def _normalize_host(value: str) -> str:
    if not isinstance(value, str):
        raise PolicyError("host must be a string")
    host = value.strip().rstrip(".").lower()
    if host.startswith("*."):
        if host == "*." or "." not in host[2:]:
            raise PolicyError(f"invalid wildcard host: {value!r}")
        return host
    if not host or "/" in host or ":" in host or "@" in host:
        raise PolicyError(f"invalid host: {value!r}")
    return host


def _host_allowed(host: str, allowed_hosts: Sequence[str]) -> bool:
    normalized = _normalize_host(host)
    for allowed in allowed_hosts:
        if allowed.startswith("*."):
            suffix = allowed[1:]
            if normalized.endswith(suffix) and normalized != suffix[1:]:
                return True
        elif normalized == allowed:
            return True
    return False


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global and not address.is_multicast


def _default_resolver(host: str) -> Iterable[str]:
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as error:
        raise PolicyError(f"could not resolve allowed host: {host}") from error
    return {result[4][0] for result in results}


def _resolve_public_addresses(
    host: str, resolver: Callable[[str], Iterable[str]]
) -> tuple[str, ...]:
    """Resolve one approved host once and retain only safe peer addresses."""

    addresses = tuple(resolver(host))
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise PolicyError("URL resolves to a non-public address")
    return addresses


def validate_request_url(
    url: str,
    policy: SourcePolicy,
    *,
    method: str = "GET",
    resolver: Callable[[str], Iterable[str]] | None = _default_resolver,
) -> ParseResult:
    """Validate a request or redirect destination against source policy.

    Host allowlisting is checked before DNS resolution, and every resolved
    address must be public.  Passing ``resolver=None`` is reserved for an
    already trusted transport that performs equivalent address checks.
    """

    if not isinstance(url, str) or not url:
        raise PolicyError("URL is required")
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise PolicyError("URL userinfo is not allowed")
    if parsed.fragment:
        raise PolicyError("URL fragments are not valid acquisition targets")
    if policy.require_https and parsed.scheme.lower() != "https":
        raise PolicyError("HTTPS is required by source policy")
    if parsed.scheme.lower() not in {"https", "http"}:
        raise PolicyError("unsupported URL scheme")
    if not parsed.hostname:
        raise PolicyError("URL hostname is required")
    try:
        literal_address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None and not _is_public_address(str(literal_address)):
        raise PolicyError("URL uses a private, loopback, or otherwise unsafe IP literal")
    if parsed.port not in (None, 443):
        raise PolicyError("non-default URL port is not allowed")
    if not _host_allowed(parsed.hostname, policy.allowed_hosts):
        raise PolicyError("URL host is not allowlisted")
    if method.upper() not in policy.allowed_methods:
        raise PolicyError(f"method is not allowed: {method.upper()}")
    query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if policy.allowed_query_keys and not query_keys.issubset(policy.allowed_query_keys):
        raise PolicyError("URL includes a query key not approved by source policy")
    if resolver is not None:
        _resolve_public_addresses(parsed.hostname, resolver)
    return parsed


def validate_source_url(
    url: str,
    policy: SourcePolicy,
    *,
    method: str = "GET",
    resolver: Callable[[str], Iterable[str]] | None = _default_resolver,
) -> ParseResult:
    """Public acquisition-boundary alias for :func:`validate_request_url`."""

    return validate_request_url(url, policy, method=method, resolver=resolver)


def validated_source_addresses(
    url: str,
    policy: SourcePolicy,
    *,
    method: str = "GET",
    resolver: Callable[[str], Iterable[str]] | None = None,
) -> tuple[str, ...]:
    """Validate a target and resolve its peer addresses exactly once.

    Acquisition transports must connect to one of the returned addresses rather
    than asking the operating system to resolve the hostname a second time.
    This closes the DNS-rebinding gap between policy validation and connect.
    """

    parsed = validate_request_url(url, policy, method=method, resolver=None)
    return _resolve_public_addresses(parsed.hostname, resolver or _default_resolver)


def redact_url(url: str) -> str:
    """Return a persisted URL without credentials or sensitive query values."""

    parsed = urlparse(url)
    query = [
        (key, REDACTED_VALUE if _SENSITIVE_KEY.search(key) else value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    host = parsed.hostname or ""
    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            urlencode(query, doseq=True),
            "",
        )
    )


def redact_headers(
    headers: Mapping[str, str], *, allowed_headers: Sequence[str] = _SAFE_PERSISTED_HEADERS
) -> dict[str, str]:
    """Keep only approved non-secret headers suitable for persistent audit."""

    allowed = {header.lower() for header in allowed_headers}
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered not in allowed:
            continue
        redacted[key] = REDACTED_VALUE if _SENSITIVE_KEY.search(key) else str(value)
    return redacted


def redact_secrets(value: Any) -> Any:
    """Recursively remove known secret fields before persistence or hashing."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            lower = normalized_key.lower()
            if _SENSITIVE_KEY.search(normalized_key):
                result[normalized_key] = REDACTED_VALUE
            elif lower in {"url", "source_url", "final_url", "base_url"} and isinstance(
                item, str
            ):
                result[normalized_key] = redact_url(item)
            elif lower == "headers" and isinstance(item, Mapping):
                result[normalized_key] = redact_headers(
                    {str(header): str(header_value) for header, header_value in item.items()}
                )
            else:
                result[normalized_key] = redact_secrets(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    return value


def frozen_redacted(value: Any) -> Any:
    """Return a deep-frozen redacted audit value."""

    return freeze_json(redact_secrets(value))


def thaw_redacted(value: Any) -> Any:
    """Return a mutable copy for a JSON response."""

    return thaw_json(value)


def validate_artifact_size(byte_size: int, policy: ArtifactPolicy) -> None:
    if byte_size < 0:
        raise PolicyError("artifact byte size cannot be negative")
    if byte_size > policy.max_bytes:
        raise PolicyError(
            f"artifact exceeds policy maximum ({byte_size} > {policy.max_bytes})"
        )


def validate_media_type(media_type: str | None, policy: ArtifactPolicy) -> None:
    if not policy.allowed_media_types or media_type is None:
        return
    normalized = media_type.split(";", 1)[0].strip().lower()
    if normalized not in {item.lower() for item in policy.allowed_media_types}:
        raise PolicyError(f"media type is not allowed: {media_type!r}")


def _unsafe_member_path(name: str) -> bool:
    path = PurePosixPath(name)
    return path.is_absolute() or ".." in path.parts or name.startswith("/") or "\\" in name


def validate_zip_members(members: Iterable[ZipInfo], policy: ArtifactPolicy) -> None:
    """Reject archive traversal, symlink, count, size, and ratio hazards."""

    total_expanded = 0
    count = 0
    for member in members:
        count += 1
        if count > policy.max_archive_members:
            raise PolicyError("archive exceeds member count limit")
        if _unsafe_member_path(member.filename):
            raise PolicyError(f"unsafe archive member path: {member.filename!r}")
        # Unix symlink file type appears in the top 16 permission bits.
        mode = member.external_attr >> 16
        if mode and stat.S_ISLNK(mode):
            raise PolicyError(f"symlink archive member is not allowed: {member.filename!r}")
        total_expanded += member.file_size
        if total_expanded > policy.max_expanded_bytes:
            raise PolicyError("archive exceeds expanded byte limit")
        if member.file_size and member.compress_size == 0:
            raise PolicyError("archive member has an invalid compression size")
        if member.compress_size and member.file_size / member.compress_size > policy.max_compression_ratio:
            raise PolicyError("archive member exceeds compression ratio limit")


def validate_zip_artifact(content: bytes, policy: ArtifactPolicy) -> None:
    """Validate an XLSX/ODS/ZIP container before a parser opens its members."""

    validate_artifact_size(len(content), policy)
    try:
        with ZipFile(BytesIO(content)) as archive:
            validate_zip_members(archive.infolist(), policy)
    except BadZipFile as error:
        raise PolicyError("artifact is not a valid ZIP container") from error


def validate_pdf_artifact(content: bytes, policy: ArtifactPolicy) -> None:
    """Reject malformed or active PDF features before a PDF parser sees bytes."""

    validate_artifact_size(len(content), policy)
    if not content.startswith(b"%PDF-"):
        raise PolicyError("artifact is not a PDF document")
    if content.count(b"/Type /Page") > 500:
        raise PolicyError("PDF exceeds page limit")
    for forbidden in (b"/Encrypt", b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFile"):
        if forbidden in content:
            raise PolicyError("PDF contains an unsupported active or encrypted feature")


def validate_html_artifact(content: bytes, policy: ArtifactPolicy) -> None:
    """Bound raw HTML storage; it is evidence only and is never rendered."""

    validate_artifact_size(len(content), policy)
    if b"\x00" in content:
        raise PolicyError("HTML artifact contains NUL bytes")


def validate_artifact_bytes(
    content: bytes,
    *,
    media_type: str | None,
    policy: ArtifactPolicy,
) -> None:
    """Apply content-type-aware safety checks before CAS persistence or parsing."""

    validate_artifact_size(len(content), policy)
    validate_media_type(media_type, policy)
    normalized = (media_type or "").split(";", 1)[0].strip().lower()
    looks_like_zip = content.startswith(b"PK\x03\x04") or normalized in {
        "application/zip",
        "application/x-zip-compressed",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.oasis.opendocument.spreadsheet",
    }
    if looks_like_zip:
        validate_zip_artifact(content, policy)
    elif content.startswith(b"%PDF-") or normalized == "application/pdf":
        validate_pdf_artifact(content, policy)
    elif normalized in {"text/html", "application/xhtml+xml"}:
        validate_html_artifact(content, policy)


def validate_artifact_file(
    path: Path,
    *,
    byte_size: int,
    media_type: str | None,
    policy: ArtifactPolicy,
) -> None:
    """Apply artifact safety checks to a private file without buffering it.

    Acquisition uses this while an object still lives in the CAS temporary
    directory.  The checks therefore reject unsafe content before the file is
    published or can be referenced by an evidence row.
    """

    try:
        actual_size = path.stat().st_size
    except OSError as error:
        raise PolicyError("artifact temporary file is unavailable") from error
    if actual_size != byte_size:
        raise PolicyError("artifact byte size changed during validation")
    validate_artifact_size(byte_size, policy)
    validate_media_type(media_type, policy)
    normalized = (media_type or "").split(";", 1)[0].strip().lower()
    with path.open("rb") as artifact:
        prefix = artifact.read(8)
    looks_like_zip = prefix.startswith(b"PK\x03\x04") or normalized in {
        "application/zip",
        "application/x-zip-compressed",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.oasis.opendocument.spreadsheet",
    }
    if looks_like_zip:
        try:
            with ZipFile(path) as archive:
                validate_zip_members(archive.infolist(), policy)
        except BadZipFile as error:
            raise PolicyError("artifact is not a valid ZIP container") from error
    elif prefix.startswith(b"%PDF-") or normalized == "application/pdf":
        _validate_pdf_file(path, policy)
    elif normalized in {"text/html", "application/xhtml+xml"}:
        _validate_html_file(path, policy)


def _validate_pdf_file(path: Path, policy: ArtifactPolicy) -> None:
    with path.open("rb") as artifact:
        if not artifact.read(5).startswith(b"%PDF-"):
            raise PolicyError("artifact is not a PDF document")
    counts = _count_file_markers(
        path,
        (b"/Type /Page", b"/Encrypt", b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFile"),
    )
    if counts[b"/Type /Page"] > 500:
        raise PolicyError("PDF exceeds page limit")
    if any(counts[marker] for marker in counts if marker != b"/Type /Page"):
        raise PolicyError("PDF contains an unsupported active or encrypted feature")


def _validate_html_file(path: Path, policy: ArtifactPolicy) -> None:
    del policy  # Size validation was already applied by validate_artifact_file.
    if _count_file_markers(path, (b"\x00",))[b"\x00"]:
        raise PolicyError("HTML artifact contains NUL bytes")


def _count_file_markers(path: Path, markers: Sequence[bytes]) -> dict[bytes, int]:
    """Count short byte markers without materializing the artifact in memory."""

    counts = {marker: 0 for marker in markers}
    overlap = max((len(marker) for marker in markers), default=1) - 1
    tail = b""
    with path.open("rb") as artifact:
        while chunk := artifact.read(64 * 1024):
            window = tail + chunk
            for marker in markers:
                counts[marker] += window.count(marker) - tail.count(marker)
            tail = window[-overlap:] if overlap else b""
    return counts
