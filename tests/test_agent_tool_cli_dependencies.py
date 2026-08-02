from __future__ import annotations

from types import SimpleNamespace

import pytest

from nan_fung.agent_tools import cli


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("describe_market_data", {"read_service"}),
        ("query_market_data", {"read_service", "citation_projection"}),
        ("get_citation_metadata", {"citation_projection"}),
        ("request_data_refresh", {"read_service", "refresh_broker", "approval_store"}),
        ("get_refresh_status", {"refresh_broker"}),
        ("approve_refresh", {"refresh_broker", "approval_store"}),
    ],
)
def test_runtime_facade_constructs_only_the_selector_dependency_graph(
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
    expected: set[str],
) -> None:
    calls: list[str] = []
    config = SimpleNamespace(data_dir="data-dir", backup_dir="backup-dir", database_path="database-path")

    def mark(name: str):
        def constructor(*_args: object, **_kwargs: object) -> object:
            calls.append(name)
            return SimpleNamespace(name=name)

        return constructor

    monkeypatch.setattr(cli, "load_handle_secret_from_fd", lambda: b"x" * 32)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "SQLiteReadRepository", mark("repository"))
    monkeypatch.setattr(cli, "ReadService", mark("read_service"))
    monkeypatch.setattr(cli, "OperationalStore", mark("approval_store"))
    monkeypatch.setattr(cli, "OperationalRefreshBackend", mark("refresh_backend"))
    monkeypatch.setattr(cli, "RefreshBroker", mark("refresh_broker"))

    facade = cli._runtime_facade(selector)

    actual = {
        name
        for name, attribute in (
            ("read_service", "_read_service"),
            ("citation_projection", "_citation_projection"),
            ("refresh_broker", "_refresh_broker"),
            ("approval_store", "_approval_store"),
        )
        if getattr(facade, attribute) is not None
    }
    assert actual == expected

    if selector in {"describe_market_data", "query_market_data", "get_citation_metadata"}:
        assert {"approval_store", "refresh_backend", "refresh_broker"}.isdisjoint(calls)
    if selector == "get_refresh_status":
        assert {"repository", "read_service"}.isdisjoint(calls)
    if selector == "approve_refresh":
        assert {"repository", "read_service"}.isdisjoint(calls)


def test_runtime_facade_rejects_unknown_selector_before_constructing_dependencies() -> None:
    with pytest.raises(ValueError, match="unknown agent tool selector"):
        cli._runtime_facade("not-a-selector")
