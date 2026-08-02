"""Versioned, selector-specific contracts for non-Python tool consumers.

The argv selector intentionally stays outside the generic wire envelope. This
catalog supplies the companion arguments and successful-data schema for each
selector without duplicating a ``tool`` field in stdin JSON.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import json
from importlib import resources
from types import MappingProxyType

from .facade import HOST_TOOL_NAMES, MODEL_TOOL_NAMES


TOOL_CONTRACT_ASSET = "agent_tool_contracts.v1.json"
TOOL_CONTRACT_CATALOG_SCHEMA_ASSET = "agent_tool_contract_catalog.v1.schema.json"
TOOL_CONTRACT_SCHEMA_VERSION = "agent_tool_contracts.v1"


class ToolContractError(ValueError):
    """Raised when the packaged selector contract catalog is unsafe."""


@dataclass(frozen=True)
class ToolContract:
    """The selector contract consumed by a trusted non-Python host."""

    selector: str
    audience: str
    refresh_request_id: str
    arguments_schema: dict[str, object]
    success_data_schema: dict[str, object]


class ToolContractCatalog(Mapping[str, ToolContract]):
    """Validated immutable selector mapping plus model/host projections."""

    def __init__(self, *, version: str, contracts: Mapping[str, ToolContract]) -> None:
        if not version:
            raise ToolContractError("catalog version is required")
        if any(selector != contract.selector for selector, contract in contracts.items()):
            raise ToolContractError("catalog selector mapping mismatch")
        model_selectors = frozenset(
            selector for selector, contract in contracts.items() if contract.audience == "model"
        )
        host_selectors = frozenset(contracts)
        if model_selectors != MODEL_TOOL_NAMES or host_selectors != HOST_TOOL_NAMES:
            raise ToolContractError("catalog selectors do not match the facade surface")
        self.version = version
        self._contracts = MappingProxyType(dict(contracts))
        self.model_selectors = tuple(sorted(model_selectors))
        self.host_selectors = tuple(sorted(host_selectors))

    def __getitem__(self, selector: str) -> ToolContract:
        return self._contracts[selector]

    def __iter__(self) -> Iterator[str]:
        return iter(self._contracts)

    def __len__(self) -> int:
        return len(self._contracts)


def load_tool_contracts() -> ToolContractCatalog:
    """Load the wheel-packaged selector catalog without a JSON Schema runtime."""

    value = _load_asset(TOOL_CONTRACT_ASSET)
    if value.get("schema_version") != TOOL_CONTRACT_SCHEMA_VERSION:
        raise ToolContractError("unsupported tool contract schema")
    version = _text(value.get("catalog_version"), "catalog_version")
    raw_contracts = value.get("contracts")
    if not isinstance(raw_contracts, list) or not raw_contracts:
        raise ToolContractError("catalog contracts must be a non-empty array")
    contracts: dict[str, ToolContract] = {}
    for raw_contract in raw_contracts:
        if not isinstance(raw_contract, Mapping):
            raise ToolContractError("catalog contract must be an object")
        contract = _parse_contract(raw_contract)
        if contract.selector in contracts:
            raise ToolContractError("catalog selectors must be unique")
        contracts[contract.selector] = contract
    return ToolContractCatalog(version=version, contracts=contracts)


def default_tool_contracts() -> ToolContractCatalog:
    """Compatibility-friendly name for the packaged selector catalog."""

    return load_tool_contracts()


def _parse_contract(value: Mapping[str, object]) -> ToolContract:
    expected = {
        "selector",
        "audience",
        "refresh_request_id",
        "arguments_schema",
        "success_data_schema",
    }
    if set(value) != expected:
        raise ToolContractError("catalog contract fields do not match schema")
    selector = _text(value.get("selector"), "selector")
    audience = _text(value.get("audience"), "audience")
    refresh_request_id = _text(value.get("refresh_request_id"), "refresh_request_id")
    if audience not in {"model", "host"}:
        raise ToolContractError("catalog audience is invalid")
    if refresh_request_id not in {"required", "forbidden"}:
        raise ToolContractError("catalog refresh identity policy is invalid")
    arguments_schema = value.get("arguments_schema")
    success_data_schema = value.get("success_data_schema")
    if not isinstance(arguments_schema, Mapping) or not isinstance(success_data_schema, Mapping):
        raise ToolContractError("catalog schemas must be objects")
    return ToolContract(
        selector=selector,
        audience=audience,
        refresh_request_id=refresh_request_id,
        arguments_schema=dict(arguments_schema),
        success_data_schema=dict(success_data_schema),
    )


def _load_asset(name: str) -> Mapping[str, object]:
    try:
        raw = resources.files("nan_fung.agent_tools").joinpath(name).read_text(encoding="utf-8")
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (FileNotFoundError, ModuleNotFoundError, ValueError, json.JSONDecodeError) as error:
        raise ToolContractError("packaged tool contract asset is unavailable") from error
    if not isinstance(value, Mapping):
        raise ToolContractError("catalog root must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ToolContractError(f"{name} must be a bounded non-empty string")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


__all__ = [
    "TOOL_CONTRACT_ASSET",
    "TOOL_CONTRACT_CATALOG_SCHEMA_ASSET",
    "TOOL_CONTRACT_SCHEMA_VERSION",
    "ToolContract",
    "ToolContractCatalog",
    "ToolContractError",
    "default_tool_contracts",
    "load_tool_contracts",
]
