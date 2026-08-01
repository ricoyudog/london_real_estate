"""Bounded artifact-to-record parser subprocess protocol.

The parent verifies and opens the already persisted CAS object, then gives the
child only its bytes and a versioned module-level parser identity.  The child
has no database handle, path, URL, or network capability and emits one bounded
canonical-JSON frame.  This is intentionally a small local isolation boundary,
not a generic sandbox or remote execution framework.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import selectors
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Any, TypeVar
from urllib import request as urllib_request

from nan_fung.storage.artifacts import ArtifactStore, StoredArtifact

from .canonical import CanonicalizationError, canonical_json, parse_canonical_json


T = TypeVar("T")


class ParserExecutionError(RuntimeError):
    """A parser could not safely produce a valid bounded output frame."""


class ParserTimeoutError(ParserExecutionError):
    """The parser did not finish within its fixed wall-clock budget."""


def parser_isolation_status() -> dict[str, str | bool | None]:
    """Report whether this host can enforce the parser child boundary.

    The parser protocol deliberately does not fall back to a Python-only
    guard.  It needs macOS ``sandbox-exec`` today, so operators can see this
    prerequisite before a job reaches the parsing stage.
    """

    if sys.platform != "darwin":
        return {
            "available": False,
            "backend": None,
            "reason": "PARSER_ISOLATION_UNAVAILABLE",
        }
    if shutil.which("sandbox-exec") is None:
        return {
            "available": False,
            "backend": None,
            "reason": "PARSER_ISOLATION_UNAVAILABLE",
        }
    return {"available": True, "backend": "sandbox-exec", "reason": None}


@dataclass(frozen=True, slots=True)
class ParserLimits:
    """Hard limits for one local parser child invocation."""

    timeout_seconds: float = 30.0
    max_input_bytes: int = 32 * 1024 * 1024
    max_output_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("parser timeout_seconds must be positive")
        if self.max_input_bytes < 0 or self.max_output_bytes < 1:
            raise ValueError("parser byte limits must be positive")


def parse_saved_artifact(
    store: ArtifactStore,
    artifact: StoredArtifact | str,
    parser: Callable[[bytes], T],
    *,
    limits: ParserLimits = ParserLimits(),
    decoder: Callable[[Any], T] | None = None,
) -> T:
    """Read a verified CAS object in the parent and parse it in a child.

    ``parser`` must be a top-level callable.  Its return value must be
    canonical-JSON compatible or a dataclass tree.  ``decoder`` lets the parent
    turn that safe JSON frame back into a typed domain record.
    """

    with store.open(artifact) as input_file:
        payload = input_file.read(limits.max_input_bytes + 1)
    if len(payload) > limits.max_input_bytes:
        raise ParserExecutionError("PARSER_INPUT_LIMIT")
    result = run_bounded_parser(parser, payload, limits=limits)
    return decoder(result) if decoder is not None else result  # type: ignore[return-value]


def run_bounded_parser(
    parser: Callable[[bytes], T],
    payload: bytes,
    *,
    limits: ParserLimits = ParserLimits(),
) -> Any:
    """Run a pure parser in an OS-enforced child with only artifact bytes."""

    if not isinstance(payload, bytes):
        raise TypeError("parser payload must be bytes")
    if len(payload) > limits.max_input_bytes:
        raise ParserExecutionError("PARSER_INPUT_LIMIT")
    module_name, qualname = _parser_identity(parser)
    parser_source = _parser_source_path(parser)
    isolation = parser_isolation_status()
    sandbox_executable = shutil.which("sandbox-exec")
    if not isolation["available"] or sandbox_executable is None:
        # The child must have a kernel-enforced policy.  A Python-only guard
        # can be bypassed by an already-bound C extension function.
        raise ParserExecutionError("PARSER_ISOLATION_UNAVAILABLE")
    command = (
        sandbox_executable,
        "-p",
        _macos_parser_profile(parser_source),
        str(Path(sys.executable).resolve()),
        "-c",
        _SANDBOX_BOOTSTRAP,
        module_name,
        qualname,
        str(parser_source),
        str(limits.max_input_bytes),
        str(limits.max_output_bytes),
    )
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        cwd=str(_source_root()),
        env=_sandbox_environment(),
        close_fds=True,
        start_new_session=True,
    )
    returncode, frame = _communicate_bounded(
        process,
        payload,
        timeout_seconds=limits.timeout_seconds,
        max_output_bytes=limits.max_output_bytes,
    )
    if returncode != 0 and not frame:
        raise ParserExecutionError("PARSER_PROTOCOL_EOF")
    try:
        envelope = parse_canonical_json(frame)
    except CanonicalizationError as error:
        raise ParserExecutionError("PARSER_PROTOCOL_INVALID") from error
    if not isinstance(envelope, dict) or set(envelope) - {"result", "error"}:
        raise ParserExecutionError("PARSER_PROTOCOL_INVALID")
    error = envelope.get("error")
    if error is not None:
        raise ParserExecutionError(str(error))
    if "result" not in envelope:
        raise ParserExecutionError("PARSER_PROTOCOL_INVALID")
    return envelope["result"]


_STREAM_CHUNK_BYTES = 64 * 1024


def _communicate_bounded(
    process: subprocess.Popen[bytes],
    payload: bytes,
    *,
    timeout_seconds: float,
    max_output_bytes: int,
) -> tuple[int, bytes]:
    """Write input and read at most one byte beyond the output limit."""

    stdin = process.stdin
    stdout = process.stdout
    if stdin is None or stdout is None:
        raise ParserExecutionError("PARSER_PROTOCOL_EOF")
    selector = selectors.DefaultSelector()
    output = bytearray()
    input_offset = 0
    deadline = time.monotonic() + timeout_seconds

    def close_stream(stream: Any) -> None:
        try:
            selector.unregister(stream)
        except (KeyError, ValueError):
            pass
        try:
            stream.close()
        except OSError:
            pass

    try:
        os.set_blocking(stdin.fileno(), False)
        os.set_blocking(stdout.fileno(), False)
        selector.register(stdout, selectors.EVENT_READ)
        if payload:
            selector.register(stdin, selectors.EVENT_WRITE)
        else:
            stdin.close()

        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            for key, _ in selector.select(remaining):
                stream = key.fileobj
                if stream is stdin:
                    try:
                        written = os.write(
                            stdin.fileno(),
                            memoryview(payload)[
                                input_offset : input_offset + _STREAM_CHUNK_BYTES
                            ],
                        )
                    except (BlockingIOError, InterruptedError):
                        continue
                    except BrokenPipeError:
                        close_stream(stdin)
                        continue
                    input_offset += written
                    if input_offset == len(payload):
                        close_stream(stdin)
                    continue

                remaining_capacity = max_output_bytes - len(output)
                read_size = (
                    1
                    if remaining_capacity <= 0
                    else min(_STREAM_CHUNK_BYTES, remaining_capacity + 1)
                )
                try:
                    chunk = os.read(stdout.fileno(), read_size)
                except (BlockingIOError, InterruptedError):
                    continue
                if not chunk:
                    close_stream(stdout)
                    continue
                output.extend(chunk)
                if len(output) > max_output_bytes:
                    raise ParserExecutionError("PARSER_OUTPUT_LIMIT")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError from error
    except TimeoutError as error:
        _terminate_parser_child(process)
        raise ParserTimeoutError("PARSER_TIMEOUT") from error
    except BaseException:
        _terminate_parser_child(process)
        raise
    finally:
        close_stream(stdin)
        close_stream(stdout)
        selector.close()

    if process.returncode is None:
        raise ParserExecutionError("PARSER_PROTOCOL_EOF")
    return process.returncode, bytes(output)


def _terminate_parser_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


def _parser_identity(parser: Callable[[bytes], Any]) -> tuple[str, str]:
    module_name = getattr(parser, "__module__", "")
    qualname = getattr(parser, "__qualname__", "")
    if not module_name or not qualname or "<locals>" in qualname:
        raise ParserExecutionError("parser must be a module-level callable")
    return module_name, qualname


_SANDBOX_BOOTSTRAP = """\
import sys
from nan_fung.ingestion.parser_runner import _sandbox_child_main
_sandbox_child_main(*sys.argv[1:])
"""


def _sandbox_child_main(
    module_name: str,
    qualname: str,
    parser_source: str,
    max_input_bytes: str,
    max_output_bytes: str,
) -> None:
    """Execute one parser after macOS Sandbox has removed external access."""

    try:
        input_limit = int(max_input_bytes)
        output_limit = int(max_output_bytes)
        payload = sys.stdin.buffer.read(input_limit + 1)
        if len(payload) > input_limit:
            raise ParserExecutionError("PARSER_INPUT_LIMIT")
        parser = _load_sandboxed_parser(module_name, qualname, Path(parser_source))
        with _deny_external_capabilities():
            value = parser(payload)
        result = _json_value(value)
        frame = canonical_json({"result": result}).encode("utf-8")
    except BaseException as error:
        # Do not send parser tracebacks or source payloads back through the
        # protocol.  Error type is sufficient for durable error classification.
        frame = canonical_json({"error": f"PARSER_{type(error).__name__.upper()}"}).encode(
            "utf-8"
        )
    if len(frame) > output_limit:
        frame = canonical_json({"error": "PARSER_OUTPUT_LIMIT"}).encode("utf-8")
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()


def _load_sandboxed_parser(
    module_name: str, qualname: str, source: Path
) -> Callable[[bytes], Any]:
    """Load exactly the parent's declared module source inside the sandbox."""

    source = source.resolve()
    if source.is_relative_to(_source_root()):
        module = importlib.import_module(module_name)
    else:
        spec = importlib.util.spec_from_file_location(module_name, source)
        if spec is None or spec.loader is None:
            raise ParserExecutionError("PARSER_MODULE_UNAVAILABLE")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str) or Path(module_file).resolve() != source:
        raise ParserExecutionError("PARSER_MODULE_MISMATCH")
    value: Any = module
    for component in qualname.split("."):
        value = getattr(value, component)
    if not callable(value):
        raise TypeError("parser identity did not resolve to a callable")
    return value


def _parser_source_path(parser: Callable[[bytes], Any]) -> Path:
    source = inspect.getsourcefile(parser)
    if not source:
        raise ParserExecutionError("parser source is unavailable")
    candidate = Path(source).resolve()
    if not candidate.is_file():
        raise ParserExecutionError("parser source is unavailable")
    return candidate


def _source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _package_root() -> Path:
    return _source_root() / "nan_fung"


def _sandbox_environment() -> dict[str, str]:
    """Pass no ambient credentials into the parser child."""

    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join(str(path) for path in _sandbox_import_paths()),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    for name in ("LANG", "LC_ALL", "TZ"):
        if value := os.environ.get(name):
            environment[name] = value
    return environment


def _sandbox_import_paths() -> tuple[Path, ...]:
    """Preserve only this package and its venv dependencies after exec."""

    prefix = Path(sys.prefix).resolve()
    candidates = {_source_root()}
    for value in sys.path:
        if not value:
            continue
        candidate = Path(value).resolve()
        if candidate.is_relative_to(prefix):
            candidates.add(candidate)
    return tuple(sorted(candidates, key=str))


def _macos_parser_profile(parser_source: Path) -> str:
    """Build the minimal macOS profile needed to load parser code, not data."""

    readable_roots = {
        Path("/System"),
        Path("/usr/lib"),
        Path("/private/var/db/timezone"),
        Path(sys.prefix),
        Path(sys.base_prefix),
        _package_root(),
    }
    read_rules = "\n".join(
        f'  (subpath "{_sandbox_quote(path.resolve())}")'
        for path in sorted(readable_roots, key=str)
    )
    return "\n".join(
        (
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            # ``sandbox-exec`` resolves interpreter symlinks after applying
            # the profile.  Its process-exec operation must therefore be
            # inherited by the resolved interpreter; the file/network rules
            # below remain fail-closed for every descendant process.
            "(allow process-exec*)",
            "(allow file-read-metadata\n"
            f'  (subpath "{_sandbox_quote(_source_root())}")\n'
            f'  (subpath "{_sandbox_quote(Path(sys.base_prefix).resolve().parent)}")\n'
            ")",
            f'(allow file-read* (literal "{_sandbox_quote(_source_root())}"))',
            f"(allow file-read*\n{read_rules})",
            f'(allow file-read* (literal "{_sandbox_quote(parser_source.resolve())}"))',
        )
    )


def _sandbox_quote(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


@contextmanager
def _deny_external_capabilities() -> Any:
    """Make accidental file, DB, and network use fail inside a parser child."""

    def denied(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("parser child has no external capabilities")

    replacements: list[tuple[Any, str, Any]] = [
        (builtins, "open", denied),
        (io, "open", denied),
        (io, "FileIO", denied),
        (os, "open", denied),
        (os, "system", denied),
        (Path, "open", denied),
        (socket, "socket", denied),
        (socket, "create_connection", denied),
        (socket, "socketpair", denied),
        (socket, "fromfd", denied),
        (sqlite3, "connect", denied),
        (urllib_request, "urlopen", denied),
    ]
    replacements.extend(
        (os, name, denied)
        for name in (
            "close",
            "closerange",
            "dup",
            "dup2",
            "dup3",
            "fdopen",
            "lseek",
            "pread",
            "preadv",
            "pwrite",
            "pwritev",
            "read",
            "readv",
            "sendfile",
            "write",
            "writev",
            "copy_file_range",
            "pipe",
            "pipe2",
            "openpty",
        )
        if hasattr(os, name)
    )
    originals = [(owner, name, getattr(owner, name)) for owner, name, _ in replacements]
    try:
        for owner, name, replacement in replacements:
            setattr(owner, name, replacement)
        yield
    finally:
        for owner, name, original in reversed(originals):
            setattr(owner, name, original)
