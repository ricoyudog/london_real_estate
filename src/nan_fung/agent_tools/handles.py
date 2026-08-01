"""Session-scoped authenticated handles for the model-facing facade.

Only opaque handles are exposed to a model.  Their payloads are authenticated
with a runtime-boot key and additionally bound to principal and session scope.
The host supplies that key to one-shot children through inherited FD 3; it is
never serialised into request JSON, argv, environment, disk, or logs.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import hmac
import json
import os
from types import MappingProxyType
from .protocol import InvalidCursor, PolicyDenied


HANDLE_VERSION = "h1"
HANDLE_KEY_BYTES = 32
DEFAULT_HANDLE_TTL = timedelta(minutes=30)
MAX_HANDLE_BYTES = 8 * 1024


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HandleError(InvalidCursor):
    """Raised for a malformed, tampered, expired, or wrong-context handle."""


class HandleScopeError(PolicyDenied):
    """Raised when a valid handle is replayed in another principal or scope."""


class ScopedHandleCodec:
    """Seal and verify versioned handles with a runtime-boot HMAC secret."""

    def __init__(self, secret: bytes, *, clock: Callable[[], datetime] = _utc_now) -> None:
        if not isinstance(secret, bytes) or not secret:
            raise ValueError("handle secret must be non-empty bytes")
        self._secret = bytes(secret)
        self._clock = clock

    def mint(
        self,
        kind: str,
        *,
        principal: str,
        capability_scope_id: str,
        payload: Mapping[str, object],
        binding: Mapping[str, object] | None = None,
        expires_at: datetime | None = None,
        ttl: timedelta = DEFAULT_HANDLE_TTL,
    ) -> str:
        """Create an opaque handle bound to one trusted session capability."""

        _validate_kind(kind)
        _validate_identity(principal, "principal")
        _validate_identity(capability_scope_id, "capability_scope_id")
        if ttl <= timedelta(0):
            raise ValueError("handle ttl must be positive")
        now = _normalise_utc(self._clock())
        expiry = _normalise_utc(expires_at) if expires_at is not None else now + ttl
        if expiry <= now:
            raise ValueError("handle expiry must be in the future")
        body = {
            "v": HANDLE_VERSION,
            "k": kind,
            "p": principal,
            "s": capability_scope_id,
            "e": _timestamp(expiry),
            "d": _json_object(payload, "payload"),
            "b": _json_object(binding or {}, "binding"),
        }
        encoded = _encode_json(body)
        signature = _sign(self._secret, encoded)
        value = f"{HANDLE_VERSION}.{_b64(encoded)}.{_b64(signature)}"
        if len(value.encode("ascii")) > MAX_HANDLE_BYTES:
            raise ValueError("handle is too large")
        return value

    def verify(
        self,
        value: str,
        kind: str,
        *,
        principal: str,
        capability_scope_id: str,
        binding: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        """Verify version, HMAC, kind, identity, scope, expiry, and binding."""

        _validate_kind(kind)
        _validate_identity(principal, "principal")
        _validate_identity(capability_scope_id, "capability_scope_id")
        body = self._decode(value)
        if body.get("k") != kind:
            raise HandleError("handle kind does not match")
        if body.get("p") != principal or body.get("s") != capability_scope_id:
            raise HandleScopeError("handle belongs to another scope")
        try:
            expiry = _parse_timestamp(body["e"])
        except (KeyError, TypeError, ValueError) as error:
            raise HandleError("handle expiry is invalid") from error
        if expiry <= _normalise_utc(self._clock()):
            raise HandleError("handle is expired")
        actual_binding = body.get("b")
        if not isinstance(actual_binding, dict):
            raise HandleError("handle binding is invalid")
        requested_binding = _json_object(binding or {}, "binding")
        if not hmac.compare_digest(_encode_json(actual_binding), _encode_json(requested_binding)):
            raise HandleError("handle binding does not match")
        payload = body.get("d")
        if not isinstance(payload, dict):
            raise HandleError("handle payload is invalid")
        return MappingProxyType(payload)

    def _decode(self, value: str) -> dict[str, object]:
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > MAX_HANDLE_BYTES:
            raise HandleError("handle encoding is invalid")
        parts = value.split(".")
        if len(parts) != 3 or parts[0] != HANDLE_VERSION:
            raise HandleError("handle version is invalid")
        try:
            encoded = _unb64(parts[1])
            signature = _unb64(parts[2])
        except ValueError as error:
            raise HandleError("handle encoding is invalid") from error
        expected = _sign(self._secret, encoded)
        if not hmac.compare_digest(signature, expected):
            raise HandleError("handle signature is invalid")
        try:
            body = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HandleError("handle payload is invalid") from error
        if not isinstance(body, dict) or set(body) != {"v", "k", "p", "s", "e", "d", "b"}:
            raise HandleError("handle payload is invalid")
        if body.get("v") != HANDLE_VERSION:
            raise HandleError("handle version is invalid")
        return body


def load_handle_secret_from_fd(fd: int = 3) -> bytes:
    """Consume the exact 256-bit session HMAC key from inherited FD 3.

    The descriptor is closed even when validation fails, so a child cannot
    retain a usable key descriptor after startup.
    """

    if not isinstance(fd, int) or fd < 0:
        raise ValueError("fd must be a non-negative integer")
    try:
        chunks: list[bytes] = []
        remaining = HANDLE_KEY_BYTES + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        secret = b"".join(chunks)
    except OSError as error:
        raise HandleError("handle key is unavailable") from error
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if len(secret) != HANDLE_KEY_BYTES:
        raise HandleError("handle key has an invalid length")
    return secret


def _sign(secret: bytes, payload: bytes) -> bytes:
    return hmac.new(secret, payload, sha256).digest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeEncodeError) as error:
        raise ValueError("not base64url") from error


def _encode_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _json_object(value: Mapping[str, object], name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    try:
        encoded = _encode_json(dict(value))
        decoded = json.loads(encoded.decode("utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"{name} must be JSON-compatible") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"{name} must be an object")
    return decoded


def _validate_kind(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError("handle kind must be a bounded string")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in value):
        raise ValueError("handle kind contains unsupported characters")


def _validate_identity(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{name} must be a bounded string")


def _normalise_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _normalise_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is not text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _normalise_utc(parsed)


__all__ = [
    "DEFAULT_HANDLE_TTL",
    "HANDLE_KEY_BYTES",
    "HANDLE_VERSION",
    "HandleError",
    "HandleScopeError",
    "ScopedHandleCodec",
    "load_handle_secret_from_fd",
]
