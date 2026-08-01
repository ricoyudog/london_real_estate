from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from nan_fung.datasources.common import HostThrottleBlocked
from nan_fung.operational import OperationalStore


def test_host_gate_paces_requests_and_persists_retry_after_across_reopen(tmp_path) -> None:
    started_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    current = [started_at]
    gate = OperationalStore(tmp_path).host_throttle_gate(clock=lambda: current[0])

    gate.permit("WWW.BankOfEngland.Co.Uk.")
    first = gate.record_response(
        "www.bankofengland.co.uk", status=200, retry_after=None
    )

    assert first is None
    with pytest.raises(HostThrottleBlocked) as paced:
        gate.permit("www.bankofengland.co.uk")
    assert paced.value.blocked_until == started_at + timedelta(seconds=1)

    current[0] = started_at + timedelta(seconds=1)
    gate.permit("www.bankofengland.co.uk")
    blocked_until = gate.record_response(
        "www.bankofengland.co.uk", status=429, retry_after="120"
    )
    assert blocked_until == started_at + timedelta(seconds=121)

    reopened = OperationalStore(tmp_path).host_throttle_gate(clock=lambda: current[0])
    with pytest.raises(HostThrottleBlocked) as persisted:
        reopened.permit("www.bankofengland.co.uk")
    assert persisted.value.blocked_until == blocked_until

    http_deadline = started_at + timedelta(minutes=5)
    extended_until = reopened.record_response(
        "www.bankofengland.co.uk",
        status=429,
        retry_after=format_datetime(http_deadline, usegmt=True),
    )
    assert extended_until == http_deadline

    current[0] = http_deadline
    reopened.permit("www.bankofengland.co.uk")
    assert reopened.record_response(
        "www.bankofengland.co.uk", status=200, retry_after=None
    ) is None
