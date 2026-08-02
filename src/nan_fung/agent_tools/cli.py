"""One-selector executable entry point for the agent-tool facade.

This module deliberately does not reuse the ``cre`` parser.  Its only public
process contract is ``nan-fung-agent-tools <tool-name>`` plus one JSON document
on stdin/stdout.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
import sys
from typing import BinaryIO, Protocol

from nan_fung.config import ConfigurationError, load_config
from nan_fung.operational import OperationalStore
from nan_fung.read_api import ReadService, SQLiteReadRepository
from nan_fung.refresh_api import OperationalRefreshBackend, RefreshBroker, RefreshProfile

from .facade import AgentToolFacade, HOST_TOOL_NAMES
from .handles import load_handle_secret_from_fd
from .manifest import AgentRefreshProfile, RefreshProfileCatalog, load_refresh_profiles
from .protocol import (
    AgentToolError,
    InternalError,
    ProtocolError,
    RetryableUnavailable,
    SchemaViolation,
    error_result,
    exit_code_for_result,
    read_request,
    validate_result,
    write_result,
)


class FacadeExecutor(Protocol):
    def execute(self, tool_name: str, request: Mapping[str, object]) -> dict[str, object]: ...


def main(argv: Sequence[str] | None = None) -> int:
    """Run the installed console script without emitting diagnostics to stdout."""

    return run_cli(
        list(sys.argv[1:] if argv is None else argv),
        stdin=sys.stdin.buffer,
        stdout=sys.stdout.buffer,
    )


def run_cli(
    argv: Sequence[str],
    *,
    facade: FacadeExecutor | None = None,
    stdin: BinaryIO,
    stdout: BinaryIO,
) -> int:
    """Execute exactly one selected tool and write exactly one result object."""

    request_id: str | None = None
    try:
        if len(argv) != 1 or argv[0] not in HOST_TOOL_NAMES:
            raise SchemaViolation("exactly one known tool selector is required")
        request = read_request(stdin)
        candidate = request.get("request_id")
        request_id = candidate if isinstance(candidate, str) else None
        selected_facade = facade or _runtime_facade(argv[0])
        response = selected_facade.execute(argv[0], request)
        validate_result(response)
    except AgentToolError as error:
        response = error_result(request_id, error)
    except (ConfigurationError, OSError):
        response = error_result(request_id, RetryableUnavailable())
    except Exception:
        response = error_result(request_id, InternalError())
    try:
        write_result(stdout, response)
    except AgentToolError:
        # The response itself is deliberately tiny.  If an injected facade
        # returned an invalid/non-serializable object, replace it once with a
        # stable protocol failure rather than producing partial stdout.
        fallback = error_result(request_id, ProtocolError())
        try:
            write_result(stdout, fallback)
        except Exception:
            return 6
        return exit_code_for_result(fallback)
    except Exception:
        return 6
    return exit_code_for_result(response)


def _runtime_facade(tool_name: str) -> AgentToolFacade:
    """Build only the selected child process's minimum dependency graph."""

    if tool_name not in HOST_TOOL_NAMES:
        raise ValueError("unknown agent tool selector")
    handle_secret = load_handle_secret_from_fd()

    read_service: ReadService | None = None
    citation_projection: SQLiteReadRepository | None = None
    refresh_broker: RefreshBroker | None = None
    approval_store: OperationalStore | None = None
    profiles: RefreshProfileCatalog | None = None

    if tool_name in {"describe_market_data", "query_market_data", "get_citation_metadata", "request_data_refresh"}:
        config = load_config()
        read_repository = SQLiteReadRepository(config.database_path)
        if tool_name in {"describe_market_data", "query_market_data", "request_data_refresh"}:
            read_service = ReadService(read_repository, cursor_secret=handle_secret)
        if tool_name in {"query_market_data", "get_citation_metadata"}:
            citation_projection = read_repository

    if tool_name in {"request_data_refresh", "get_refresh_status", "approve_refresh"}:
        config = load_config()
        profiles = load_refresh_profiles()
        store = OperationalStore(config.data_dir, backup_dir=config.backup_dir)
        refresh_profiles = {
            profile.profile_id: _broker_profile(profile)
            for profile in profiles.values()
        }
        refresh_broker = RefreshBroker(
            refresh_profiles,
            OperationalRefreshBackend(store),
        )
        if tool_name in {"request_data_refresh", "approve_refresh"}:
            approval_store = store

    return AgentToolFacade(
        read_service=read_service,
        citation_projection=citation_projection,
        refresh_broker=refresh_broker,
        approval_store=approval_store,
        profiles=profiles,
        handle_secret=handle_secret,
    )


def _broker_profile(profile: AgentRefreshProfile) -> RefreshProfile:
    return RefreshProfile(
        profile_id=profile.profile_id,
        datasource_id=profile.datasource_id,
        definition_version=profile.definition_version,
        effective_lane=profile.effective_lane,
        allowed_scope_keys=profile.allowed_scope_keys,
        required_scope_keys=profile.required_scope_keys,
        single_value_scope_keys=profile.single_value_scope_keys,
        max_scope_values=profile.max_scope_values,
        cooldown=timedelta(minutes=5),
        poll_after=timedelta(seconds=profile.poll_after_seconds),
        promotion_policy=profile.promotion_policy,
    )


__all__ = ["main", "run_cli"]
