"""Strict wire protocol shared by the agent-tool executable and its host.

The protocol deliberately has one bounded JSON document in each direction.
Nothing in this module writes diagnostics: callers may log safe operational
information to stderr, but stdout is reserved for :func:`write_result`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import io
import json
from collections.abc import Mapping
from typing import BinaryIO


REQUEST_SCHEMA_VERSION = "agent_tool_request.v1"
RESULT_SCHEMA_VERSION = "agent_tool_result.v1"
MAX_STDIN_BYTES = 64 * 1024
MAX_STDOUT_BYTES = 256 * 1024
MAX_STDERR_BYTES = 64 * 1024

EXIT_OK = 0
EXIT_INVALID_REQUEST = 2
EXIT_ACCESS_DENIED = 3
EXIT_RETRYABLE_UNAVAILABLE = 4
EXIT_INTERNAL_FAILURE = 5
EXIT_PROTOCOL_VIOLATION = 6


_ERROR_DETAILS: dict[str, tuple[int, bool, str]] = {
    "INVALID_ARGUMENT": (EXIT_INVALID_REQUEST, False, "The request arguments are invalid."),
    "INVALID_CURSOR": (EXIT_INVALID_REQUEST, False, "The cursor is invalid or expired."),
    "ACCESS_DENIED": (EXIT_ACCESS_DENIED, False, "Access to this capability is denied."),
    "CAPABILITY_BLOCKED": (EXIT_ACCESS_DENIED, False, "This capability is not available."),
    "POLICY_DENIED": (EXIT_ACCESS_DENIED, False, "The request is not allowed by policy."),
    "RETRYABLE_UNAVAILABLE": (
        EXIT_RETRYABLE_UNAVAILABLE,
        True,
        "The requested service is temporarily unavailable.",
    ),
    "TIMEOUT": (EXIT_RETRYABLE_UNAVAILABLE, True, "The tool call timed out."),
    "INTERNAL_ERROR": (EXIT_INTERNAL_FAILURE, False, "The tool could not complete safely."),
    "SCHEMA_VIOLATION": (EXIT_PROTOCOL_VIOLATION, False, "The request schema is invalid."),
    "PROTOCOL_ERROR": (EXIT_PROTOCOL_VIOLATION, False, "The tool protocol was violated."),
    "RESULT_TOO_LARGE": (
        EXIT_PROTOCOL_VIOLATION,
        False,
        "The tool result exceeds the response limit.",
    ),
}


class AgentToolError(ValueError):
    """A safe, stable error that can cross the process boundary."""

    code = "INTERNAL_ERROR"

    def __init__(self, message: str | None = None) -> None:
        # The supplied message is useful in a local traceback but must never be
        # rendered into the model-facing result.  ``safe_message`` is fixed.
        super().__init__(message or self.safe_message)

    @property
    def exit_code(self) -> int:
        return _ERROR_DETAILS[self.code][0]

    @property
    def retryable(self) -> bool:
        return _ERROR_DETAILS[self.code][1]

    @property
    def safe_message(self) -> str:
        return _ERROR_DETAILS[self.code][2]


class InvalidArgument(AgentToolError):
    code = "INVALID_ARGUMENT"


class InvalidCursor(AgentToolError):
    code = "INVALID_CURSOR"


class AccessDenied(AgentToolError):
    code = "ACCESS_DENIED"


class CapabilityBlocked(AgentToolError):
    code = "CAPABILITY_BLOCKED"


class PolicyDenied(AgentToolError):
    code = "POLICY_DENIED"


class RetryableUnavailable(AgentToolError):
    code = "RETRYABLE_UNAVAILABLE"


class Timeout(AgentToolError):
    code = "TIMEOUT"


class InternalError(AgentToolError):
    code = "INTERNAL_ERROR"


class ProtocolError(AgentToolError):
    code = "PROTOCOL_ERROR"


class SchemaViolation(ProtocolError):
    code = "SCHEMA_VIOLATION"


class ResultTooLarge(ProtocolError):
    code = "RESULT_TOO_LARGE"


@dataclass(frozen=True)
class HostContext:
    """Trusted per-call capability context; never model-controlled arguments."""

    principal: str
    capability_scope_id: str
    turn_id: str
    tool_call_id: str
    allowed_access_classes: frozenset[str]
    allowed_capability_ids: frozenset[str]
    allowed_refresh_profiles: frozenset[str]
    refresh_request_id: str | None = None


@dataclass(frozen=True)
class AgentToolRequest:
    request_id: str
    arguments: Mapping[str, object]
    host_context: HostContext


_TOP_LEVEL_FIELDS = frozenset(
    {"schema_version", "request_id", "arguments", "host_context"}
)
_HOST_CONTEXT_FIELDS = frozenset(
    {
        "principal",
        "capability_scope_id",
        "turn_id",
        "tool_call_id",
        "refresh_request_id",
        "allowed_access_classes",
        "allowed_capability_ids",
        "allowed_refresh_profiles",
    }
)


def read_request(stream: BinaryIO, *, maximum_bytes: int = MAX_STDIN_BYTES) -> dict[str, object]:
    """Read one UTF-8 JSON object and reject trailing non-whitespace bytes."""

    if maximum_bytes < 1:
        raise ValueError("maximum_bytes must be positive")
    value = _read_json_object(stream, maximum_bytes=maximum_bytes)
    # The byte-level reader is intentionally also strict about the common
    # envelope, so a host cannot mistake arbitrary JSON for a valid request.
    parse_request(value)
    return value


def parse_request(value: Mapping[str, object]) -> AgentToolRequest:
    """Validate the envelope independently from any particular tool selector."""

    unknown = set(value) - _TOP_LEVEL_FIELDS
    missing = _TOP_LEVEL_FIELDS - set(value)
    if unknown or missing:
        raise SchemaViolation("request fields do not match the v1 schema")
    if value.get("schema_version") != REQUEST_SCHEMA_VERSION:
        raise SchemaViolation("unsupported request schema")
    request_id = _required_text(value.get("request_id"), "request_id")
    if len(request_id) > 256:
        raise SchemaViolation("request_id is too long")
    arguments = value.get("arguments")
    if not isinstance(arguments, Mapping):
        raise SchemaViolation("arguments must be an object")
    host_value = value.get("host_context")
    if not isinstance(host_value, Mapping):
        raise SchemaViolation("host_context must be an object")
    host_context = parse_host_context(host_value)
    return AgentToolRequest(
        request_id=request_id,
        arguments=dict(arguments),
        host_context=host_context,
    )


def parse_host_context(value: Mapping[str, object]) -> HostContext:
    """Validate the fixed host context and reject policy-field injection."""

    unknown = set(value) - _HOST_CONTEXT_FIELDS
    required = _HOST_CONTEXT_FIELDS - {"refresh_request_id"}
    missing = required - set(value)
    if unknown or missing:
        raise SchemaViolation("host_context fields do not match the v1 schema")
    principal = _required_text(value.get("principal"), "principal")
    scope = _required_text(value.get("capability_scope_id"), "capability_scope_id")
    turn_id = _required_text(value.get("turn_id"), "turn_id")
    tool_call_id = _required_text(value.get("tool_call_id"), "tool_call_id")
    for item in (principal, scope, turn_id, tool_call_id):
        if len(item) > 256:
            raise SchemaViolation("host_context value is too long")
    refresh_request_id = value.get("refresh_request_id")
    if refresh_request_id is not None:
        refresh_request_id = _required_text(refresh_request_id, "refresh_request_id")
        if len(refresh_request_id) > 256:
            raise SchemaViolation("refresh_request_id is too long")
    return HostContext(
        principal=principal,
        capability_scope_id=scope,
        turn_id=turn_id,
        tool_call_id=tool_call_id,
        allowed_access_classes=_string_set(value.get("allowed_access_classes"), "allowed_access_classes"),
        allowed_capability_ids=_string_set(
            value.get("allowed_capability_ids"), "allowed_capability_ids", allow_empty=True
        ),
        allowed_refresh_profiles=_string_set(
            value.get("allowed_refresh_profiles"), "allowed_refresh_profiles", allow_empty=True
        ),
        refresh_request_id=refresh_request_id,
    )


def result(
    request_id: str | None,
    *,
    status: str,
    data: object = None,
    warnings: list[object] | tuple[object, ...] = (),
    error: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the only model-facing result envelope."""

    if status not in {"ok", "partial", "error"}:
        raise ValueError("status must be ok, partial, or error")
    if status == "error":
        if error is None:
            raise ValueError("error results need an error object")
        data = None
    elif error is not None:
        raise ValueError("successful results cannot contain error details")
    checked_warnings = list(warnings)
    if not all(isinstance(item, (str, Mapping)) for item in checked_warnings):
        raise ValueError("warnings must be strings or objects")
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "request_id": request_id,
        "status": status,
        "data": data,
        "warnings": checked_warnings,
        "error": dict(error) if error is not None else None,
    }


def error_result(request_id: str | None, error: AgentToolError) -> dict[str, object]:
    """Render only stable, non-sensitive fields from an exception."""

    return result(
        _safe_request_id(request_id),
        status="error",
        error={
            "code": error.code,
            "message": error.safe_message,
            "retryable": error.retryable,
        },
    )


def exit_code_for_result(value: Mapping[str, object]) -> int:
    """Map a schema-valid result envelope to the documented process code."""

    if value.get("status") in {"ok", "partial"}:
        return EXIT_OK
    error = value.get("error")
    if isinstance(error, Mapping) and isinstance(error.get("code"), str):
        details = _ERROR_DETAILS.get(error["code"])
        if details is not None:
            return details[0]
    return EXIT_INTERNAL_FAILURE


def write_result(
    stream: BinaryIO,
    value: Mapping[str, object],
    *,
    maximum_bytes: int = MAX_STDOUT_BYTES,
) -> None:
    """Write exactly one bounded UTF-8 JSON result document to a binary stream."""

    if maximum_bytes < 1:
        raise ValueError("maximum_bytes must be positive")
    validate_result(value)
    try:
        payload = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ProtocolError("result is not JSON serializable") from error
    if len(payload) > maximum_bytes:
        raise ResultTooLarge("result exceeds response bound")
    stream.write(payload)
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


def read_result(stream: BinaryIO, *, maximum_bytes: int = MAX_STDOUT_BYTES) -> dict[str, object]:
    """Read and validate one child stdout result without exposing child output."""

    value = _read_json_object(stream, maximum_bytes=maximum_bytes)
    validate_result(value)
    return value


def validate_result(value: Mapping[str, object]) -> None:
    fields = {"schema_version", "request_id", "status", "data", "warnings", "error"}
    if set(value) != fields or value.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ProtocolError("child result schema is invalid")
    if value.get("status") not in {"ok", "partial", "error"}:
        raise ProtocolError("child result status is invalid")
    request_id = value.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or not request_id or len(request_id) > 256
    ):
        raise ProtocolError("child result request_id is invalid")
    if not isinstance(value.get("warnings"), list):
        raise ProtocolError("child result warnings are invalid")
    if not all(isinstance(item, (str, Mapping)) for item in value["warnings"]):
        raise ProtocolError("child result warnings are invalid")
    status = value["status"]
    error = value.get("error")
    if status == "error":
        if not isinstance(error, Mapping):
            raise ProtocolError("error result lacks error object")
        if value.get("data") is not None:
            raise ProtocolError("error result contains data")
        if set(error) != {"code", "message", "retryable"}:
            raise ProtocolError("child error schema is invalid")
        if error.get("code") not in _ERROR_DETAILS:
            raise ProtocolError("child error code is invalid")
        if not isinstance(error.get("message"), str) or not isinstance(error.get("retryable"), bool):
            raise ProtocolError("child error details are invalid")
        expected_exit, expected_retryable, expected_message = _ERROR_DETAILS[error["code"]]
        if (
            error["message"] != expected_message
            or error["retryable"] is not expected_retryable
            or expected_exit != exit_code_for_result(value)
        ):
            raise ProtocolError("child error details are not stable")
    elif error is not None:
        raise ProtocolError("success result contains error")


def utc_timestamp(value: datetime) -> str:
    """Serialize an aware timestamp in the wire contract's RFC3339 UTC form."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _read_bounded(
    stream: BinaryIO,
    maximum_bytes: int,
    *,
    oversized: type[AgentToolError],
) -> bytes:
    # Binary pipes are allowed to return short reads.  Keep reading only up to
    # one byte beyond the contract boundary so a slow child cannot make the
    # parser mistake a partial document for EOF or grow memory without bound.
    chunks: list[bytes] = []
    remaining = maximum_bytes + 1
    while remaining:
        chunk = stream.read(min(65_536, remaining))
        if isinstance(chunk, str):
            raise TypeError("protocol streams must be binary")
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > maximum_bytes:
        raise oversized("protocol document exceeds its byte limit")
    return payload


def _read_json_object(stream: BinaryIO, *, maximum_bytes: int) -> dict[str, object]:
    payload = _read_bounded(stream, maximum_bytes, oversized=ProtocolError)
    if not payload:
        raise ProtocolError("protocol body is empty")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ProtocolError("protocol body is not UTF-8") from error
    try:
        decoder = json.JSONDecoder(
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_non_json_constant,
        )
        value, end = decoder.raw_decode(text)
    except (json.JSONDecodeError, ValueError) as error:
        raise ProtocolError("protocol body is not valid JSON") from error
    if text[end:].strip():
        raise ProtocolError("protocol body has trailing bytes")
    if not isinstance(value, dict):
        raise ProtocolError("protocol body must be a JSON object")
    return value


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> object:
    raise ValueError(f"JSON constant {value!r} is not permitted")


def _safe_request_id(value: object) -> str | None:
    if isinstance(value, str) and value and len(value) <= 256:
        return value
    return None


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaViolation(f"{name} must be a non-empty string")
    return value


def _string_set(value: object, name: str, *, allow_empty: bool = False) -> frozenset[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise SchemaViolation(f"{name} must be a non-empty list")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 256 for item in value):
        raise SchemaViolation(f"{name} must contain bounded strings")
    if len(set(value)) != len(value):
        raise SchemaViolation(f"{name} must not contain duplicates")
    return frozenset(value)


__all__ = [
    "AgentToolError",
    "AgentToolRequest",
    "CapabilityBlocked",
    "EXIT_ACCESS_DENIED",
    "EXIT_INTERNAL_FAILURE",
    "EXIT_INVALID_REQUEST",
    "EXIT_OK",
    "EXIT_PROTOCOL_VIOLATION",
    "EXIT_RETRYABLE_UNAVAILABLE",
    "HostContext",
    "InternalError",
    "InvalidArgument",
    "InvalidCursor",
    "MAX_STDERR_BYTES",
    "MAX_STDIN_BYTES",
    "MAX_STDOUT_BYTES",
    "PolicyDenied",
    "ProtocolError",
    "REQUEST_SCHEMA_VERSION",
    "RESULT_SCHEMA_VERSION",
    "ResultTooLarge",
    "RetryableUnavailable",
    "SchemaViolation",
    "Timeout",
    "error_result",
    "exit_code_for_result",
    "parse_host_context",
    "parse_request",
    "read_request",
    "read_result",
    "result",
    "utc_timestamp",
    "validate_result",
    "write_result",
]
