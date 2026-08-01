"""Datasource ingestion contracts and trusted local workflow primitives.

This package deliberately exposes contracts, policies, and orchestration
helpers only.  Agent-facing code should use :mod:`nan_fung.read_api` and
:mod:`nan_fung.refresh_api` instead of importing any collector or writer from
here.
"""

from .bank_rate import (
    BANK_RATE_DATASOURCE_ID,
    AcquiredArtifact,
    BankRateLifecycle,
    BankRateLifecycleResult,
    BankRateRecord,
    parse_bank_rate_csv,
)
from .canonical import canonical_json, content_sha256, new_id, record_key_hash
from .registry import (
    DatasourceDefinitionDescriptor,
    DatasourceRegistry,
    RuntimeBindings,
    SourceDefinitionDescriptor,
    default_registry,
    default_runtime_bindings,
)

__all__ = [
    "BANK_RATE_DATASOURCE_ID",
    "AcquiredArtifact",
    "BankRateLifecycle",
    "BankRateLifecycleResult",
    "BankRateRecord",
    "DatasourceDefinitionDescriptor",
    "DatasourceRegistry",
    "RuntimeBindings",
    "SourceDefinitionDescriptor",
    "canonical_json",
    "content_sha256",
    "default_registry",
    "default_runtime_bindings",
    "new_id",
    "parse_bank_rate_csv",
    "record_key_hash",
]
