from __future__ import annotations

import io
import os
import ctypes
import sys
import time

import pytest

from nan_fung.ingestion import parser_runner
from nan_fung.ingestion.parser_runner import (
    ParserExecutionError,
    ParserLimits,
    ParserTimeoutError,
    parse_saved_artifact,
    parser_isolation_status,
    run_bounded_parser,
)
from nan_fung.storage.artifacts import ArtifactStore


_LIBC = ctypes.CDLL(None)
_LIBC_OPEN = _LIBC.open
_LIBC_OPEN.argtypes = (ctypes.c_char_p, ctypes.c_int)
_LIBC_OPEN.restype = ctypes.c_int


def _parse_text(payload: bytes) -> dict[str, str]:
    return {"value": payload.decode("utf-8")}


def _attempt_file_read(_payload: bytes) -> dict[str, str]:
    with open("/etc/hosts", encoding="utf-8") as input_file:
        return {"value": input_file.read()}


def _attempt_io_file_read(_payload: bytes) -> dict[str, str]:
    with io.open("/etc/hosts", encoding="utf-8") as input_file:
        return {"value": input_file.read()}


def _attempt_inherited_fd_read(payload: bytes) -> dict[str, str]:
    return {"value": os.read(int(payload), 1).decode("utf-8")}


def _attempt_inherited_fd_pread(payload: bytes) -> dict[str, str]:
    return {"value": os.pread(int(payload), 1, 0).decode("utf-8")}


def _probe_inherited_fd_closed(payload: bytes) -> dict[str, bool]:
    try:
        os.fstat(int(payload))
    except OSError:
        return {"closed": True}
    return {"closed": False}


def _attempt_prebound_libc_file_read(_payload: bytes) -> dict[str, str]:
    descriptor = _LIBC_OPEN(b"/etc/hosts", os.O_RDONLY)
    if descriptor < 0:
        raise PermissionError("macOS sandbox denied prebound libc open")
    try:
        return {"value": os.read(descriptor, 1).decode("utf-8")}
    finally:
        os.close(descriptor)


def _attempt_filesystem_metadata_probe(_payload: bytes) -> dict[str, bool]:
    if os.path.exists("/etc/hosts"):
        return {"visible": True}
    raise PermissionError("macOS sandbox denied arbitrary metadata probe")


def _emit_unbounded_stdout(_payload: bytes) -> None:
    while True:
        sys.stdout.buffer.write(b"noise" * 128)
        sys.stdout.buffer.flush()
        time.sleep(0.01)


def _never_finish(_payload: bytes) -> None:
    time.sleep(60)


def test_parser_child_receives_only_bytes_and_returns_bounded_json(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    artifact = store.put_bytes(b"saved evidence")

    result = parse_saved_artifact(store, artifact, _parse_text)

    assert result == {"value": "saved evidence"}


def test_parser_child_denies_arbitrary_file_capability() -> None:
    with pytest.raises(ParserExecutionError, match="PARSER_PERMISSIONERROR"):
        run_bounded_parser(_attempt_file_read, b"ignored", limits=ParserLimits(timeout_seconds=5))


def test_parser_child_denies_io_open_bypass() -> None:
    with pytest.raises(ParserExecutionError, match="PARSER_PERMISSIONERROR"):
        run_bounded_parser(_attempt_io_file_read, b"ignored", limits=ParserLimits(timeout_seconds=5))


def test_parser_child_denies_prebound_ctypes_file_bypass() -> None:
    with pytest.raises(ParserExecutionError, match="PARSER_PERMISSIONERROR"):
        run_bounded_parser(_attempt_prebound_libc_file_read, b"ignored")


def test_parser_child_denies_arbitrary_filesystem_metadata() -> None:
    with pytest.raises(ParserExecutionError, match="PARSER_PERMISSIONERROR"):
        run_bounded_parser(_attempt_filesystem_metadata_probe, b"ignored")


def test_parser_child_closes_and_blocks_inherited_parent_fd(tmp_path) -> None:
    secret = tmp_path / "parent-only-evidence.txt"
    secret.write_text("secret", encoding="utf-8")
    inherited_fd = os.open(secret, os.O_RDONLY)
    try:
        payload = str(inherited_fd).encode("ascii")
        assert run_bounded_parser(_probe_inherited_fd_closed, payload) == {"closed": True}
        with pytest.raises(ParserExecutionError):
            run_bounded_parser(_attempt_inherited_fd_read, payload)
        with pytest.raises(ParserExecutionError):
            run_bounded_parser(_attempt_inherited_fd_pread, payload)
    finally:
        os.close(inherited_fd)


def test_parser_parent_stops_noisy_stdout_at_output_limit() -> None:
    with pytest.raises(ParserExecutionError, match="PARSER_OUTPUT_LIMIT"):
        run_bounded_parser(
            _emit_unbounded_stdout,
            b"ignored",
            limits=ParserLimits(timeout_seconds=5, max_output_bytes=64),
        )


def test_parser_parent_preserves_timeout() -> None:
    with pytest.raises(ParserTimeoutError, match="PARSER_TIMEOUT"):
        run_bounded_parser(
            _never_finish,
            b"ignored",
            limits=ParserLimits(timeout_seconds=0.25),
        )


def test_parser_isolation_status_is_explicit_when_the_host_cannot_enforce_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "aix")
    assert parser_isolation_status() == {
        "available": False,
        "backend": None,
        "reason": "PARSER_ISOLATION_UNAVAILABLE",
    }


def test_parser_isolation_status_picks_bubblewrap_on_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(parser_runner.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    assert parser_isolation_status() == {
        "available": True,
        "backend": "bubblewrap",
        "reason": None,
    }


def test_parser_isolation_status_fails_closed_when_bubblewrap_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(parser_runner.shutil, "which", lambda _name: None)
    assert parser_isolation_status() == {
        "available": False,
        "backend": None,
        "reason": "PARSER_ISOLATION_UNAVAILABLE",
    }


def test_parser_isolation_status_picks_sandbox_exec_on_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(parser_runner.shutil, "which", lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else None)
    assert parser_isolation_status() == {
        "available": True,
        "backend": "sandbox-exec",
        "reason": None,
    }
