from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from nan_fung.agent_tools import AgentToolFacade, load_capability_manifest, load_refresh_profiles
from nan_fung.agent_tools.facade import _as_of
from nan_fung.agent_tools.protocol import InvalidArgument, utc_timestamp


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "agent_tools" / "v1"


def _fixture(path: str, name: str) -> dict[str, object]:
    fixtures = json.loads((FIXTURE_PATH / path).read_text(encoding="utf-8"))
    return deepcopy(fixtures[name])


def _facade() -> AgentToolFacade:
    return AgentToolFacade(handle_secret=b"c" * 32)


def _error_code(result: dict[str, object]) -> str:
    error = result["error"]
    assert isinstance(error, dict)
    code = error["code"]
    assert isinstance(code, str)
    return code


def test_packaged_manifest_and_profiles_define_the_launch_bank_rate_capability() -> None:
    manifest = load_capability_manifest()
    profiles = load_refresh_profiles()
    request = _fixture("requests.json", "describe_market_data")

    result = _facade().execute("describe_market_data", request)

    bank_rate_capability = manifest["uk.bank-rate-current"]
    assert bank_rate_capability.status == "supported"
    assert bank_rate_capability.numeric_value_field == "bank_rate_percent"
    assert bank_rate_capability.numeric_value_type == "decimal_string"
    assert bank_rate_capability.query_templates["metrics"].fixed_filters == {
        "datasource_id": ("boe.bank_rate.iudbedr",)
    }
    assert profiles["bank-rate-latest"].datasource_id == "boe.bank_rate.iudbedr"
    assert result["status"] == "ok"
    data = result["data"]
    assert isinstance(data, dict)
    capabilities = data["capabilities"]
    assert isinstance(capabilities, list)
    assert [entry["capability_id"] for entry in capabilities] == ["uk.bank-rate-current"]
    bank_rate = capabilities[0]
    assert bank_rate["status"] == "supported"
    assert "bank-rate-latest" in json.dumps(bank_rate)
    forbidden_fields = {
        "adapter_name",
        "collector",
        "endpoint",
        "credentials",
        "retention_internal",
        "operator_command",
    }
    assert not forbidden_fields & set(bank_rate)


def test_planning_activity_permits_the_canonical_city_geography_filter() -> None:
    manifest = load_capability_manifest()

    planning = manifest["london-planning-activity"]

    assert planning.status == "supported"
    assert planning.datasource_ids == ("pld.applications_search",)
    assert planning.query_templates["metrics"].allowed_filters == frozenset(
        {"geography_code", "source_date_from", "source_date_to"}
    )


def test_describe_intersects_host_allowlist_but_preserves_partial_and_blocked_coverage() -> None:
    request = _fixture("requests.json", "describe_market_data")
    host_context = request["host_context"]
    assert isinstance(host_context, dict)
    host_context["allowed_capability_ids"] = [
        "uk.bank-rate-current",
        "uk.postcode-resolution",
        "london-prime-rent",
    ]

    result = _facade().execute("describe_market_data", request)

    assert result["status"] == "ok"
    data = result["data"]
    assert isinstance(data, dict)
    capabilities = data["capabilities"]
    assert isinstance(capabilities, list)
    statuses = {entry["capability_id"]: entry["status"] for entry in capabilities}
    assert statuses == {
        "uk.bank-rate-current": "supported",
        "uk.postcode-resolution": "partial",
        "london-prime-rent": "blocked",
    }


def test_query_fails_closed_when_the_capability_is_blocked_or_not_granted() -> None:
    blocked = _fixture("requests.json", "query_bank_rate")
    blocked_arguments = blocked["arguments"]
    blocked_context = blocked["host_context"]
    assert isinstance(blocked_arguments, dict)
    assert isinstance(blocked_context, dict)
    blocked_arguments["capability_id"] = "london-prime-rent"
    blocked_context["allowed_capability_ids"] = ["london-prime-rent"]
    blocked_result = _facade().execute("query_market_data", blocked)

    assert blocked_result["status"] == "error"
    assert _error_code(blocked_result) == "CAPABILITY_BLOCKED"

    ungranted = _fixture("requests.json", "query_bank_rate")
    ungranted_context = ungranted["host_context"]
    assert isinstance(ungranted_context, dict)
    ungranted_context["allowed_capability_ids"] = []
    ungranted_result = _facade().execute("query_market_data", ungranted)

    assert ungranted_result["status"] == "error"
    assert _error_code(ungranted_result) == "ACCESS_DENIED"


def test_model_cannot_inject_host_policy_or_network_fields_into_a_query() -> None:
    request = _fixture("invalid-requests.json", "argument_policy_injection")

    result = _facade().execute("query_market_data", request)

    assert result["status"] == "error"
    assert _error_code(result) == "INVALID_ARGUMENT"


def test_query_disabled_partial_capability_is_not_a_query_bypass() -> None:
    request = _fixture("requests.json", "query_bank_rate")
    arguments = request["arguments"]
    context = request["host_context"]
    assert isinstance(arguments, dict)
    assert isinstance(context, dict)
    arguments.update(
        {
            "capability_id": "uk.postcode-resolution",
            "query_kind": "geographies",
            "filters": {},
            "limit": 1,
        }
    )
    context["allowed_capability_ids"] = ["uk.postcode-resolution"]
    context["allowed_refresh_profiles"] = ["onspd-one-postcode"]

    result = _facade().execute("query_market_data", request)

    assert result["status"] == "error"
    assert _error_code(result) == "CAPABILITY_BLOCKED"


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        ({"filters": {"datasource_id": "other.datasource"}}, "INVALID_ARGUMENT"),
        ({"filters": {"provider": "untrusted"}}, "INVALID_ARGUMENT"),
        ({"query_kind": "supply"}, "INVALID_ARGUMENT"),
        ({"limit": 0}, "INVALID_ARGUMENT"),
        ({"limit": 21}, "INVALID_ARGUMENT"),
        ({"as_of": "2026-08-01T12:00:00+00:00"}, "INVALID_ARGUMENT"),
        ({"as_of": "2026-08-01Z"}, "INVALID_ARGUMENT"),
    ],
)
def test_query_rejects_template_escape_and_model_bounds(
    changes: dict[str, object], expected_code: str
) -> None:
    request = _fixture("requests.json", "query_bank_rate")
    arguments = request["arguments"]
    assert isinstance(arguments, dict)
    arguments.update(changes)

    result = _facade().execute("query_market_data", request)

    assert result["status"] == "error"
    assert _error_code(result) == expected_code


def test_query_rejects_an_invalid_host_access_class_before_data_access() -> None:
    request = _fixture("requests.json", "query_bank_rate")
    context = request["host_context"]
    assert isinstance(context, dict)
    context["allowed_access_classes"] = ["not-an-access-class"]

    result = _facade().execute("query_market_data", request)

    assert result["status"] == "error"
    assert _error_code(result) == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-01T12:00:00Z",
        "2024-02-29T23:59:59.1Z",
        "2024-02-29T23:59:59.123456Z",
    ],
)
def test_as_of_accepts_only_strict_rfc3339_utc_instants(value: str) -> None:
    parsed = _as_of(value)

    assert parsed is not None
    assert utc_timestamp(parsed).startswith(value.removesuffix("Z"))
    assert utc_timestamp(parsed).endswith("Z")


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-01",
        "2026-08-01Z",
        "2026-08-01 12:00:00Z",
        "2026-08-01T12:00Z",
        "2026-08-01T12:00:00+00:00",
        "2026-08-01T12:00:00z",
        "2026-08-01T12:00:00.1234567Z",
        "2026-08-01T12:00:60Z",
        "2026-02-30T12:00:00Z",
    ],
)
def test_as_of_rejects_noncanonical_or_invalid_utc_inputs(value: str) -> None:
    with pytest.raises(InvalidArgument):
        _as_of(value)
