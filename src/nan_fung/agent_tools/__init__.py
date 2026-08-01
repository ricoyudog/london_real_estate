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
    "load_capability_manifest",
    "load_refresh_profiles",
    "main",
    "run_cli",
]
