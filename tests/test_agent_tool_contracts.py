from __future__ import annotations

from copy import deepcopy
from importlib import resources
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from nan_fung.agent_tools import (
    AgentToolFacade,
    HOST_TOOL_NAMES,
    MODEL_TOOL_NAMES,
    load_tool_contracts,
)
from nan_fung.agent_tools.protocol import (
    AccessDenied,
    CapabilityBlocked,
    InternalError,
    InvalidArgument,
    InvalidCursor,
    PolicyDenied,
    ProtocolError,
    ResultTooLarge,
    RetryableUnavailable,
    SchemaViolation,
    Timeout,
    error_result,
    parse_request,
    validate_result,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "agent_tools" / "v1"


def _asset(name: str) -> dict[str, object]:
    return json.loads(
        resources.files("nan_fung.agent_tools").joinpath(name).read_text(encoding="utf-8")
    )


def _fixture(name: str) -> dict[str, object]:
    values = json.loads((FIXTURE_PATH / "tool-contract-fixtures.json").read_text(encoding="utf-8"))
    return deepcopy(values[name])


def _validator(schema: dict[str, object]) -> Draft202012Validator:
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_catalog_matches_the_published_model_and_host_selector_sets() -> None:
    catalog = load_tool_contracts()

    assert set(catalog.model_selectors) == MODEL_TOOL_NAMES
    assert set(catalog.host_selectors) == HOST_TOOL_NAMES
    assert {
        name: (contract.audience, contract.refresh_request_id)
        for name, contract in catalog.items()
    } == {
        "describe_market_data": ("model", "forbidden"),
        "query_market_data": ("model", "forbidden"),
        "get_citation_metadata": ("model", "forbidden"),
        "request_data_refresh": ("model", "required"),
        "get_refresh_status": ("model", "forbidden"),
        "approve_refresh": ("host", "forbidden"),
    }


def test_catalog_and_all_embedded_schemas_are_draft_2020_12_valid() -> None:
    catalog_schema = _asset("agent_tool_contract_catalog.v1.schema.json")
    request_schema = _asset("agent_tool_request.v1.schema.json")
    result_schema = _asset("agent_tool_result.v1.schema.json")
    raw_catalog = _asset("agent_tool_contracts.v1.json")

    _validator(catalog_schema).validate(raw_catalog)
    _validator(request_schema)
    _validator(result_schema)
    for contract in load_tool_contracts().values():
        _validator(contract.arguments_schema)
        _validator(contract.success_data_schema)


def test_language_neutral_fixtures_validate_each_selector_contract() -> None:
    catalog = load_tool_contracts()
    fixtures = _fixture("valid")
    assert set(fixtures) == set(catalog)

    for selector, fixture in fixtures.items():
        assert isinstance(selector, str)
        assert isinstance(fixture, dict)
        contract = catalog[selector]
        _validator(contract.arguments_schema).validate(fixture["arguments"])
        success_data = fixture["success_data"]
        assert isinstance(success_data, list) and success_data
        for data in success_data:
            _validator(contract.success_data_schema).validate(data)


def test_invalid_fixtures_fail_the_selected_schema_while_policy_cases_remain_structural() -> None:
    catalog = load_tool_contracts()
    invalid = _fixture("invalid")
    policy_invalid = _fixture("policy_invalid")

    for fixture in invalid:
        assert isinstance(fixture, dict)
        selector = fixture["selector"]
        target = fixture["target"]
        value = fixture["value"]
        assert isinstance(selector, str)
        assert target in {"arguments", "success_data"}
        schema = getattr(catalog[selector], f"{target}_schema")
        assert not _validator(schema).is_valid(value)

    for fixture in policy_invalid:
        assert isinstance(fixture, dict)
        selector = fixture["selector"]
        assert isinstance(selector, str)
        assert _validator(catalog[selector].arguments_schema).is_valid(fixture["arguments"])


def test_partial_query_and_citation_fixtures_keep_their_selected_data_contracts() -> None:
    catalog = load_tool_contracts()
    partial = _fixture("partial")

    for selector, fixture in partial.items():
        assert isinstance(selector, str)
        assert isinstance(fixture, dict)
        _validator(catalog[selector].success_data_schema).validate(fixture["data"])
        warnings = fixture["warnings"]
        assert isinstance(warnings, list) and warnings


def test_opaque_projections_are_explicitly_bounded_and_accept_safe_nested_locators() -> None:
    catalog = load_tool_contracts()
    describe_schema = catalog["describe_market_data"].success_data_schema
    query_schema = catalog["query_market_data"].success_data_schema
    citation_schema = catalog["get_citation_metadata"].success_data_schema

    describe = _fixture("valid")["describe_market_data"]["success_data"][0]
    query = _fixture("valid")["query_market_data"]["success_data"][0]
    citation = _fixture("valid")["get_citation_metadata"]["success_data"][0]
    assert isinstance(describe, dict)
    assert isinstance(query, dict)
    assert isinstance(citation, dict)

    citation["citations"][0]["locator"] = {
        "evidence_id": "evidence_001",
        "role": "primary",
        "record_locator": {"kind": "csv_row", "row_key": "2026-07-31"},
    }

    assert _validator(citation_schema).is_valid(citation)

    describe["capabilities"][0]["geography"] = {"nested": {"unbounded": "object"}}
    query["records"][0]["payload"] = {"nested": {"unbounded": "object"}}
    citation["citations"][0]["locator"] = {str(index): index for index in range(65)}

    assert not _validator(describe_schema).is_valid(describe)
    assert not _validator(query_schema).is_valid(query)
    assert not _validator(citation_schema).is_valid(citation)


def test_result_envelope_schema_locks_status_data_error_relationships_and_stable_errors() -> None:
    schema = _asset("agent_tool_result.v1.schema.json")
    validator = _validator(schema)
    valid_success = {
        "schema_version": "agent_tool_result.v1",
        "request_id": "call_contract_001",
        "status": "ok",
        "data": {},
        "warnings": [],
        "error": None,
    }
    validator.validate(valid_success)

    invalid_success = dict(valid_success, data=None)
    assert not validator.is_valid(invalid_success)
    invalid_error = dict(valid_success, status="error", data={}, error=None)
    assert not validator.is_valid(invalid_error)

    for error_type in (
        InvalidArgument,
        InvalidCursor,
        AccessDenied,
        CapabilityBlocked,
        PolicyDenied,
        RetryableUnavailable,
        Timeout,
        InternalError,
        SchemaViolation,
        ProtocolError,
        ResultTooLarge,
    ):
        validator.validate(error_result("call_contract_error", error_type()))


def test_existing_wire_fixtures_remain_compatible_while_selected_contracts_reject_injection() -> None:
    request_schema = _validator(_asset("agent_tool_request.v1.schema.json"))
    result_schema = _validator(_asset("agent_tool_result.v1.schema.json"))
    requests = json.loads((FIXTURE_PATH / "requests.json").read_text(encoding="utf-8"))
    invalid_requests = json.loads((FIXTURE_PATH / "invalid-requests.json").read_text(encoding="utf-8"))
    results = json.loads((FIXTURE_PATH / "results.json").read_text(encoding="utf-8"))
    invalid_results = json.loads((FIXTURE_PATH / "invalid-results.json").read_text(encoding="utf-8"))

    for request in requests.values():
        request_schema.validate(request)
        parse_request(request)
    for result in results.values():
        result_schema.validate(result)
        validate_result(result)

    assert not request_schema.is_valid(invalid_requests["unknown_top_level"])
    assert not request_schema.is_valid(invalid_requests["wrong_schema_version"])
    assert request_schema.is_valid(invalid_requests["argument_policy_injection"])
    assert not _validator(load_tool_contracts()["query_market_data"].arguments_schema).is_valid(
        invalid_requests["argument_policy_injection"]["arguments"]
    )
    for result in invalid_results.values():
        assert not result_schema.is_valid(result)


def test_real_facade_describe_projection_matches_its_selected_success_schema() -> None:
    requests = json.loads((FIXTURE_PATH / "requests.json").read_text(encoding="utf-8"))
    request = deepcopy(requests["describe_market_data"])
    facade = AgentToolFacade(handle_secret=b"c" * 32)

    result = facade.execute("describe_market_data", request)

    assert result["status"] == "ok"
    data = result["data"]
    assert isinstance(data, dict)
    _validator(load_tool_contracts()["describe_market_data"].success_data_schema).validate(data)
