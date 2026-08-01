from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
from typing import Iterable

from nan_fung.agent_tools import AgentToolFacade
from nan_fung.read_api import (
    AccessClass,
    CitationProjection,
    InMemoryReadRepository,
    ReadRecord,
    ReadService,
)


NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "agent_tools" / "v1"


class CitationResolver:
    """A test-only exact-lineage provider usable as a callable or repository."""

    def __init__(self, *, missing_optional_metadata: bool = False) -> None:
        self.calls: list[tuple[datetime, tuple[str, ...]]] = []
        self._missing_optional_metadata = missing_optional_metadata

    def citation_projection(
        self,
        _context: object,
        *,
        anchor_as_of: datetime,
        observation_ids: Iterable[str],
    ) -> tuple[CitationProjection, ...]:
        ids = tuple(observation_ids)
        self.calls.append((anchor_as_of, ids))
        return tuple(self._projection(anchor_as_of, observation_id) for observation_id in ids)

    def __call__(self, *args: object, **kwargs: object) -> tuple[CitationProjection, ...]:
        anchor = kwargs.get("anchor_as_of")
        observations = kwargs.get("observation_ids")
        assert isinstance(anchor, datetime)
        assert observations is not None
        return self.citation_projection(
            args[-1] if args else object(),
            anchor_as_of=anchor,
            observation_ids=observations,  # type: ignore[arg-type]
        )

    def _projection(self, anchor: datetime, observation_id: str) -> CitationProjection:
        return CitationProjection(
            anchor_as_of=anchor,
            canonical_run_id="run_bank_rate_canonical",
            observation_id=observation_id,
            evidence_id=f"ev_{observation_id}",
            locator_hash="a" * 64,
            datasource_id="boe.bank_rate.iudbedr",
            publisher="Bank of England",
            retrieved_at=NOW,
            access_class="open",
            data_kind="numeric_observation",
            confidence="high",
            limitations=("UK macro metric; not a London office rent measure.",),
            locator={"kind": "csv_row", "row_key": observation_id},
            title=None if self._missing_optional_metadata else "Official Bank Rate data",
            public_url=(
                None
                if self._missing_optional_metadata
                else "https://www.bankofengland.co.uk/boeapps/database/Bank-Rate"
            ),
            published_at=None,
            source_updated_at=None,
            licence_or_attribution="Bank of England Open Data",
            warnings=("published_at_unavailable", "source_updated_at_unavailable"),
        )


def _record(
    observation_id: str,
    *,
    minutes_ago: int,
    rate: str,
    padding: str | None = None,
) -> ReadRecord:
    payload: dict[str, object] = {"bank_rate_percent": rate}
    if padding is not None:
        payload["padding"] = padding
    return ReadRecord(
        observation_id=observation_id,
        datasource_id="boe.bank_rate.iudbedr",
        query_kind="metrics",
        category="macro",
        record_type="metric",
        access_class=AccessClass.OPEN,
        available_at=NOW - timedelta(minutes=minutes_ago),
        payload=payload,
        evidence_ids=(f"ev_{observation_id}",),
        source_date=date(2026, 7, 31),
        retrieved_at=NOW,
        unit="percent",
        definition="Official Bank Rate",
        period_label="31 Jul 2026",
        retrieval_freshness="fresh",
        observation_freshness="fresh",
    )


def _facade(
    resolver: CitationResolver, *, handle_clock: Callable[[], datetime] | None = None
) -> AgentToolFacade:
    repository = InMemoryReadRepository(
        (
            _record("obs_3", minutes_ago=1, rate="3.75"),
            _record("obs_2", minutes_ago=2, rate="4.00"),
            _record("obs_1", minutes_ago=3, rate="4.25"),
        )
    )
    return AgentToolFacade(
        read_service=ReadService(
            repository,
            cursor_secret=b"read-cursor-secret",
            clock=lambda: NOW,
        ),
        citation_projection=resolver,
        handle_secret=b"q" * 32,
        clock=handle_clock or (lambda: NOW),
    )


def _fixture(name: str) -> dict[str, object]:
    fixtures = json.loads((FIXTURE_PATH / "requests.json").read_text(encoding="utf-8"))
    return deepcopy(fixtures[name])


def _citation_request(citation_ref: str) -> dict[str, object]:
    request = _fixture("describe_market_data")
    request["request_id"] = "call_citation_001"
    request["arguments"] = {"citation_refs": [citation_ref]}
    context = request["host_context"]
    assert isinstance(context, dict)
    context["tool_call_id"] = "toolcall_citation_001"
    return request


def _error_code(result: dict[str, object]) -> str:
    error = result["error"]
    assert isinstance(error, dict)
    code = error["code"]
    assert isinstance(code, str)
    return code


def test_query_projects_the_fixed_bank_rate_numeric_field_and_scoped_pagination() -> None:
    resolver = CitationResolver()
    facade = _facade(resolver)
    first = facade.execute("query_market_data", _fixture("query_bank_rate"))

    assert first["status"] == "ok"
    first_data = first["data"]
    assert isinstance(first_data, dict)
    first_records = first_data["records"]
    assert isinstance(first_records, list)
    assert [record["observation_id"] for record in first_records] == ["obs_3", "obs_2"]
    numeric = first_records[0]["numeric"]
    assert numeric == {
        "value": "3.75",
        "unit": "percent",
        "definition": "Official Bank Rate",
        "as_of": first_data["anchor_as_of"],
        "source_date": "2026-07-31",
        "period_label": "31 Jul 2026",
    }
    citation_ref = first_records[0]["citation_refs"][0]
    cursor_ref = first_data["cursor_ref"]
    assert isinstance(citation_ref, str)
    assert isinstance(cursor_ref, str)

    second_request = _fixture("query_bank_rate")
    second_request["request_id"] = "call_query_002"
    arguments = second_request["arguments"]
    context = second_request["host_context"]
    assert isinstance(arguments, dict)
    assert isinstance(context, dict)
    arguments["cursor_ref"] = cursor_ref
    context["tool_call_id"] = "toolcall_query_002"
    second = facade.execute("query_market_data", second_request)

    assert second["status"] == "ok"
    second_data = second["data"]
    assert isinstance(second_data, dict)
    second_records = second_data["records"]
    assert isinstance(second_records, list)
    assert [record["observation_id"] for record in second_records] == ["obs_1"]
    assert second_data["cursor_ref"] is None
    assert resolver.calls
    anchor_as_of = first_data["anchor_as_of"]
    assert isinstance(anchor_as_of, str)
    assert {anchor for anchor, _ids in resolver.calls} == {
        datetime.fromisoformat(anchor_as_of.replace("Z", "+00:00"))
    }

    replayed_elsewhere = _fixture("query_bank_rate")
    replayed_elsewhere["request_id"] = "call_query_003"
    replayed_arguments = replayed_elsewhere["arguments"]
    replayed_context = replayed_elsewhere["host_context"]
    assert isinstance(replayed_arguments, dict)
    assert isinstance(replayed_context, dict)
    replayed_arguments["cursor_ref"] = cursor_ref
    replayed_context["capability_scope_id"] = "scope_fedcba9876543210fedcba9876543210"
    denied = facade.execute("query_market_data", replayed_elsewhere)
    assert _error_code(denied) == "POLICY_DENIED"


def test_cursor_and_citation_handles_reject_tampering_kind_binding_scope_and_expiry() -> None:
    current = [NOW]
    facade = _facade(CitationResolver(), handle_clock=lambda: current[0])
    first = facade.execute("query_market_data", _fixture("query_bank_rate"))
    first_data = first["data"]
    assert isinstance(first_data, dict)
    records = first_data["records"]
    assert isinstance(records, list)
    cursor_ref = first_data["cursor_ref"]
    citation_ref = records[0]["citation_refs"][0]
    assert isinstance(cursor_ref, str)
    assert isinstance(citation_ref, str)

    def query_with(cursor: str, *, filters: dict[str, object] | None = None) -> dict[str, object]:
        request = _fixture("query_bank_rate")
        request["request_id"] = "call_handle_query"
        arguments = request["arguments"]
        assert isinstance(arguments, dict)
        arguments["cursor_ref"] = cursor
        if filters is not None:
            arguments["filters"] = filters
        return request

    tampered = facade.execute("query_market_data", query_with(f"{cursor_ref}x"))
    wrong_kind = facade.execute("query_market_data", query_with(citation_ref))
    changed_binding = facade.execute(
        "query_market_data",
        query_with(cursor_ref, filters={"source_date_from": "2026-07-01"}),
    )
    other_scope_request = query_with(cursor_ref)
    other_context = other_scope_request["host_context"]
    assert isinstance(other_context, dict)
    other_context["capability_scope_id"] = "scope_fedcba9876543210fedcba9876543210"
    other_scope = facade.execute("query_market_data", other_scope_request)

    assert _error_code(tampered) == "INVALID_CURSOR"
    assert _error_code(wrong_kind) == "INVALID_CURSOR"
    assert _error_code(changed_binding) == "INVALID_CURSOR"
    assert _error_code(other_scope) == "POLICY_DENIED"

    tampered_citation = facade.execute(
        "get_citation_metadata", _citation_request(f"{citation_ref}x")
    )
    assert _error_code(tampered_citation) == "INVALID_CURSOR"

    current[0] += timedelta(minutes=31)
    expired = facade.execute("query_market_data", query_with(cursor_ref))
    expired_citation = facade.execute(
        "get_citation_metadata", _citation_request(citation_ref)
    )
    assert _error_code(expired) == "INVALID_CURSOR"
    assert _error_code(expired_citation) == "INVALID_CURSOR"


def test_citations_are_exact_scope_bound_metadata_not_bare_lineage_ids() -> None:
    resolver = CitationResolver()
    facade = _facade(resolver)
    query_request = _fixture("query_bank_rate")
    query_arguments = query_request["arguments"]
    assert isinstance(query_arguments, dict)
    query_arguments["as_of"] = "2026-08-01T11:59:30Z"
    query = facade.execute("query_market_data", query_request)
    query_data = query["data"]
    assert isinstance(query_data, dict)
    records = query_data["records"]
    assert isinstance(records, list)
    citation_ref = records[0]["citation_refs"][0]
    assert isinstance(citation_ref, str)

    resolved = facade.execute("get_citation_metadata", _citation_request(citation_ref))

    assert resolved["status"] == "partial"
    resolved_data = resolved["data"]
    assert isinstance(resolved_data, dict)
    citations = resolved_data["citations"]
    assert isinstance(citations, list)
    citation = citations[0]
    assert citation["citation_ref"] == citation_ref
    for key in (
        "observation_id",
        "evidence_id",
        "datasource_id",
        "publisher",
        "retrieved_at",
        "access_class",
        "data_kind",
        "confidence",
        "limitations",
        "locator",
        "title",
        "public_url",
        "published_at",
        "source_updated_at",
        "licence_or_attribution",
    ):
        assert key in citation
    assert citation["observation_id"] == "obs_3"
    assert citation["evidence_id"] == "ev_obs_3"
    assert "artifact_uri" not in citation
    assert "raw" not in json.dumps(citation).lower()
    assert resolver.calls
    assert {anchor for anchor, _ids in resolver.calls} == {
        datetime(2026, 8, 1, 11, 59, 30, tzinfo=UTC)
    }

    bare_id = _citation_request("obs_3")
    bare_result = facade.execute("get_citation_metadata", bare_id)
    assert bare_result["status"] == "error"
    assert _error_code(bare_result) in {"INVALID_ARGUMENT", "INVALID_CURSOR"}

    other_scope = _citation_request(citation_ref)
    other_context = other_scope["host_context"]
    assert isinstance(other_context, dict)
    other_context["capability_scope_id"] = "scope_fedcba9876543210fedcba9876543210"
    denied = facade.execute("get_citation_metadata", other_scope)
    assert _error_code(denied) == "POLICY_DENIED"


def test_citation_missing_user_metadata_is_partial_with_a_field_warning() -> None:
    facade = _facade(CitationResolver(missing_optional_metadata=True))
    query = facade.execute("query_market_data", _fixture("query_bank_rate"))
    query_data = query["data"]
    assert isinstance(query_data, dict)
    records = query_data["records"]
    assert isinstance(records, list)
    citation_ref = records[0]["citation_refs"][0]

    resolved = facade.execute("get_citation_metadata", _citation_request(citation_ref))

    assert resolved["status"] == "partial"
    citations_data = resolved["data"]
    assert isinstance(citations_data, dict)
    citations = citations_data["citations"]
    assert isinstance(citations, list)
    assert citations[0]["title"] is None
    assert citations[0]["public_url"] is None
    warnings = resolved["warnings"]
    assert isinstance(warnings, list)
    assert warnings
    assert any(
        "title" in str(warning) or "public_url" in str(warning)
        for warning in warnings
    )


def test_size_truncated_query_pages_without_skipping_or_duplicating_records() -> None:
    resolver = CitationResolver()
    records = tuple(
        _record(
            f"obs_large_{index}",
            minutes_ago=index,
            rate="3.75",
            padding="x" * 54_000,
        )
        for index in range(1, 6)
    )
    facade = AgentToolFacade(
        read_service=ReadService(
            InMemoryReadRepository(records),
            cursor_secret=b"large-read-cursor-secret",
            clock=lambda: NOW,
        ),
        citation_projection=resolver,
        handle_secret=b"l" * 32,
        clock=lambda: NOW,
    )
    first_request = _fixture("query_bank_rate")
    first_arguments = first_request["arguments"]
    assert isinstance(first_arguments, dict)
    first_arguments["limit"] = 20

    first = facade.execute("query_market_data", first_request)

    assert first["status"] == "partial"
    assert len(json.dumps(first, ensure_ascii=False).encode("utf-8")) <= 256 * 1024
    first_data = first["data"]
    assert isinstance(first_data, dict)
    first_records = first_data["records"]
    assert isinstance(first_records, list)
    assert 0 < len(first_records) < len(records)
    cursor_ref = first_data["cursor_ref"]
    assert isinstance(cursor_ref, str)

    second_request = _fixture("query_bank_rate")
    second_request["request_id"] = "call_large_query_002"
    second_arguments = second_request["arguments"]
    second_context = second_request["host_context"]
    assert isinstance(second_arguments, dict)
    assert isinstance(second_context, dict)
    second_arguments["limit"] = 20
    second_arguments["cursor_ref"] = cursor_ref
    second_context["tool_call_id"] = "toolcall_large_query_002"
    second = facade.execute("query_market_data", second_request)

    assert second["status"] in {"ok", "partial"}
    second_data = second["data"]
    assert isinstance(second_data, dict)
    second_records = second_data["records"]
    assert isinstance(second_records, list)
    emitted_ids = [record["observation_id"] for record in first_records + second_records]
    expected_ids = [f"obs_large_{index}" for index in range(1, 6)]
    assert emitted_ids == expected_ids


def test_query_keeps_last_good_canonical_freshness_and_degraded_state_visible() -> None:
    stale_record = replace(
        _record("obs_stale", minutes_ago=1, rate="3.75"),
        retrieval_freshness="stale",
        observation_freshness="stale",
        degraded=True,
        canonical_available=True,
    )
    facade = AgentToolFacade(
        read_service=ReadService(
            InMemoryReadRepository((stale_record,)),
            cursor_secret=b"stale-read-cursor-secret",
            clock=lambda: NOW,
        ),
        citation_projection=CitationResolver(),
        handle_secret=b"s" * 32,
        clock=lambda: NOW,
    )

    result = facade.execute("query_market_data", _fixture("query_bank_rate"))

    assert result["status"] in {"ok", "partial"}
    data = result["data"]
    assert isinstance(data, dict)
    records = data["records"]
    assert isinstance(records, list)
    record = records[0]
    assert record["canonical_available"] is True
    assert record["degraded"] is True
    assert record["retrieval_freshness"] == "stale"
    assert record["observation_freshness"] == "stale"


def test_one_complete_record_larger_than_the_wire_limit_returns_no_progress_error() -> None:
    oversized_record = replace(
        _record("obs_oversized", minutes_ago=1, rate="3.75"),
        definition="d" * (256 * 1024),
    )
    facade = AgentToolFacade(
        read_service=ReadService(
            InMemoryReadRepository((oversized_record,)),
            cursor_secret=b"oversized-read-cursor-secret",
            clock=lambda: NOW,
        ),
        citation_projection=CitationResolver(),
        handle_secret=b"o" * 32,
        clock=lambda: NOW,
    )

    result = facade.execute("query_market_data", _fixture("query_bank_rate"))

    assert result["status"] == "error"
    assert _error_code(result) == "RESULT_TOO_LARGE"
    assert result["data"] is None
