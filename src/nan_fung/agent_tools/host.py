"""Trusted one-shot subprocess host for the agent-tool executable.

This is deliberately a small transport/security adapter, not a runtime,
profile manager, or task system.  It builds host context, keeps the runtime
HMAC key out of argv/environment/disk, and contains child-process failures.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
import io
import json
import os
import secrets
import selectors
import signal
import subprocess
import threading
import time
from typing import Protocol

from .facade import HOST_TOOL_NAMES, MODEL_TOOL_NAMES
from .handles import HANDLE_KEY_BYTES
from .protocol import (
    MAX_STDERR_BYTES,
    MAX_STDIN_BYTES,
    MAX_STDOUT_BYTES,
    InvalidArgument,
    PolicyDenied,
    ProtocolError,
    ResultTooLarge,
    RetryableUnavailable,
    Timeout,
    error_result,
    exit_code_for_result,
    read_result,
)


HANDLE_FD = 3


class CancelSignal(Protocol):
    def is_set(self) -> bool: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AgentToolHost:
    """Invoke one bounded child with an inherited runtime HMAC descriptor."""

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        timeout_seconds: float = 10.0,
        output_limit: int = MAX_STDOUT_BYTES,
        stderr_limit: int = MAX_STDERR_BYTES,
        handle_secret: bytes | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        selected = tuple(command or ("nan-fung-agent-tools",))
        if not selected or any(not isinstance(item, str) or not item for item in selected):
            raise ValueError("command must be a non-empty argv sequence")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if output_limit < 1 or stderr_limit < 1:
            raise ValueError("process output bounds must be positive")
        secret = handle_secret or secrets.token_bytes(HANDLE_KEY_BYTES)
        if not isinstance(secret, bytes) or len(secret) != HANDLE_KEY_BYTES:
            raise ValueError("handle_secret must be exactly 256 bits")
        self._command = selected
        self._timeout_seconds = timeout_seconds
        self._output_limit = output_limit
        self._stderr_limit = stderr_limit
        self._handle_secret = bytes(secret)
        self._environment = dict(environment) if environment is not None else None
        self._scope_lock = threading.Lock()
        self._used_capability_scope_ids: set[str] = set()

    def open_session(
        self,
        *,
        principal: str,
        allowed_access_classes: Sequence[str],
        allowed_capability_ids: Sequence[str],
        allowed_refresh_profiles: Sequence[str],
        capability_scope_id: str | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> "AgentToolSession":
        scope = self._reserve_scope(capability_scope_id)
        try:
            return AgentToolSession(
                self,
                principal=principal,
                allowed_access_classes=allowed_access_classes,
                allowed_capability_ids=allowed_capability_ids,
                allowed_refresh_profiles=allowed_refresh_profiles,
                capability_scope_id=scope,
                clock=clock,
            )
        except Exception:
            # An invalid caller configuration did not create a session, so it
            # should not consume an injected test scope permanently.
            with self._scope_lock:
                self._used_capability_scope_ids.discard(scope)
            raise

    def _reserve_scope(self, capability_scope_id: str | None) -> str:
        if capability_scope_id is not None:
            if (
                not isinstance(capability_scope_id, str)
                or not capability_scope_id
                or len(capability_scope_id) > 256
            ):
                raise ValueError("capability_scope_id must be a bounded non-empty string")
            with self._scope_lock:
                if capability_scope_id in self._used_capability_scope_ids:
                    raise ValueError("capability_scope_id has already been used by this host")
                self._used_capability_scope_ids.add(capability_scope_id)
            return capability_scope_id

        while True:
            generated = f"scope_{secrets.token_urlsafe(24)}"
            with self._scope_lock:
                if generated not in self._used_capability_scope_ids:
                    self._used_capability_scope_ids.add(generated)
                    return generated

    def invoke(
        self,
        tool_name: str,
        request: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
        cancel_event: CancelSignal | None = None,
    ) -> dict[str, object]:
        """Run the fixed executable with ``tool_name`` as its sole selector."""

        request_id = request.get("request_id") if isinstance(request.get("request_id"), str) else None
        if tool_name not in HOST_TOOL_NAMES:
            return error_result(request_id, InvalidArgument("unknown agent tool selector"))
        try:
            payload = json.dumps(
                dict(request), ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError):
            return error_result(request_id, InvalidArgument("request is not JSON-compatible"))
        if len(payload) > MAX_STDIN_BYTES:
            return error_result(request_id, InvalidArgument("request exceeds stdin bound"))
        budget = timeout_seconds if timeout_seconds is not None else self._timeout_seconds
        if budget <= 0:
            return error_result(request_id, InvalidArgument("timeout must be positive"))

        read_fd, write_fd = os.pipe()
        placeholder_fd = _ensure_handle_fd_is_open(read_fd)
        process: subprocess.Popen[bytes] | None = None
        try:
            _write_key(write_fd, self._handle_secret)
            os.close(write_fd)
            write_fd = -1
            process = subprocess.Popen(
                [*self._command, tool_name],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                # ``preexec_fn`` runs before ``close_fds`` on CPython.  Keep
                # both the pipe source and FD 3 through that close pass, then
                # the setup function atomically leaves only FD 3 for exec.
                pass_fds=tuple(sorted({read_fd, HANDLE_FD})),
                preexec_fn=_child_fd_setup(read_fd),
                start_new_session=True,
                env=self._environment,
            )
        except OSError:
            return error_result(request_id, RetryableUnavailable("child could not start"))
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            if process is not None:
                os.close(read_fd)
            elif read_fd >= 0:
                os.close(read_fd)
            if placeholder_fd >= 0:
                os.close(placeholder_fd)

        assert process is not None
        try:
            if process.stdin is None:
                return error_result(request_id, ProtocolError("child stdin is unavailable"))
            process.stdin.write(payload)
            process.stdin.close()
            state = _collect_child_output(
                process,
                timeout_seconds=budget,
                output_limit=self._output_limit,
                stderr_limit=self._stderr_limit,
                cancel_event=cancel_event,
            )
            if state.reason == "timeout" or state.reason == "cancelled":
                _terminate_process_group(process)
                return error_result(request_id, Timeout())
            if state.reason == "output_limit":
                _terminate_process_group(process)
                return error_result(request_id, ResultTooLarge())
            if state.reason is not None:
                _terminate_process_group(process)
                return error_result(request_id, ProtocolError())
            try:
                parsed = read_result(io.BytesIO(state.stdout), maximum_bytes=self._output_limit)
            except Exception:
                return error_result(request_id, ProtocolError())
            if parsed.get("request_id") != request_id:
                return error_result(request_id, ProtocolError("child result request_id mismatches request"))
            expected_exit = exit_code_for_result(parsed)
            if process.returncode != expected_exit:
                _terminate_process_group(process)
                return error_result(request_id, ProtocolError())
            # A child is allowed to be one-shot only.  If it returned while a
            # descendant remains in its dedicated process group, terminate the
            # descendant before passing the result to the caller.
            _terminate_process_group(process)
            return parsed
        except (BrokenPipeError, OSError):
            _terminate_process_group(process)
            return error_result(request_id, ProtocolError())
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass


class AgentToolSession:
    """A minimal trusted-session helper for context, IDs, and poll cadence."""

    def __init__(
        self,
        host: AgentToolHost,
        *,
        principal: str,
        allowed_access_classes: Sequence[str],
        allowed_capability_ids: Sequence[str],
        allowed_refresh_profiles: Sequence[str],
        capability_scope_id: str | None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(principal, str) or not principal or len(principal) > 256:
            raise ValueError("principal must be non-empty")
        self._host = host
        self._principal = principal
        self._access = _trusted_strings(allowed_access_classes, "allowed_access_classes")
        if not self._access:
            raise ValueError("allowed_access_classes must be non-empty")
        self._capabilities = _trusted_strings(
            allowed_capability_ids, "allowed_capability_ids", allow_empty=True
        )
        self._profiles = _trusted_strings(
            allowed_refresh_profiles, "allowed_refresh_profiles", allow_empty=True
        )
        self._scope = capability_scope_id or f"scope_{secrets.token_urlsafe(24)}"
        if not isinstance(self._scope, str) or not self._scope or len(self._scope) > 256:
            raise ValueError("capability_scope_id must be a bounded non-empty string")
        self._clock = clock
        self._closed = False
        self._refresh_ids: dict[tuple[str, str], tuple[str, str]] = {}
        self._poll_not_before: dict[str, datetime] = {}
        self._poll_intervals: dict[str, timedelta] = {}

    @property
    def capability_scope_id(self) -> str:
        return self._scope

    def close(self) -> None:
        """Discard local state; the host keeps this scope tombstoned."""

        self._closed = True
        self._refresh_ids.clear()
        self._poll_not_before.clear()
        self._poll_intervals.clear()

    def call(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        turn_id: str,
        tool_call_id: str,
        timeout_seconds: float | None = None,
        cancel_event: CancelSignal | None = None,
    ) -> dict[str, object]:
        if self._closed:
            return error_result(None, PolicyDenied("session is closed"))
        if tool_name not in MODEL_TOOL_NAMES:
            return error_result(None, PolicyDenied("tool is not model-facing"))
        if not isinstance(arguments, Mapping):
            return error_result(None, InvalidArgument("tool arguments must be an object"))
        if not _trusted_identifier(turn_id) or not _trusted_identifier(tool_call_id):
            return error_result(None, InvalidArgument("turn and tool-call IDs must be bounded strings"))
        if tool_name == "get_refresh_status":
            job_ref = arguments.get("job_ref")
            if isinstance(job_ref, str):
                not_before = self._poll_not_before.get(job_ref)
                if not_before is not None and self._now() < not_before:
                    return error_result(None, PolicyDenied("refresh status polling is too frequent"))
        host_context: dict[str, object] = {
            "principal": self._principal,
            "capability_scope_id": self._scope,
            "turn_id": turn_id,
            "tool_call_id": tool_call_id,
            "allowed_access_classes": list(self._access),
            "allowed_capability_ids": list(self._capabilities),
            "allowed_refresh_profiles": list(self._profiles),
        }
        if tool_name == "request_data_refresh":
            fingerprint = _fingerprint(arguments)
            key = (turn_id, tool_call_id)
            existing = self._refresh_ids.get(key)
            if existing is None:
                request_id = f"refresh_{secrets.token_urlsafe(18)}"
                self._refresh_ids[key] = (fingerprint, request_id)
            elif existing[0] != fingerprint:
                return error_result(None, InvalidArgument("tool-call retry changed refresh arguments"))
            else:
                request_id = existing[1]
            host_context["refresh_request_id"] = request_id
        request = {
            "schema_version": "agent_tool_request.v1",
            "request_id": f"call_{secrets.token_urlsafe(18)}",
            "arguments": dict(arguments),
            "host_context": host_context,
        }
        response = self._host.invoke(
            tool_name, request, timeout_seconds=timeout_seconds, cancel_event=cancel_event
        )
        self._remember_poll_cadence(tool_name, arguments, response)
        return response

    def approve_refresh(
        self,
        approval_id: str,
        decision: str,
        *,
        turn_id: str = "host-approval",
        tool_call_id: str = "host-approval",
    ) -> dict[str, object]:
        """Invoke the host-only approval selector outside the model allowlist."""

        if self._closed:
            return error_result(None, PolicyDenied("session is closed"))
        request = {
            "schema_version": "agent_tool_request.v1",
            "request_id": f"call_{secrets.token_urlsafe(18)}",
            "arguments": {"approval_id": approval_id, "decision": decision},
            "host_context": {
                "principal": self._principal,
                "capability_scope_id": self._scope,
                "turn_id": turn_id,
                "tool_call_id": tool_call_id,
                "allowed_access_classes": list(self._access),
                "allowed_capability_ids": list(self._capabilities),
                "allowed_refresh_profiles": list(self._profiles),
            },
        }
        return self._host.invoke("approve_refresh", request)

    def _remember_poll_cadence(
        self, tool_name: str, arguments: Mapping[str, object], response: Mapping[str, object]
    ) -> None:
        if response.get("status") != "ok":
            return
        data = response.get("data")
        if not isinstance(data, Mapping):
            return
        if tool_name == "request_data_refresh":
            job_ref = data.get("job_ref")
            seconds = data.get("poll_after_seconds")
            if isinstance(job_ref, str) and isinstance(seconds, int) and seconds > 0:
                interval = timedelta(seconds=seconds)
                self._poll_intervals[job_ref] = interval
                self._poll_not_before[job_ref] = self._now() + interval
        elif tool_name == "get_refresh_status":
            job_ref = arguments.get("job_ref")
            if not isinstance(job_ref, str):
                return
            state = data.get("job_state")
            if state in {"succeeded", "empty", "failed", "dead_letter", "cancelled"}:
                self._poll_not_before.pop(job_ref, None)
                self._poll_intervals.pop(job_ref, None)
                return
            interval = self._poll_intervals.get(job_ref)
            if interval is not None:
                self._poll_not_before[job_ref] = self._now() + interval

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("session clock must return an aware datetime")
        return value.astimezone(UTC)


class _CollectedOutput:
    def __init__(self, stdout: bytes, stderr: bytes, reason: str | None) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.reason = reason


def _write_key(fd: int, secret: bytes) -> None:
    offset = 0
    while offset < len(secret):
        offset += os.write(fd, secret[offset:])


def _child_fd_setup(read_fd: int):
    def setup() -> None:
        if read_fd != HANDLE_FD:
            os.dup2(read_fd, HANDLE_FD)
            os.close(read_fd)

    return setup


def _ensure_handle_fd_is_open(read_fd: int) -> int:
    """Make FD 3 a valid ``pass_fds`` member without leaking a parent FD.

    If the parent already uses FD 3, the child replaces it in ``preexec_fn``.
    If it is unused, keep a private ``/dev/null`` placeholder only until the
    fork.  This is needed because CPython performs ``close_fds`` after the
    pre-exec descriptor duplication.
    """

    if read_fd == HANDLE_FD:
        return -1
    try:
        os.fstat(HANDLE_FD)
        return -1
    except OSError:
        pass
    placeholder = os.open(os.devnull, os.O_RDONLY)
    if placeholder == HANDLE_FD:
        return placeholder
    try:
        os.dup2(placeholder, HANDLE_FD)
    finally:
        os.close(placeholder)
    return HANDLE_FD


def _collect_child_output(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float,
    output_limit: int,
    stderr_limit: int,
    cancel_event: CancelSignal | None,
) -> _CollectedOutput:
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    streams = {
        process.stdout.fileno(): ("stdout", output_limit),
        process.stderr.fileno(): ("stderr", stderr_limit),
    }
    for descriptor in streams:
        os.set_blocking(descriptor, False)
        selector.register(descriptor, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout_seconds
    reason: str | None = None
    try:
        while selector.get_map() or process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                reason = "cancelled"
                break
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            for key, _ in selector.select(timeout=min(0.05, max(0.0, deadline - time.monotonic()))):
                descriptor = key.fd
                target, maximum = streams[descriptor]
                try:
                    chunk = os.read(descriptor, 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(descriptor)
                    continue
                buffer = stdout if target == "stdout" else stderr
                if len(buffer) + len(chunk) > maximum:
                    if target == "stdout":
                        reason = "output_limit"
                        break
                    # stderr is diagnostics only: truncate it but do not make
                    # its contents observable or let it grow memory usage.
                    # Keep draining afterward so a noisy child cannot block
                    # on its own stderr pipe and turn a bounded failure into a
                    # zombie until the full timeout.
                    buffer.extend(chunk[: max(0, maximum - len(buffer))])
                    continue
                if target == "stderr" and len(buffer) >= maximum:
                    continue
                buffer.extend(chunk)
            if reason is not None:
                break
        if reason is None:
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                reason = "timeout"
    finally:
        selector.close()
    return _CollectedOutput(bytes(stdout), bytes(stderr), reason)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    # Even after the direct child exits, its session/process group can still
    # contain descendants.  ``killpg`` is intentionally attempted in that
    # case; a missing group simply means there is nothing left to clean up.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        if process.poll() is None:
            try:
                process.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        time.sleep(0.01)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def _fingerprint(value: Mapping[str, object]) -> str:
    from hashlib import sha256

    return sha256(
        json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _trusted_strings(
    values: Sequence[str], name: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    result = tuple(values)
    if (not result and not allow_empty) or any(not _trusted_identifier(value) for value in result):
        raise ValueError(f"{name} must contain bounded non-empty strings")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _trusted_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 256


__all__ = ["AgentToolHost", "AgentToolSession", "HANDLE_FD"]
