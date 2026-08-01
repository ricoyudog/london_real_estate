"""Shared HTTP and result helpers for public datasources."""

from __future__ import annotations

import json
import http.client
import socket
import ssl
from datetime import UTC, datetime
from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable, Iterable, Mapping, Protocol, TypedDict
from urllib.parse import urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request

from nan_fung.ingestion.policies import (
    SourcePolicy,
    redact_headers,
    redact_url,
    validate_artifact_bytes,
    validate_artifact_file,
    validated_source_addresses,
)
from nan_fung.storage.artifacts import ArtifactStore, StoredArtifact

USER_AGENT = "nan-fung-datasource-research/0.1"
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})


class HostThrottleBlocked(RuntimeError):
    """A request was deferred until a host's durable throttle window expires."""

    def __init__(self, host: str, blocked_until: datetime) -> None:
        self.host = host
        self.blocked_until = blocked_until.astimezone(UTC)
        super().__init__(
            f"host {host!r} is throttled until {self.blocked_until.isoformat()}"
        )


class HostRequestGate(Protocol):
    """Permit one host request and retain its response throttle metadata."""

    def permit(self, host: str, *, continuation: bool = False) -> None:
        """Permit a request, or a redirect continuation of one acquisition."""

    def record_response(
        self, host: str, *, status: int, retry_after: str | None
    ) -> datetime | None:
        """Record response information used to calculate later permits."""


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    """TLS connection that uses a policy-validated IP but hostname SNI."""

    def __init__(
        self, host: str, *, peer_ip: str, port: int, timeout: float
    ) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._peer_ip = peer_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._peer_ip, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedResponse:
    """Minimal response adapter used by the acquisition boundary and tests."""

    def __init__(
        self, response: http.client.HTTPResponse, connection: http.client.HTTPConnection, url: str) -> None:
        self._response = response
        self._connection = connection
        self._url = url
        self.status = response.status
        self.headers = response.headers

    def __enter__(self) -> _PinnedResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def read(self, size: int = -1) -> bytes:
        return self._response.read(size)

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self._response.close()
        self._connection.close()


class _DeadlineReader:
    """Fail a streamed body once its total monotonic deadline has elapsed."""

    def __init__(
        self,
        response: object,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> None:
        self._response = response
        self._deadline = deadline
        self._clock = clock

    def read(self, size: int = -1) -> bytes:
        if self._clock() >= self._deadline:
            raise TimeoutError("streamed acquisition exceeded its total deadline")
        read = getattr(self._response, "read")
        chunk = read(size)
        if self._clock() >= self._deadline:
            raise TimeoutError("streamed acquisition exceeded its total deadline")
        return chunk


def _open_once(request: Request, timeout: int):
    """Open exactly one request against its already-validated resolved IPs.

    The URL hostname remains the HTTPS SNI and certificate-validation name;
    DNS is never re-resolved after the policy check.
    """

    parsed = urlparse(request.full_url)
    peer_addresses = tuple(getattr(request, "_nan_fung_peer_addresses", ()))
    if not peer_addresses:
        raise ValueError("acquisition transport requires validated peer addresses")
    if not parsed.hostname:
        raise ValueError("acquisition URL must have a hostname")
    target = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
    headers = dict(request.header_items())
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    headers.setdefault(
        "Host", parsed.hostname if port == default_port else f"{parsed.hostname}:{port}"
    )
    last_error: OSError | None = None
    for peer_ip in peer_addresses:
        connection: http.client.HTTPConnection
        if parsed.scheme == "https":
            connection = _PinnedHttpsConnection(
                parsed.hostname, peer_ip=peer_ip, port=port, timeout=timeout
            )
        elif parsed.scheme == "http":
            connection = http.client.HTTPConnection(peer_ip, port=port, timeout=timeout)
        else:
            raise ValueError("unsupported acquisition URL scheme")
        try:
            connection.request(request.get_method(), target, body=request.data, headers=headers)
            return _PinnedResponse(connection.getresponse(), connection, request.full_url)
        except OSError as error:
            connection.close()
            last_error = error
    raise OSError("all validated peer addresses failed") from last_error


@dataclass(frozen=True)
class AcquisitionResponse:
    """A bounded, replayable description of one HTTP acquisition.

    This object deliberately carries response metadata which the legacy
    ``get_bytes`` helper used to discard.  The ingestion workflow persists the
    body before parsing it; legacy datasource functions may continue to use
    ``get_bytes`` during the compatibility transition.
    """

    request_url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    retrieved_at: str
    method: str

    @property
    def redacted_request_url(self) -> str:
        return redact_url(self.request_url)

    @property
    def redacted_final_url(self) -> str:
        return redact_url(self.final_url)


@dataclass(frozen=True)
class AcquisitionMetadata:
    """Response metadata that is available before a body is published."""

    request_url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    retrieved_at: str
    method: str

    @property
    def redacted_request_url(self) -> str:
        return redact_url(self.request_url)

    @property
    def redacted_final_url(self) -> str:
        return redact_url(self.final_url)


@dataclass(frozen=True)
class StoredAcquisitionResponse:
    """A streamed acquisition whose verified body already lives in the CAS."""

    request_url: str
    final_url: str
    status: int
    headers: Mapping[str, str]
    artifact: StoredArtifact
    retrieved_at: str
    method: str

    @property
    def redacted_request_url(self) -> str:
        return redact_url(self.request_url)

    @property
    def redacted_final_url(self) -> str:
        return redact_url(self.final_url)


class SourceResult(TypedDict):
    """JSON-serializable result returned by every datasource function."""

    category: str
    source: str
    source_url: str
    retrieved_at: str
    published_at: str | None
    source_updated_at: str | None
    records: list[dict[str, Any]]


def build_url(url: str, params: Mapping[str, Any] | None = None) -> str:
    """Build a request URL without changing pre-existing query parameters."""

    if not params:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params, doseq=True)}"


def _throttle_host(host: str) -> str:
    """Match the durable throttle's host identity for redirect continuations."""

    return host.strip().rstrip(".").lower()


def acquire(
    url: str,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: int = 30,
    *,
    method: str = "GET",
    body: bytes | None = None,
    allowed_hosts: Iterable[str] | None = None,
    max_bytes: int = 32 * 1024 * 1024,
    policy: SourcePolicy | None = None,
    resolver: Callable[[str], Iterable[str]] | None = None,
    host_gate: HostRequestGate | None = None,
    require_full_response: bool = False,
) -> AcquisitionResponse:
    """Acquire a bounded HTTP response with policy hooks for ingestion.

    A source policy is mandatory.  The helper neither stores credentials nor
    returns them in its metadata.
    """

    if policy is None:
        raise ValueError("acquisition requires a source policy")
    request_url = build_url(url, params)
    allowed_host_set = frozenset(_throttle_host(host) for host in allowed_hosts or ())

    def validate_target(target_url: str) -> tuple[str, ...]:
        parsed = urlparse(target_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("acquisition URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("acquisition URL must not contain userinfo")
        if allowed_host_set and _throttle_host(parsed.hostname) not in allowed_host_set:
            raise ValueError(f"host is not allowed: {parsed.hostname}")
        return validated_source_addresses(
            target_url, policy, method=method, resolver=resolver
        )

    peer_addresses = validate_target(request_url)
    max_bytes = min(max_bytes, policy.artifact.max_bytes)
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    if not {
        header.lower() for header in request_headers
    }.issubset(policy.allowed_request_headers):
        raise ValueError("request header is not allowed by source policy")

    redirect_limit = policy.max_redirects
    current_url = request_url
    redirects = 0
    permitted_hosts: set[str] = set()
    while True:
        raw_host = urlparse(current_url).hostname
        if raw_host is None:  # ``validate_target`` already rejects this case.
            raise ValueError("acquisition URL must have a hostname")
        host = _throttle_host(raw_host)
        if host_gate is not None:
            host_gate.permit(host, continuation=host in permitted_hosts)
            permitted_hosts.add(host)
        request = Request(current_url, data=body, headers=request_headers, method=method)
        setattr(request, "_nan_fung_peer_addresses", peer_addresses)
        response = _open_once(request, timeout)
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        if host_gate is not None:
            try:
                retry_after = response.headers.get("Retry-After")
                blocked_until = host_gate.record_response(
                    host,
                    status=status,
                    retry_after=retry_after if isinstance(retry_after, str) else None,
                )
            except BaseException:
                close = getattr(response, "close", None)
                if close is not None:
                    close()
                raise
            if status == 429 and blocked_until is not None:
                close = getattr(response, "close", None)
                if close is not None:
                    close()
                raise HostThrottleBlocked(host, blocked_until)
        if status in _REDIRECT_STATUS_CODES:
            location = response.headers.get("Location")
            close = getattr(response, "close", None)
            if close is not None:
                close()
            if not location:
                raise ValueError("redirect response is missing Location")
            if method.upper() != "GET":
                raise ValueError("redirected non-GET requests are not supported")
            if redirects >= redirect_limit:
                raise ValueError("redirect limit exceeded")
            next_url = urljoin(current_url, location)
            # This validation happens before the next transport operation, so
            # an allowlisted host cannot bounce us to an internal target.
            peer_addresses = validate_target(next_url)
            current_url = next_url
            redirects += 1
            continue
        if not 200 <= status < 300:
            close = getattr(response, "close", None)
            if close is not None:
                close()
            raise ValueError("acquisition requires a successful HTTP response")
        if require_full_response and (
            status != 200
            or any(name.lower() == "content-range" for name in response.headers)
        ):
            close = getattr(response, "close", None)
            if close is not None:
                close()
            raise ValueError("acquisition requires a complete HTTP 200 response")
        with response:
            chunks: list[bytes] = []
            received = 0
            while chunk := response.read(min(64 * 1024, max_bytes - received + 1)):
                received += len(chunk)
                if received > max_bytes:
                    raise ValueError(f"response exceeds {max_bytes} byte limit")
                chunks.append(chunk)
            final_url = response.geturl()
            if final_url != current_url:
                raise ValueError("transport returned an unvalidated final URL")
            artifact = b"".join(chunks)
            validate_artifact_bytes(
                artifact,
                media_type=response.headers.get("Content-Type"),
                policy=policy.artifact,
            )
            return AcquisitionResponse(
                request_url=request_url,
                final_url=final_url,
                status=status,
                headers=redact_headers(dict(response.headers.items())),
                body=artifact,
                retrieved_at=datetime.now(UTC).isoformat(),
                method=method,
            )


def acquire_to_artifact(
    url: str,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: int = 30,
    *,
    artifact_store: ArtifactStore,
    method: str = "GET",
    body: bytes | None = None,
    allowed_hosts: Iterable[str] | None = None,
    max_bytes: int = 32 * 1024 * 1024,
    policy: SourcePolicy | None = None,
    resolver: Callable[[str], Iterable[str]] | None = None,
    host_gate: HostRequestGate | None = None,
    continuation_hosts: Iterable[str] = (),
    before_publish: Callable[[AcquisitionMetadata], None] | None = None,
    max_stream_seconds: float | None = None,
    monotonic_clock: Callable[[], float] = monotonic,
    require_full_response: bool = False,
) -> StoredAcquisitionResponse:
    """Stream an approved HTTP response into CAS without buffering its body.

    Unlike :func:`acquire`, this additive path never materializes response
    chunks in memory.  Artifact policy validation runs on the private fsynced
    temporary file before its atomic CAS publication.  A trusted workflow may
    pass hosts from an immediately preceding selected artifact as
    ``continuation_hosts``; this preserves one logical multi-stage acquisition
    while the durable gate still rejects any active 429 block.
    """

    if policy is None:
        raise ValueError("acquisition requires a source policy")
    if max_stream_seconds is not None and max_stream_seconds <= 0:
        raise ValueError("max_stream_seconds must be positive")
    stream_deadline = (
        monotonic_clock() + max_stream_seconds
        if max_stream_seconds is not None
        else None
    )
    request_url = build_url(url, params)
    allowed_host_set = frozenset(_throttle_host(host) for host in allowed_hosts or ())

    def validate_target(target_url: str) -> tuple[str, ...]:
        parsed = urlparse(target_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("acquisition URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("acquisition URL must not contain userinfo")
        if allowed_host_set and _throttle_host(parsed.hostname) not in allowed_host_set:
            raise ValueError(f"host is not allowed: {parsed.hostname}")
        return validated_source_addresses(
            target_url, policy, method=method, resolver=resolver
        )

    peer_addresses = validate_target(request_url)
    max_bytes = min(max_bytes, policy.artifact.max_bytes)
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    if not {
        header.lower() for header in request_headers
    }.issubset(policy.allowed_request_headers):
        raise ValueError("request header is not allowed by source policy")

    redirect_limit = policy.max_redirects
    current_url = request_url
    redirects = 0
    permitted_hosts = {_throttle_host(host) for host in continuation_hosts}
    while True:
        raw_host = urlparse(current_url).hostname
        if raw_host is None:  # ``validate_target`` already rejects this case.
            raise ValueError("acquisition URL must have a hostname")
        host = _throttle_host(raw_host)
        if host_gate is not None:
            host_gate.permit(host, continuation=host in permitted_hosts)
            permitted_hosts.add(host)
        request = Request(current_url, data=body, headers=request_headers, method=method)
        setattr(request, "_nan_fung_peer_addresses", peer_addresses)
        response = _open_once(request, timeout)
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        if host_gate is not None:
            try:
                retry_after = response.headers.get("Retry-After")
                blocked_until = host_gate.record_response(
                    host,
                    status=status,
                    retry_after=retry_after if isinstance(retry_after, str) else None,
                )
            except BaseException:
                close = getattr(response, "close", None)
                if close is not None:
                    close()
                raise
            if status == 429 and blocked_until is not None:
                close = getattr(response, "close", None)
                if close is not None:
                    close()
                raise HostThrottleBlocked(host, blocked_until)
        if status in _REDIRECT_STATUS_CODES:
            location = response.headers.get("Location")
            close = getattr(response, "close", None)
            if close is not None:
                close()
            if not location:
                raise ValueError("redirect response is missing Location")
            if method.upper() != "GET":
                raise ValueError("redirected non-GET requests are not supported")
            if redirects >= redirect_limit:
                raise ValueError("redirect limit exceeded")
            next_url = urljoin(current_url, location)
            peer_addresses = validate_target(next_url)
            current_url = next_url
            redirects += 1
            continue
        if not 200 <= status < 300:
            close = getattr(response, "close", None)
            if close is not None:
                close()
            raise ValueError("streamed acquisition requires a successful HTTP response")
        if require_full_response and (
            status != 200
            or any(name.lower() == "content-range" for name in response.headers)
        ):
            close = getattr(response, "close", None)
            if close is not None:
                close()
            raise ValueError("acquisition requires a complete HTTP 200 response")
        with response:
            final_url = response.geturl()
            if final_url != current_url:
                raise ValueError("transport returned an unvalidated final URL")
            media_type = response.headers.get("Content-Type")
            if not isinstance(media_type, str):
                media_type = None
            metadata = AcquisitionMetadata(
                request_url=request_url,
                final_url=final_url,
                status=status,
                headers=redact_headers(dict(response.headers.items())),
                retrieved_at=datetime.now(UTC).isoformat(),
                method=method,
            )
            stream = (
                _DeadlineReader(
                    response,
                    deadline=stream_deadline,
                    clock=monotonic_clock,
                )
                if stream_deadline is not None
                else response
            )
            artifact = artifact_store.put_stream(
                stream,
                media_type=media_type,
                max_bytes=max_bytes,
                validator=lambda path, byte_size: validate_artifact_file(
                    path,
                    byte_size=byte_size,
                    media_type=media_type,
                    policy=policy.artifact,
                ),
                before_publish=(
                    (lambda: before_publish(metadata))
                    if before_publish is not None
                    else None
                ),
            )
            return StoredAcquisitionResponse(
                request_url=metadata.request_url,
                final_url=metadata.final_url,
                status=metadata.status,
                headers=metadata.headers,
                artifact=artifact,
                retrieved_at=metadata.retrieved_at,
                method=metadata.method,
            )


def get_bytes(
    url: str,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: int = 30,
    *,
    policy: SourcePolicy,
    resolver: Callable[[str], Iterable[str]] | None = None,
) -> bytes:
    """Return bytes from a public endpoint through the acquisition seam."""

    return acquire(
        url, params, headers, timeout, policy=policy, resolver=resolver
    ).body


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
    *,
    policy: SourcePolicy,
    resolver: Callable[[str], Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Return a decoded JSON object from a public HTTP endpoint."""

    return json.loads(
        get_bytes(url, params, headers, timeout, policy=policy, resolver=resolver)
    )


def post_json(
    url: str,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    timeout: int = 30,
    policy: SourcePolicy,
    resolver: Callable[[str], Iterable[str]] | None = None,
) -> dict[str, Any]:
    """POST JSON through the same metadata-preserving acquisition boundary."""

    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    response = acquire(
        url,
        headers=request_headers,
        timeout=timeout,
        method="POST",
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        policy=policy,
        resolver=resolver,
    )
    return json.loads(response.body)


def source_result(
    *,
    category: str,
    source: str,
    source_url: str,
    records: list[dict[str, Any]],
    published_at: str | None = None,
    source_updated_at: str | None = None,
) -> SourceResult:
    """Build the common JSON result envelope."""

    return {
        "category": category,
        "source": source,
        "source_url": source_url,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "published_at": published_at,
        "source_updated_at": source_updated_at,
        "records": records,
    }
