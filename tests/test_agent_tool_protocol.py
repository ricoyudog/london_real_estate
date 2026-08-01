from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from nan_fung.agent_tools.protocol import (
    AgentToolError,
    parse_request,
    read_request,
    read_result,
    write_result,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "agent_tools" / "v1"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_PATH / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "name",
    [
        "describe_market_data",
        "query_bank_rate",
        "request_bank_rate_refresh",
        "get_citation_metadata",
        "get_refresh_status",
    ],
)
def test_protocol_reads_each_versioned_request_fixture(name: str) -> None:
    request = _fixture("requests.json")[name]
    assert isinstance(request, dict)

    decoded = read_request(
        io.BytesIO(json.dumps(request, separators=(",", ":")).encode("utf-8"))
    )

    assert decoded == request
    assert parse_request(decoded).request_id == request["request_id"]


@pytest.mark.parametrize("case", ["unknown_top_level", "wrong_schema_version"])
def test_protocol_rejects_unknown_or_wrong_versioned_request_shapes(case: str) -> None:
    request = _fixture("invalid-requests.json")[case]

    with pytest.raises(AgentToolError):
        parse_request(read_request(io.BytesIO(json.dumps(request).encode("utf-8"))))


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b'{"schema_version":"agent_tool_request.v1"}{}',
        b" " * (64 * 1024 + 1),
    ],
)
def test_protocol_rejects_invalid_utf8_trailing_bytes_and_oversized_input(
    payload: bytes,
) -> None:
    with pytest.raises(AgentToolError):
        read_request(io.BytesIO(payload))


def test_protocol_writes_exactly_one_utf8_json_result_document() -> None:
    result = _fixture("result-envelope.json")
    stdout = io.BytesIO()

    write_result(stdout, result)

    payload = stdout.getvalue()
    assert payload.decode("utf-8").rstrip().startswith("{")
    assert json.loads(payload) == result


@pytest.mark.parametrize("name", ["ok", "partial", "access_denied"])
def test_protocol_reads_each_shared_non_python_result_fixture(name: str) -> None:
    result = _fixture("results.json")[name]
    assert isinstance(result, dict)

    decoded = read_result(io.BytesIO(json.dumps(result).encode("utf-8")))

    assert decoded == result


@pytest.mark.parametrize("name", ["unknown_result_field", "unsafe_error_shape"])
def test_protocol_rejects_invalid_non_python_result_fixtures(name: str) -> None:
    result = _fixture("invalid-results.json")[name]
    assert isinstance(result, dict)

    with pytest.raises(AgentToolError):
        read_result(io.BytesIO(json.dumps(result).encode("utf-8")))


def test_protocol_rejects_an_oversized_result_before_writing_it() -> None:
    result = _fixture("result-envelope.json")
    result["data"] = {"large": "x" * (256 * 1024)}
    stdout = io.BytesIO()

    with pytest.raises(AgentToolError):
        write_result(stdout, result)

    assert stdout.getvalue() == b""
