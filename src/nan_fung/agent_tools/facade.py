"""Model-facing facade above the typed read and refresh data-plane APIs.

The facade intentionally owns the agent wire contract.  It never exposes a
repository, a collector, an operator command, SQL, raw evidence, or a refresh
confirmation token to a model-facing result.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
import re
import sqlite3

from nan_fung.operational import (
    ApprovalDecisionConflictError,
    OperationalStore,
    RefreshApprovalAccessError,
    RefreshApprovalError,
    RefreshApprovalExpiredError,
    RefreshApprovalReplayError,
)
from nan_fung.read_api import (
    AccessClass,
    CitationProjection,
    CitationProjectionRepository,
    ReadContext,
    ReadQuery,
    ReadResponse,
    ReadService,
    citation_projection_v1,
    query_data_v1,
)
from nan_fung.read_api.contracts import (
    AccessDenied as ReadAccessDenied,
    InvalidCursor as ReadInvalidCursor,
    InvalidReadRequest,
    ReadApiError,
    ReadRecord,
)
from nan_fung.refresh_api import (
    InvalidRefreshRequest,
    RefreshAccessDenied,
    RefreshAcknowledgement,
    RefreshBroker,
    RefreshContext,
    RefreshDisposition,
    RefreshRequest,
    RefreshStatus,
    get_refresh_status_v1,
    request_refresh_v1,
)

from .handles import ScopedHandleCodec
from .manifest import (
    AgentRefreshProfile,
    Capability,
    CapabilityManifest,
    RefreshProfileCatalog,
    load_capability_manifest,
    load_refresh_profiles,
)
from .protocol import (
    AgentToolError,
    AgentToolRequest,
    AccessDenied,
    CapabilityBlocked,
    HostContext,
    InternalError,
    InvalidArgument,
    InvalidCursor,
    PolicyDenied,
    ResultTooLarge,
    RetryableUnavailable,
    SchemaViolation,
    error_result,
    parse_request,
    result,
    utc_timestamp,
)


MODEL_TOOL_NAMES = frozenset(
    {
        "describe_market_data",
        "query_market_data",
        "get_citation_metadata",
        "request_data_refresh",
        "get_refresh_status",
    }
)
HOST_TOOL_NAMES = MODEL_TOOL_NAMES | {"approve_refresh"}
MAX_MODEL_LIMIT = 20
_POSTCODE = re.compile(r"^(?:GIR0AA|[A-Z]{1,2}[0-9][A-Z0-9]?[0-9][A-Z]{2})$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AgentToolFacade:
    """Dispatch only the five approved model tools and one host-only selector.

    All dependencies are injected so read-only children can be built without a
    writer store, while refresh/approval children receive only the separately
    permissioned broker/store they require.
    """

    def __init__(
        self,
        *,
        read_service: ReadService | None = None,
        citation_projection: CitationProjectionRepository
        | Callable[..., Iterable[CitationProjection]]
        | None = None,
        refresh_broker: RefreshBroker | None = None,
        approval_store: OperationalStore | None = None,
        manifest: CapabilityManifest | None = None,
        profiles: RefreshProfileCatalog | None = None,
        handle_secret: bytes,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._read_service = read_service
        self._citation_projection = citation_projection
        self._refresh_broker = refresh_broker
        self._approval_store = approval_store
        self._manifest = manifest or load_capability_manifest()
        self._profiles = profiles or load_refresh_profiles()
        self._handles = ScopedHandleCodec(handle_secret, clock=clock)
        self._clock = clock

    def execute(
        self, tool_name: str, raw_request: Mapping[str, object] | object
    ) -> dict[str, object]:
        """Execute one selected tool and always return a safe wire envelope."""

        request_id = (
            raw_request.get("request_id")
            if isinstance(raw_request, Mapping) and isinstance(raw_request.get("request_id"), str)
            else None
        )
        try:
            if tool_name not in HOST_TOOL_NAMES:
                raise SchemaViolation("unknown agent tool selector")
            if not isinstance(raw_request, Mapping):
                raise SchemaViolation("request must be an object")
            request = parse_request(raw_request)
            self._validate_refresh_identity(tool_name, request.host_context)
            dispatch = {
                "describe_market_data": self._describe_market_data,
                "query_market_data": self._query_market_data,
                "get_citation_metadata": self._get_citation_metadata,
                "request_data_refresh": self._request_data_refresh,
                "get_refresh_status": self._get_refresh_status,
                "approve_refresh": self._approve_refresh,
            }[tool_name]
            status, data, warnings = dispatch(request)
            envelope = result(
                request.request_id,
                status=status,
                data=data,
                warnings=warnings,
            )
            if _wire_size(envelope) > 256 * 1024:
                raise ResultTooLarge("model result exceeds protocol limit")
            return envelope
        except AgentToolError as error:
            return error_result(request_id, error)
        except ReadInvalidCursor as error:
            return error_result(request_id, InvalidCursor(str(error)))
        except ReadAccessDenied as error:
            return error_result(request_id, AccessDenied(str(error)))
        except InvalidReadRequest as error:
            return error_result(request_id, InvalidArgument(str(error)))
        except ReadApiError:
            return error_result(request_id, InternalError())
        except RefreshAccessDenied as error:
            return error_result(request_id, AccessDenied(str(error)))
        except InvalidRefreshRequest as error:
            return error_result(request_id, InvalidArgument(str(error)))
        except (RefreshApprovalAccessError, RefreshApprovalExpiredError) as error:
            return error_result(request_id, AccessDenied(str(error)))
        except (RefreshApprovalReplayError, ApprovalDecisionConflictError) as error:
            return error_result(request_id, PolicyDenied(str(error)))
        except RefreshApprovalError:
            return error_result(request_id, InternalError())
        except OSError:
            return error_result(request_id, RetryableUnavailable())
        except Exception:
            return error_result(request_id, InternalError())

    def _describe_market_data(
        self, request: AgentToolRequest
    ) -> tuple[str, dict[str, object], list[str]]:
        _arguments(request.arguments, allowed=set(), required=set())
        context = request.host_context
        availability = self._availability(context)
        entries: list[dict[str, object]] = []
        for capability_id in sorted(context.allowed_capability_ids):
            capability = self._manifest.get(capability_id)
            if capability is None:
                continue
            item = capability.safe_projection(context.allowed_refresh_profiles)
            health = availability.get(capability_id)
            item["canonical_availability"] = health or _unknown_availability()
            entries.append(item)
        return "ok", {"capabilities": entries}, []

    def _query_market_data(
        self, request: AgentToolRequest
    ) -> tuple[str, dict[str, object], list[str]]:
        arguments = _arguments(
            request.arguments,
            allowed={"capability_id", "query_kind", "filters", "as_of", "cursor_ref", "limit"},
            required={"capability_id", "query_kind"},
        )
        capability = self._capability_for(request.host_context, arguments["capability_id"])
        if capability.query_disabled:
            raise CapabilityBlocked("capability does not support canonical query")
        query_kind = _text(arguments["query_kind"], "query_kind")
        try:
            template = capability.query_templates[query_kind]
        except KeyError as error:
            raise InvalidArgument("query kind is not enabled for this capability") from error
        filters = _query_filters(arguments.get("filters", {}), template)
        limit = _limit(arguments.get("limit", MAX_MODEL_LIMIT))
        read_context = _read_context(request.host_context)
        if "as_of" in arguments and arguments["as_of"] is None:
            raise InvalidArgument("as_of must be RFC3339 UTC")
        requested_as_of = _as_of(arguments["as_of"]) if "as_of" in arguments else None
        binding = _query_binding(capability.capability_id, query_kind, filters)
        internal_cursor: str | None = None
        if "cursor_ref" in arguments:
            cursor_ref = _text(arguments["cursor_ref"], "cursor_ref")
            payload = self._handles.verify(
                cursor_ref,
                "cursor",
                principal=request.host_context.principal,
                capability_scope_id=request.host_context.capability_scope_id,
                binding=binding,
            )
            if payload.get("capability_id") != capability.capability_id:
                raise InvalidCursor("cursor capability does not match")
            internal_cursor = _text(payload.get("read_cursor"), "read_cursor")
            anchor = _as_of(payload.get("anchor_as_of"))
            if anchor is None:
                raise InvalidCursor("cursor anchor is missing")
            if requested_as_of is not None and requested_as_of != anchor:
                raise InvalidCursor("cursor cannot be reused with another as_of")
            requested_as_of = anchor

        if self._read_service is None:
            raise RetryableUnavailable("read service is not configured")

        query = ReadQuery(
            query_kind=query_kind,
            filters=filters,
            as_of=requested_as_of,
            cursor=internal_cursor,
            limit=limit,
        )
        response = query_data_v1(self._read_service, read_context, query)
        return self._bounded_query_result(
            request,
            capability,
            read_context,
            query,
            internal_cursor,
            response,
            binding,
        )

    def _bounded_query_result(
        self,
        request: AgentToolRequest,
        capability: Capability,
        read_context: ReadContext,
        query: ReadQuery,
        internal_cursor: str | None,
        response: ReadResponse,
        binding: Mapping[str, object],
    ) -> tuple[str, dict[str, object], list[str]]:
        rendered, warnings = self._render_query_records(
            capability, read_context, response.anchor_as_of, response.records, request.host_context
        )
        data = self._query_data(
            capability,
            response,
            rendered,
            response.next_cursor,
            request.host_context,
            binding,
        )
        envelope = result(request.request_id, status="ok", data=data, warnings=warnings)
        if _wire_size(envelope) <= 256 * 1024:
            return "ok", data, warnings

        # Re-query with an exact smaller prefix.  The ReadService then mints a
        # data-plane cursor after the last *emitted* row, preventing skips.
        for prefix_size in range(len(response.records) - 1, 0, -1):
            prefix_query = ReadQuery(
                query_kind=query.query_kind,
                filters=query.filters,
                as_of=response.anchor_as_of,
                cursor=internal_cursor,
                limit=prefix_size,
            )
            prefix_response = query_data_v1(self._read_service, read_context, prefix_query)  # type: ignore[arg-type]
            prefix_records, prefix_warnings = self._render_query_records(
                capability,
                read_context,
                prefix_response.anchor_as_of,
                prefix_response.records,
                request.host_context,
            )
            partial_warning = "response_truncated_to_protocol_limit"
            prefix_data = self._query_data(
                capability,
                prefix_response,
                prefix_records,
                prefix_response.next_cursor,
                request.host_context,
                binding,
            )
            combined_warnings = [*prefix_warnings, partial_warning]
            candidate = result(
                request.request_id,
                status="partial",
                data=prefix_data,
                warnings=combined_warnings,
            )
            if _wire_size(candidate) <= 256 * 1024:
                return "partial", prefix_data, combined_warnings
        raise ResultTooLarge("one complete record exceeds the protocol response bound")

    def _render_query_records(
        self,
        capability: Capability,
        read_context: ReadContext,
        anchor: datetime,
        records: Iterable[ReadRecord],
        host_context: HostContext,
    ) -> tuple[list[dict[str, object]], list[str]]:
        record_values = tuple(records)
        projections = self._citation_projections(
            read_context, anchor_as_of=anchor, observation_ids=tuple(record.observation_id for record in record_values)
        )
        by_observation: dict[str, list[CitationProjection]] = {}
        for projection in projections:
            by_observation.setdefault(projection.observation_id, []).append(projection)
        rendered: list[dict[str, object]] = []
        warnings: list[str] = []
        for record in record_values:
            citation_refs: list[str] = []
            for projection in by_observation.get(record.observation_id, []):
                if projection.datasource_id != record.datasource_id:
                    continue
                citation_refs.append(
                    self._handles.mint(
                        "citation",
                        principal=host_context.principal,
                        capability_scope_id=host_context.capability_scope_id,
                        payload={
                            "capability_id": capability.capability_id,
                            "anchor_as_of": utc_timestamp(projection.anchor_as_of),
                            "canonical_run_id": projection.canonical_run_id,
                            "observation_id": projection.observation_id,
                            "evidence_id": projection.evidence_id,
                            "locator_hash": projection.locator_hash,
                        },
                    )
                )
            if record.evidence_ids and not citation_refs:
                warnings.append("citation_unavailable")
            item: dict[str, object] = {
                "observation_id": record.observation_id,
                "datasource_id": record.datasource_id,
                "category": record.category,
                "record_type": record.record_type,
                "payload": dict(record.payload),
                "unit": record.unit,
                "definition": record.definition,
                "period_label": record.period_label,
                "source_date": record.source_date.isoformat() if record.source_date else None,
                "retrieved_at": utc_timestamp(record.retrieved_at) if record.retrieved_at else None,
                "retrieval_freshness": record.retrieval_freshness,
                "observation_freshness": record.observation_freshness,
                "degraded": record.degraded,
                "canonical_available": record.canonical_available,
                "evidence_ids": list(record.evidence_ids),
                "citation_refs": citation_refs,
            }
            if capability.numeric_value_field is not None:
                raw = record.payload.get(capability.numeric_value_field)
                if not isinstance(raw, str) or not _decimal_string(raw):
                    raise InternalError("numeric capability payload violates its manifest")
                item["numeric"] = {
                    "value": raw,
                    "unit": record.unit,
                    "definition": record.definition,
                    "as_of": utc_timestamp(anchor),
                    "source_date": record.source_date.isoformat() if record.source_date else None,
                    "period_label": record.period_label,
                }
            rendered.append(item)
        return rendered, list(dict.fromkeys(warnings))

    def _query_data(
        self,
        capability: Capability,
        response: ReadResponse,
        records: list[dict[str, object]],
        internal_next_cursor: str | None,
        host_context: HostContext,
        binding: Mapping[str, object],
    ) -> dict[str, object]:
        cursor_ref = None
        if internal_next_cursor is not None and records:
            last = records[-1]
            cursor_ref = self._handles.mint(
                "cursor",
                principal=host_context.principal,
                capability_scope_id=host_context.capability_scope_id,
                binding=binding,
                payload={
                    "capability_id": capability.capability_id,
                    "anchor_as_of": utc_timestamp(response.anchor_as_of),
                    "read_cursor": internal_next_cursor,
                    "last_available_at": _last_available_at(response.records),
                    "last_observation_id": last["observation_id"],
                },
            )
        return {
            "anchor_as_of": utc_timestamp(response.anchor_as_of),
            "query_kind": response.query_kind,
            "records": records,
            "total_count": response.total_count,
            "canonical": response.canonical,
            "access_class": str(response.access_class) if response.access_class is not None else None,
            "cursor_ref": cursor_ref,
        }

    def _get_citation_metadata(
        self, request: AgentToolRequest
    ) -> tuple[str, dict[str, object], list[str]]:
        arguments = _arguments(request.arguments, allowed={"citation_refs"}, required={"citation_refs"})
        raw_refs = arguments["citation_refs"]
        if not isinstance(raw_refs, list) or not 1 <= len(raw_refs) <= MAX_MODEL_LIMIT:
            raise InvalidArgument("citation_refs must contain 1 to 20 handles")
        if any(not isinstance(item, str) or not item for item in raw_refs):
            raise InvalidArgument("citation_refs must contain opaque strings")
        read_context = _read_context(request.host_context)
        citations: list[dict[str, object]] = []
        warnings: list[str] = []
        for citation_ref in raw_refs:
            payload = self._handles.verify(
                citation_ref,
                "citation",
                principal=request.host_context.principal,
                capability_scope_id=request.host_context.capability_scope_id,
            )
            capability_id = _text(payload.get("capability_id"), "capability_id")
            if capability_id not in request.host_context.allowed_capability_ids:
                raise AccessDenied("citation capability is not granted")
            anchor = _as_of(payload.get("anchor_as_of"))
            observation_id = _text(payload.get("observation_id"), "observation_id")
            if anchor is None:
                raise InvalidCursor("citation anchor is missing")
            matches = self._citation_projections(
                read_context, anchor_as_of=anchor, observation_ids=(observation_id,)
            )
            expected = (
                payload.get("canonical_run_id"),
                observation_id,
                payload.get("evidence_id"),
                payload.get("locator_hash"),
            )
            projection = next(
                (
                    item
                    for item in matches
                    if (
                        item.canonical_run_id,
                        item.observation_id,
                        item.evidence_id,
                        item.locator_hash,
                    )
                    == expected
                ),
                None,
            )
            if projection is None:
                warnings.append("citation_unavailable")
                continue
            citation, citation_warnings = _citation_object(citation_ref, projection)
            citations.append(citation)
            warnings.extend(citation_warnings)
        status = "partial" if warnings else "ok"
        return status, {"citations": citations}, list(dict.fromkeys(warnings))

    def _request_data_refresh(
        self, request: AgentToolRequest
    ) -> tuple[str, dict[str, object], list[str]]:
        arguments = _arguments(
            request.arguments,
            allowed={"capability_id", "datasource_id", "request_profile", "bounded_scope", "intent"},
            required={"capability_id", "datasource_id", "request_profile", "bounded_scope", "intent"},
        )
        host_context = request.host_context
        capability = self._capability_for(host_context, arguments["capability_id"])
        profile_id = _text(arguments["request_profile"], "request_profile")
        try:
            profile = self._profiles[profile_id]
        except KeyError as error:
            raise InvalidArgument("refresh profile is not registered") from error
        if profile_id not in host_context.allowed_refresh_profiles:
            raise AccessDenied("refresh profile is not granted")
        if capability.capability_id not in profile.capability_ids or profile_id not in capability.refresh_profiles:
            raise PolicyDenied("profile is not allowed for this capability")
        datasource_id = _text(arguments["datasource_id"], "datasource_id")
        if datasource_id != profile.datasource_id or datasource_id not in capability.datasource_ids:
            raise InvalidArgument("datasource does not match the fixed profile")
        scope = _refresh_scope(arguments["bounded_scope"], profile)
        intent = _text(arguments["intent"], "intent")

        if self._refresh_broker is None:
            raise RetryableUnavailable("refresh broker is not configured")

        fresh_anchor = self._already_fresh(capability, profile, host_context)
        if fresh_anchor is not None:
            return "ok", _refresh_data(
                disposition="already_fresh",
                initial_state="already_fresh",
                submitted_at=self._clock(),
                canonical_anchor=fresh_anchor,
            ), []

        refresh_request_id = host_context.refresh_request_id
        if refresh_request_id is None:
            raise InvalidArgument("refresh_request_id is required from the trusted host")
        acknowledgement = request_refresh_v1(
            self._refresh_broker,
            RefreshContext(
                host_context.principal,
                refresh_request_id,
                frozenset({profile_id}),
            ),
            RefreshRequest(
                datasource_id=datasource_id,
                request_profile=profile_id,
                bounded_scope=scope,
                intent=intent,
            ),
        )
        return self._refresh_acknowledgement(
            capability, profile, host_context, acknowledgement, scope, intent
        )

    def _refresh_acknowledgement(
        self,
        capability: Capability,
        profile: AgentRefreshProfile,
        host_context: HostContext,
        acknowledgement: RefreshAcknowledgement,
        scope: Mapping[str, object],
        intent: str,
    ) -> tuple[str, dict[str, object], list[str]]:
        disposition = str(acknowledgement.disposition)
        if acknowledgement.disposition in {RefreshDisposition.ACCEPTED, RefreshDisposition.DEDUPLICATED}:
            assert acknowledgement.job_id is not None
            job_ref = self._handles.mint(
                "job",
                principal=host_context.principal,
                capability_scope_id=host_context.capability_scope_id,
                payload={
                    "job_id": acknowledgement.job_id,
                    "capability_id": capability.capability_id,
                    "profile_id": profile.profile_id,
                    "poll_after_seconds": int(acknowledgement.poll_after.total_seconds()),
                },
            )
            return "ok", _refresh_data(
                disposition=disposition,
                initial_state=acknowledgement.initial_state,
                submitted_at=acknowledgement.submitted_at,
                job_ref=job_ref,
                poll_after_seconds=int(acknowledgement.poll_after.total_seconds()),
            ), []
        if acknowledgement.disposition is RefreshDisposition.ALREADY_FRESH:
            return "ok", _refresh_data(
                disposition="already_fresh",
                initial_state=acknowledgement.initial_state,
                submitted_at=acknowledgement.submitted_at,
                canonical_anchor=acknowledgement.canonical_anchor,
            ), []
        if acknowledgement.disposition is not RefreshDisposition.CONFIRMATION_REQUIRED:
            raise InternalError("unknown refresh acknowledgement")
        if self._approval_store is None or acknowledgement.confirmation_expires_at is None:
            raise RetryableUnavailable("approval storage is unavailable")
        fingerprint = _refresh_fingerprint(
            datasource_id=profile.datasource_id,
            request_profile=profile.profile_id,
            bounded_scope=scope,
            intent=intent,
        )
        approval = self._approval_store.create_agent_refresh_approval(
            refresh_request_id=_required_refresh_id(host_context),
            principal=host_context.principal,
            capability_scope_id=host_context.capability_scope_id,
            capability_id=capability.capability_id,
            manifest_version=self._manifest.version,
            profile_version=self._profiles.version,
            request_fingerprint=fingerprint,
            datasource_id=profile.datasource_id,
            request_profile=profile.profile_id,
            bounded_scope=scope,
            intent=intent,
            now=_as_utc_now(self._clock),
        )
        return "ok", _refresh_data(
            disposition="approval_required",
            initial_state="approval_required",
            submitted_at=acknowledgement.submitted_at,
            approval_id=approval.approval_id,
            approval_expires_at=approval.expires_at,
        ), []

    def _get_refresh_status(
        self, request: AgentToolRequest
    ) -> tuple[str, dict[str, object], list[str]]:
        arguments = _arguments(request.arguments, allowed={"job_ref"}, required={"job_ref"})
        payload = self._handles.verify(
            _text(arguments["job_ref"], "job_ref"),
            "job",
            principal=request.host_context.principal,
            capability_scope_id=request.host_context.capability_scope_id,
        )
        job_id = _text(payload.get("job_id"), "job_id")
        capability_id = _text(payload.get("capability_id"), "capability_id")
        profile_id = _text(payload.get("profile_id"), "profile_id")
        if capability_id not in request.host_context.allowed_capability_ids:
            raise AccessDenied("job capability is not granted")
        if profile_id not in request.host_context.allowed_refresh_profiles:
            raise AccessDenied("job refresh profile is not granted")
        if self._refresh_broker is None:
            raise RetryableUnavailable("refresh broker is not configured")
        status = get_refresh_status_v1(
            self._refresh_broker,
            RefreshContext(
                request.host_context.principal,
                f"status:{request.request_id}",
                frozenset({profile_id}),
            ),
            job_id,
        )
        if status is None:
            raise AccessDenied("job is not visible to this scope")
        return "ok", _refresh_status_data(status), []

    def _approve_refresh(
        self, request: AgentToolRequest
    ) -> tuple[str, dict[str, object], list[str]]:
        arguments = _arguments(request.arguments, allowed={"approval_id", "decision"}, required={"approval_id", "decision"})
        approval_id = _text(arguments["approval_id"], "approval_id")
        decision = _text(arguments["decision"], "decision")
        if decision not in {"approve", "deny"}:
            raise InvalidArgument("decision must be approve or deny")
        if self._approval_store is None or self._refresh_broker is None:
            raise RetryableUnavailable("approval service is unavailable")
        approval = self._approval_store.lookup_agent_refresh_approval(approval_id, now=_as_utc_now(self._clock))
        if (
            approval.principal != request.host_context.principal
            or approval.capability_scope_id != request.host_context.capability_scope_id
            or approval.manifest_version != self._manifest.version
            or approval.profile_version != self._profiles.version
        ):
            raise AccessDenied("approval is not valid for this host context")
        if approval.capability_id not in request.host_context.allowed_capability_ids:
            raise AccessDenied("approval capability is not granted")
        try:
            profile = self._profiles[approval.snapshot["request_profile"]]
        except (KeyError, TypeError) as error:
            raise PolicyDenied("approval profile is no longer available") from error
        if profile.profile_id not in request.host_context.allowed_refresh_profiles:
            raise AccessDenied("approval refresh profile is not granted")
        fingerprint = approval.request_fingerprint
        decision_result = self._approval_store.decide_agent_refresh_approval(
            approval_id,
            decision=decision,
            principal=approval.principal,
            capability_scope_id=approval.capability_scope_id,
            capability_id=approval.capability_id,
            manifest_version=approval.manifest_version,
            profile_version=approval.profile_version,
            request_fingerprint=fingerprint,
            actor_id=request.host_context.principal,
            now=_as_utc_now(self._clock),
        )
        if decision == "deny":
            return "ok", {
                "approval_id": approval_id,
                "decision": "deny",
                "outcome": decision_result.outcome,
                "disposition": "denied",
            }, []
        recovered = self._approval_store.recover_agent_refresh_approval(
            approval_id,
            principal=approval.principal,
            capability_scope_id=approval.capability_scope_id,
            capability_id=approval.capability_id,
            manifest_version=approval.manifest_version,
            profile_version=approval.profile_version,
            request_fingerprint=fingerprint,
            now=_as_utc_now(self._clock),
        )
        snapshot = recovered.snapshot
        acknowledgement = request_refresh_v1(
            self._refresh_broker,
            RefreshContext(
                approval.principal,
                approval.refresh_request_id,
                frozenset({profile.profile_id}),
            ),
            RefreshRequest(
                datasource_id=_text(snapshot.get("datasource_id"), "datasource_id"),
                request_profile=_text(snapshot.get("request_profile"), "request_profile"),
                bounded_scope=_mapping(snapshot.get("bounded_scope"), "bounded_scope"),
                intent=_text(snapshot.get("intent"), "intent"),
                confirmation_token=recovered.confirmation_token,
            ),
        )
        capability = self._manifest.get(approval.capability_id)
        if capability is None:
            raise PolicyDenied("approval capability is no longer available")
        status, data, warnings = self._refresh_acknowledgement(
            capability,
            profile,
            request.host_context,
            acknowledgement,
            _mapping(snapshot.get("bounded_scope"), "bounded_scope"),
            _text(snapshot.get("intent"), "intent"),
        )
        data["approval_outcome"] = decision_result.outcome
        return status, data, warnings

    def _capability_for(self, context: HostContext, value: object) -> Capability:
        capability_id = _text(value, "capability_id")
        try:
            capability = self._manifest[capability_id]
        except KeyError as error:
            raise InvalidArgument("capability is not registered") from error
        if capability_id not in context.allowed_capability_ids:
            raise AccessDenied("capability is not granted")
        if capability.status == "blocked":
            raise CapabilityBlocked("capability is product-blocked")
        return capability

    def _availability(self, context: HostContext) -> dict[str, dict[str, object]]:
        if self._read_service is None:
            return {}
        try:
            response = query_data_v1(
                self._read_service,
                _read_context(context),
                ReadQuery(query_kind="health", limit=MAX_MODEL_LIMIT),
            )
        except (ReadApiError, OSError, sqlite3.Error):
            return {}
        output: dict[str, dict[str, object]] = {}
        for capability_id in context.allowed_capability_ids:
            capability = self._manifest.get(capability_id)
            if capability is None:
                continue
            matching = [record for record in response.records if record.datasource_id in capability.datasource_ids]
            if matching:
                record = matching[0]
                output[capability_id] = {
                    "canonical_available": record.canonical_available,
                    "retrieval_freshness": record.retrieval_freshness,
                    "observation_freshness": record.observation_freshness,
                    "degraded": record.degraded,
                }
        return output

    def _already_fresh(
        self,
        capability: Capability,
        profile: AgentRefreshProfile,
        host_context: HostContext,
    ) -> datetime | None:
        if profile.freshness_precheck != "canonical_bank_rate" or self._read_service is None:
            return None
        context = _read_context(host_context)
        try:
            health = query_data_v1(
                self._read_service,
                context,
                ReadQuery(
                    query_kind="health",
                    filters={"datasource_id": profile.datasource_id},
                    limit=1,
                ),
            )
            if not health.records:
                return None
            record = health.records[0]
            if not record.canonical_available or record.retrieval_freshness != "fresh":
                return None
            metric = query_data_v1(
                self._read_service,
                context,
                ReadQuery(
                    query_kind="metrics",
                    filters={"datasource_id": profile.datasource_id},
                    as_of=health.anchor_as_of,
                    limit=1,
                ),
            )
            field = capability.numeric_value_field
            if field is None:
                return health.anchor_as_of if metric.records else None
            if any(
                isinstance(record.payload.get(field), str)
                and _decimal_string(record.payload[field])
                for record in metric.records
            ):
                return health.anchor_as_of
            return None
        except ReadApiError:
            return None

    def _citation_projections(
        self,
        context: ReadContext,
        *,
        anchor_as_of: datetime,
        observation_ids: Iterable[str],
    ) -> tuple[CitationProjection, ...]:
        if self._citation_projection is None:
            return ()
        source = self._citation_projection
        if isinstance(source, CitationProjectionRepository):
            values = citation_projection_v1(
                source, context, anchor_as_of=anchor_as_of, observation_ids=observation_ids
            )
        elif hasattr(source, "citation_projection"):
            values = citation_projection_v1(  # type: ignore[arg-type]
                source, context, anchor_as_of=anchor_as_of, observation_ids=observation_ids
            )
        else:
            values = source(context, anchor_as_of=anchor_as_of, observation_ids=observation_ids)  # type: ignore[misc]
        return tuple(item for item in values if isinstance(item, CitationProjection))

    @staticmethod
    def _validate_refresh_identity(tool_name: str, context: HostContext) -> None:
        if tool_name == "request_data_refresh":
            if context.refresh_request_id is None:
                raise InvalidArgument("refresh_request_id is required for refresh")
        elif context.refresh_request_id is not None:
            raise InvalidArgument("refresh_request_id is only valid for refresh")


def _arguments(
    value: Mapping[str, object], *, allowed: set[str], required: set[str]
) -> dict[str, object]:
    if set(value) - allowed or required - set(value):
        raise InvalidArgument("tool arguments do not match the allowed schema")
    return dict(value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise InvalidArgument(f"{name} must be a bounded non-empty string")
    return value


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidArgument(f"{name} must be an object")
    return value


def _limit(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_MODEL_LIMIT:
        raise InvalidArgument("limit must be an integer between 1 and 20")
    return value


def _as_of(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise InvalidArgument("as_of must be RFC3339 UTC")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as error:
        raise InvalidArgument("as_of must be RFC3339 UTC") from error


def _query_filters(value: object, template: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidArgument("filters must be an object")
    fixed_filters = getattr(template, "fixed_filters", None)
    allowed_filters = getattr(template, "allowed_filters", None)
    if not isinstance(fixed_filters, Mapping) or not isinstance(allowed_filters, frozenset):
        raise InternalError("manifest query template is invalid")
    if set(value) - set(allowed_filters):
        raise InvalidArgument("filter is not permitted by the capability template")
    output: dict[str, object] = {
        key: list(items) for key, items in fixed_filters.items()
    }
    for key, item in value.items():
        output[key] = item
    return output


def _query_binding(capability_id: str, query_kind: str, filters: Mapping[str, object]) -> dict[str, object]:
    return {
        "capability_id": capability_id,
        "query_kind": query_kind,
        "filters": _canonical_json_object(filters),
    }


def _read_context(context: HostContext) -> ReadContext:
    try:
        return ReadContext(
            context.principal,
            frozenset(AccessClass(value) for value in context.allowed_access_classes),
        )
    except ValueError as error:
        raise InvalidArgument("host access classes are invalid") from error


def _refresh_scope(value: object, profile: AgentRefreshProfile) -> dict[str, object]:
    scope = _mapping(value, "bounded_scope")
    if set(scope) - set(profile.allowed_scope_keys) or not set(profile.required_scope_keys) <= set(scope):
        raise InvalidArgument("refresh scope does not match its fixed profile")
    output: dict[str, object] = {}
    for key, raw in scope.items():
        if not isinstance(key, str):
            raise InvalidArgument("refresh scope key is invalid")
        values = [raw] if isinstance(raw, str) else raw
        if not isinstance(values, list) or not values or any(not isinstance(item, str) or not item for item in values):
            raise InvalidArgument("refresh scope values must be non-empty strings")
        if len(values) > profile.max_scope_values or (
            key in profile.single_value_scope_keys and len(values) != 1
        ):
            raise InvalidArgument("refresh scope exceeds profile bounds")
        if key.lower() in {"url", "host", "endpoint", "path"} or any(
            item.startswith(("http://", "https://")) for item in values
        ):
            raise InvalidArgument("refresh scope cannot contain a network location")
        if profile.profile_id == "onspd-one-postcode" and key == "postcode":
            output[key] = [_normalise_postcode(values[0])]
        else:
            output[key] = list(values)
    return output


def _normalise_postcode(value: str) -> str:
    compact = "".join(value.upper().split())
    if not _POSTCODE.fullmatch(compact):
        raise InvalidArgument("postcode is not a supported normalized UK postcode")
    return f"{compact[:-3]} {compact[-3:]}"


def _refresh_fingerprint(
    *, datasource_id: str, request_profile: str, bounded_scope: Mapping[str, object], intent: str
) -> str:
    from hashlib import sha256

    payload = {
        "datasource_id": datasource_id,
        "request_profile": request_profile,
        "bounded_scope": {
            key: list(value) if isinstance(value, (tuple, list)) else [value]
            for key, value in sorted(bounded_scope.items())
        },
        "intent": intent,
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _required_refresh_id(context: HostContext) -> str:
    if context.refresh_request_id is None:
        raise InvalidArgument("trusted refresh identity is missing")
    return context.refresh_request_id


def _refresh_data(
    *,
    disposition: str,
    initial_state: str,
    submitted_at: datetime,
    job_ref: str | None = None,
    approval_id: str | None = None,
    approval_expires_at: datetime | None = None,
    canonical_anchor: datetime | None = None,
    poll_after_seconds: int | None = None,
) -> dict[str, object]:
    return {
        "disposition": disposition,
        "job_ref": job_ref,
        "approval_id": approval_id,
        "approval_expires_at": utc_timestamp(approval_expires_at) if approval_expires_at else None,
        "canonical_anchor": utc_timestamp(canonical_anchor) if canonical_anchor else None,
        "poll_after_seconds": poll_after_seconds,
        "initial_state": initial_state,
        "submitted_at": utc_timestamp(_as_utc(submitted_at)),
    }


def _unknown_availability() -> dict[str, object]:
    return {
        "canonical_available": None,
        "retrieval_freshness": "unknown",
        "observation_freshness": "unknown",
        "degraded": None,
    }


def _refresh_status_data(status: RefreshStatus) -> dict[str, object]:
    terminal_states = {"succeeded", "empty", "failed", "dead_letter", "cancelled"}
    terminal_error: dict[str, object] | None = None
    if status.terminal_error is not None:
        code = status.terminal_error.get("code")
        terminal_error = {
            "code": code if isinstance(code, str) else "INTERNAL_ERROR",
            "retryable": bool(status.terminal_error.get("retryable")),
        }
    return {
        "job_state": status.job_state,
        "latest_attempt_status": status.latest_attempt_status,
        "retry_after_seconds": (
            max(0, int(status.retry_after.total_seconds())) if status.retry_after is not None else None
        ),
        "terminal_error": terminal_error,
        "promotion_status": status.promotion_status,
        "canonical_changed": (
            bool(status.canonical_changed) if status.job_state in terminal_states else None
        ),
    }


def _citation_object(
    citation_ref: str, projection: CitationProjection
) -> tuple[dict[str, object], list[str]]:
    warnings = list(projection.warnings)
    nullable = {
        "title": projection.title,
        "public_url": projection.public_url,
        "published_at": utc_timestamp(projection.published_at) if projection.published_at else None,
        "source_updated_at": (
            utc_timestamp(projection.source_updated_at) if projection.source_updated_at else None
        ),
        "licence_or_attribution": projection.licence_or_attribution,
    }
    for field, value in nullable.items():
        if value is None:
            warnings.append(f"{field}_unavailable")
    return (
        {
            "citation_ref": citation_ref,
            "observation_id": projection.observation_id,
            "evidence_id": projection.evidence_id,
            "datasource_id": projection.datasource_id,
            "publisher": projection.publisher,
            "retrieved_at": utc_timestamp(projection.retrieved_at),
            "access_class": str(projection.access_class),
            "data_kind": projection.data_kind,
            "confidence": projection.confidence,
            "limitations": list(projection.limitations),
            "locator": dict(projection.locator),
            **nullable,
        },
        list(dict.fromkeys(warnings)),
    )


def _decimal_string(value: str) -> bool:
    try:
        decimal = Decimal(value)
    except (InvalidOperation, ValueError):
        return False
    return decimal.is_finite()


def _last_available_at(records: Iterable[ReadRecord]) -> str | None:
    values = tuple(records)
    return utc_timestamp(values[-1].available_at) if values else None


def _wire_size(value: Mapping[str, object]) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )


def _canonical_json_object(value: Mapping[str, object]) -> dict[str, object]:
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidArgument("arguments must be JSON-compatible") from error
    if not isinstance(decoded, dict):
        raise InvalidArgument("arguments must be an object")
    return decoded


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidArgument("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _as_utc_now(clock: Callable[[], datetime]) -> datetime:
    return _as_utc(clock())


__all__ = ["AgentToolFacade", "HOST_TOOL_NAMES", "MODEL_TOOL_NAMES"]
