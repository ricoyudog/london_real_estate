"""Bounded model-facing facade for canonical market-data operations."""

from .cli import main, run_cli
from .facade import AgentToolFacade, HOST_TOOL_NAMES, MODEL_TOOL_NAMES
from .host import AgentToolHost, AgentToolSession, HANDLE_FD
from .manifest import (
    Capability,
    CapabilityManifest,
    ManifestError,
    RefreshProfileCatalog,
    load_capability_manifest,
    load_refresh_profiles,
)
from .tool_contracts import (
    ToolContract,
    ToolContractCatalog,
    ToolContractError,
    default_tool_contracts,
    load_tool_contracts,
)

__all__ = [
    "AgentToolFacade",
    "AgentToolHost",
    "AgentToolSession",
    "Capability",
    "CapabilityManifest",
    "HANDLE_FD",
    "HOST_TOOL_NAMES",
    "MODEL_TOOL_NAMES",
    "ManifestError",
    "RefreshProfileCatalog",
    "ToolContract",
    "ToolContractCatalog",
    "ToolContractError",
    "default_tool_contracts",
    "load_capability_manifest",
    "load_refresh_profiles",
    "load_tool_contracts",
    "main",
    "run_cli",
]
