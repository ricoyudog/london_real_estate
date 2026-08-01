from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import time

from nan_fung.agent_tools import AgentToolHost, run_cli


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "agent_tools" / "v1"


def _request() -> dict[str, object]:
    fixtures = json.loads((FIXTURE_PATH / "requests.json").read_text(encoding="utf-8"))
    return fixtures["query_bank_rate"]


def _success_program(*, assert_selector: bool = False) -> str:
    selector_check = (
        "assert sys.argv[1:] == ['query_market_data'], sys.argv"
        if assert_selector
        else ""
    )
    return "\n".join(
        (
            "import json, sys",
            selector_check,
            "sys.stdin.buffer.read()",
            "sys.stdout.write(json.dumps({"
            "'schema_version': 'agent_tool_result.v1', "
            "'request_id': 'call_query_001', 'status': 'ok', "
            "'data': {'records': []}, 'warnings': [], 'error': None}))",
        )
    )


def _error_code(result: dict[str, object]) -> str:
    error = result["error"]
    assert isinstance(error, dict)
    code = error["code"]
    assert isinstance(code, str)
    return code


def test_host_uses_the_argv_tool_selector_and_accepts_one_result_document() -> None:
    host = AgentToolHost(command=[sys.executable, "-c", _success_program(assert_selector=True)])

    result = host.invoke("query_market_data", _request())

    assert result["status"] == "ok"
    assert result["request_id"] == "call_query_001"
    assert result["data"] == {"records": []}


def test_host_passes_the_runtime_key_only_via_the_inherited_descriptor() -> None:
    secret = b"never-leak-agent-tool-key-012345"
    program = "\n".join(
        (
            "import json, os, sys",
            "request = json.loads(sys.stdin.buffer.read())",
            "fd_open = False",
            "try:",
            "    os.fstat(3)",
            "    fd_open = True",
            "except OSError:",
            "    pass",
            "key_matches = False",
            "if fd_open:",
            "    key_matches = os.read(3, 32) == " + repr(secret),
            "sys.stdout.write(json.dumps({",
            "'schema_version': 'agent_tool_result.v1', "
            "'request_id': request['request_id'], 'status': 'ok', "
            "'data': {'fd_open': fd_open, 'key_matches': key_matches, "
            "'argv_leaks_secret': " + repr(secret.decode()) + " in ' '.join(sys.argv), "
            "'environment_leaks_secret': "
            + repr(secret.decode())
            + " in json.dumps(dict(os.environ))}, "
            "'warnings': [], 'error': None}))",
        )
    )
    host = AgentToolHost(command=[sys.executable, "-c", program], handle_secret=secret)

    result = host.invoke("query_market_data", _request())

    assert result["status"] == "ok"
    assert result["data"] == {
        "fd_open": True,
        "key_matches": True,
        "argv_leaks_secret": False,
        "environment_leaks_secret": False,
    }


def test_host_converts_malformed_stdout_and_stderr_to_a_safe_protocol_error() -> None:
    program = "import sys; sys.stderr.write('sensitive upstream body'); sys.stdout.write('{}{}')"
    host = AgentToolHost(command=[sys.executable, "-c", program])

    result = host.invoke("query_market_data", _request())

    assert result["status"] == "error"
    assert _error_code(result) == "PROTOCOL_ERROR"
    assert "sensitive upstream body" not in json.dumps(result)


def test_host_converts_a_crashing_child_to_a_safe_protocol_error() -> None:
    program = "raise RuntimeError('sensitive child traceback')"
    host = AgentToolHost(command=[sys.executable, "-c", program])

    result = host.invoke("query_market_data", _request())

    assert result["status"] == "error"
    assert _error_code(result) == "PROTOCOL_ERROR"
    assert "sensitive child traceback" not in json.dumps(result)


def test_host_converts_oversized_child_stdout_to_a_bounded_typed_error() -> None:
    program = "import sys; sys.stdout.write('x' * (256 * 1024 + 1)); sys.stdout.flush()"
    host = AgentToolHost(
        command=[sys.executable, "-c", program], output_limit=256 * 1024
    )

    result = host.invoke("query_market_data", _request())

    assert result["status"] == "error"
    assert _error_code(result) == "RESULT_TOO_LARGE"


def test_host_drains_bounded_stderr_without_exposing_or_blocking_on_it() -> None:
    program = "\n".join(
        (
            "import json, sys",
            "request = json.loads(sys.stdin.buffer.read())",
            "sys.stderr.write('sensitive noisy stderr' * 1024)",
            "sys.stdout.write(json.dumps({",
            "'schema_version': 'agent_tool_result.v1', "
            "'request_id': request['request_id'], 'status': 'ok', "
            "'data': {'records': []}, 'warnings': [], 'error': None}))",
        )
    )
    host = AgentToolHost(
        command=[sys.executable, "-c", program], stderr_limit=128
    )

    result = host.invoke("query_market_data", _request())

    assert result["status"] == "ok"
    assert "sensitive noisy stderr" not in json.dumps(result)


def test_host_timeout_terminates_the_entire_child_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "orphaned-child"
    grandchild = (
        "import pathlib, time; "
        "time.sleep(0.4); "
        f"pathlib.Path({str(marker)!r}).write_text('survived', encoding='utf-8')"
    )
    program = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        "time.sleep(60)"
    )
    host = AgentToolHost(command=[sys.executable, "-c", program], timeout_seconds=0.1)

    result = host.invoke("query_market_data", _request())

    assert result["status"] == "error"
    assert _error_code(result) == "TIMEOUT"
    time.sleep(0.6)
    assert not marker.exists()


def test_host_cancellation_terminates_the_child_process_group() -> None:
    class Cancelled:
        def is_set(self) -> bool:
            return True

    host = AgentToolHost(
        command=[sys.executable, "-c", "import time; time.sleep(60)"]
    )

    result = host.invoke("query_market_data", _request(), cancel_event=Cancelled())

    assert result["status"] == "error"
    assert _error_code(result) == "TIMEOUT"


def test_cli_maps_a_schema_valid_facade_result_to_single_stdout_and_stable_exit_code() -> None:
    class Facade:
        def execute(self, tool_name: str, request: dict[str, object]) -> dict[str, object]:
            assert tool_name == "query_market_data"
            return {
                "schema_version": "agent_tool_result.v1",
                "request_id": request["request_id"],
                "status": "error",
                "data": None,
                "warnings": [],
                "error": {
                    "code": "ACCESS_DENIED",
                    "message": "Access to this capability is denied.",
                    "retryable": False,
                },
            }

    stdin = io.BytesIO(json.dumps(_request()).encode("utf-8"))
    stdout = io.BytesIO()

    exit_code = run_cli(
        ["query_market_data"], facade=Facade(), stdin=stdin, stdout=stdout
    )

    assert exit_code == 3
    result = json.loads(stdout.getvalue())
    assert result["status"] == "error"
    assert _error_code(result) == "ACCESS_DENIED"


def test_cli_maps_invalid_selector_to_one_safe_schema_error_document() -> None:
    stdin = io.BytesIO(b"{}")
    stdout = io.BytesIO()

    exit_code = run_cli(["unknown_tool"], stdin=stdin, stdout=stdout)

    assert exit_code == 6
    result = json.loads(stdout.getvalue())
    assert result["request_id"] is None
    assert result["status"] == "error"
    assert _error_code(result) == "SCHEMA_VIOLATION"
