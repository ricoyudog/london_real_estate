from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from nan_fung.agent_tools import AgentToolHost


def _open(host: AgentToolHost, scope: str):
    return host.open_session(
        principal="competition-agent",
        allowed_access_classes=["open"],
        allowed_capability_ids=["uk.bank-rate-current"],
        allowed_refresh_profiles=["bank-rate-latest"],
        capability_scope_id=scope,
    )


def test_closed_scope_cannot_be_reopened_by_the_same_host() -> None:
    host = AgentToolHost(handle_secret=b"scope-registry-test-key-00000000")
    session = _open(host, "scope_no_reuse_001")

    session.close()
    session.close()

    with pytest.raises(ValueError, match="already been used"):
        _open(host, "scope_no_reuse_001")


def test_concurrent_duplicate_scope_reservation_has_exactly_one_winner() -> None:
    host = AgentToolHost(handle_secret=b"scope-registry-test-key-00000000")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_open, host, "scope_concurrent_001") for _ in range(2)]

    successes = 0
    failures = 0
    for future in futures:
        try:
            session = future.result()
        except ValueError as error:
            assert "already been used" in str(error)
            failures += 1
        else:
            session.close()
            successes += 1

    assert (successes, failures) == (1, 1)


def test_host_generated_scopes_are_distinct_and_remain_tombstoned_after_close() -> None:
    host = AgentToolHost(handle_secret=b"scope-registry-test-key-00000000")
    first = host.open_session(
        principal="competition-agent",
        allowed_access_classes=["open"],
        allowed_capability_ids=["uk.bank-rate-current"],
        allowed_refresh_profiles=["bank-rate-latest"],
    )
    second = host.open_session(
        principal="competition-agent",
        allowed_access_classes=["open"],
        allowed_capability_ids=["uk.bank-rate-current"],
        allowed_refresh_profiles=["bank-rate-latest"],
    )

    assert first.capability_scope_id != second.capability_scope_id
    first.close()
    second.close()
    with pytest.raises(ValueError, match="already been used"):
        _open(host, first.capability_scope_id)
