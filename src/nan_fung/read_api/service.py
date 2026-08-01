"""In-process canonical read service with authenticated keyset cursors."""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import hmac
import json
from typing import Any

from .access import ReadContext, most_restrictive_access
from .contracts import (
    READ_SCHEMA_VERSION,
    AccessDenied,
    InvalidCursor,
    ReadApiError,
    ReadPage,
    ReadQuery,
    ReadRecord,
    ReadRepository,
    ReadResponse,
    normalise_utc,
    utc_timestamp,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return sha256(_json_bytes(value)).hexdigest()


def _encode_part(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_part(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeEncodeError) as error:
        raise InvalidCursor("cursor is not base64url") from error


class ReadService:
    """Apply policy and pagination above an injected read-only repository."""

    def __init__(
        self,
        repository: ReadRepository,
        *,
        cursor_secret: bytes,
        clock: Callable[[], datetime] = _utc_now,
        cursor_max_age: timedelta = timedelta(minutes=15),
    ) -> None:
        if not cursor_secret:
            raise ValueError("cursor_secret must not be empty")
        if cursor_max_age <= timedelta(0):
            raise ValueError("cursor_max_age must be positive")
        self._repository = repository
        self._cursor_secret = bytes(cursor_secret)
        self._clock = clock
        self._cursor_max_age = cursor_max_age

    def query(self, context: ReadContext, query: ReadQuery) -> ReadResponse:
        query_fingerprint = self._query_fingerprint(query)
        cursor = self._decode_cursor(query.cursor) if query.cursor else None
        anchor_as_of = self._anchor_as_of(query, cursor)
        if cursor is not None:
            self._validate_cursor(cursor, context, query_fingerprint, anchor_as_of)

        page_loader = getattr(self._repository, "query_page", None)
        if query.query_kind != "health" and callable(page_loader):
            return self._query_storage_page(
                page_loader,
                context=context,
                query=query,
                query_fingerprint=query_fingerprint,
                anchor_as_of=anchor_as_of,
                cursor=cursor,
            )

        records = self._load_records(context, query, anchor_as_of)
        records = tuple(record for record in records if self._matches(record, query))
        records = tuple(sorted(records, key=self._sort_key, reverse=True))
        total_count = len(records)
        if cursor is not None:
            last_key = (cursor["last_available_at"], cursor["last_observation_id"])
            records = tuple(
                record
                for record in records
                if self._serialised_sort_key(record) < last_key
            )

        page = records[: query.limit]
        has_more = len(records) > len(page)
        next_cursor = None
        if has_more and page:
            next_cursor = self._make_cursor(
                context=context,
                query_fingerprint=query_fingerprint,
                anchor_as_of=anchor_as_of,
                last_record=page[-1],
            )
        return ReadResponse(
            schema_version=READ_SCHEMA_VERSION,
            query_kind=query.query_kind,
            anchor_as_of=anchor_as_of,
            records=page,
            next_cursor=next_cursor,
            total_count=total_count,
            canonical=query.result_ref is None,
            access_class=most_restrictive_access(
                record.access_class for record in page
            ),
        )

    def _query_storage_page(
        self,
        page_loader: Callable[..., ReadPage],
        *,
        context: ReadContext,
        query: ReadQuery,
        query_fingerprint: str,
        anchor_as_of: datetime,
        cursor: dict[str, Any] | None,
    ) -> ReadResponse:
        if query.result_ref is not None and query.result_ref not in context.allowed_result_refs:
            raise AccessDenied("result reference is not granted to this context")
        after = None
        if cursor is not None:
            after = (cursor["last_available_at"], cursor["last_observation_id"])
        page = page_loader(
            query,
            as_of=anchor_as_of,
            context=context,
            after=after,
        )
        if not isinstance(page, ReadPage):
            raise ReadApiError("paged repository returned an invalid page")
        records = page.records
        if len(records) > query.limit + 1:
            raise ReadApiError("paged repository exceeded the requested page bound")
        if page.total_count < len(records):
            raise ReadApiError("paged repository returned an invalid total count")
        if query.result_ref is None:
            if any(
                not record.canonical
                or record.lane != "production_ingestion"
                or record.available_at > anchor_as_of
                or not context.allows(record.access_class)
                or not self._matches(record, query)
                for record in records
            ):
                raise ReadApiError("paged repository returned an ineligible canonical row")
        elif any(
            record.canonical
            or record.available_at > anchor_as_of
            or not context.allows(record.access_class)
            or not self._matches(record, query)
            for record in records
        ):
            raise ReadApiError("paged repository returned an ineligible result row")
        if any(
            self._serialised_sort_key(left) <= self._serialised_sort_key(right)
            for left, right in zip(records, records[1:])
        ):
            raise ReadApiError("paged repository returned rows out of keyset order")
        if after is not None and any(
            self._serialised_sort_key(record) >= after for record in records
        ):
            raise ReadApiError("paged repository ignored the cursor boundary")

        returned = records[: query.limit]
        next_cursor = None
        if len(records) > len(returned) and returned:
            next_cursor = self._make_cursor(
                context=context,
                query_fingerprint=query_fingerprint,
                anchor_as_of=anchor_as_of,
                last_record=returned[-1],
            )
        return ReadResponse(
            schema_version=READ_SCHEMA_VERSION,
            query_kind=query.query_kind,
            anchor_as_of=anchor_as_of,
            records=returned,
            next_cursor=next_cursor,
            total_count=page.total_count,
            canonical=query.result_ref is None,
            access_class=most_restrictive_access(
                record.access_class for record in returned
            ),
        )

    def _load_records(
        self, context: ReadContext, query: ReadQuery, anchor_as_of: datetime
    ) -> tuple[ReadRecord, ...]:
        if query.result_ref is None:
            candidates = self._repository.query_canonical(
                query, as_of=anchor_as_of, context=context
            )
            return tuple(
                record
                for record in candidates
                if record.canonical
                and record.lane == "production_ingestion"
                and record.available_at <= anchor_as_of
                and context.allows(record.access_class)
            )

        if query.result_ref not in context.allowed_result_refs:
            raise AccessDenied("result reference is not granted to this context")
        try:
            candidates = self._repository.query_result(
                query.result_ref, query, as_of=anchor_as_of, context=context
            )
        except AttributeError as error:
            raise ReadApiError("repository does not support run-scoped results") from error
        records = tuple(candidates)
        if any(record.canonical for record in records):
            raise ReadApiError("run-scoped result references must not return canonical rows")
        return tuple(
            record
            for record in records
            if record.available_at <= anchor_as_of and context.allows(record.access_class)
        )

    def _anchor_as_of(
        self, query: ReadQuery, cursor: dict[str, Any] | None
    ) -> datetime:
        if cursor is None:
            return query.as_of if query.as_of is not None else normalise_utc(self._clock())
        try:
            anchor = datetime.fromisoformat(cursor["anchor_as_of"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidCursor("cursor anchor is invalid") from error
        anchor = normalise_utc(anchor)
        if query.as_of is not None and query.as_of != anchor:
            raise InvalidCursor("cursor cannot be reused with a different as_of")
        return anchor

    def _query_fingerprint(self, query: ReadQuery) -> str:
        filters = {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in query.filters.items()
        }
        return _fingerprint(
            {
                "schema_version": READ_SCHEMA_VERSION,
                "query_kind": query.query_kind,
                "filters": filters,
                "result_ref": query.result_ref,
                "sort": ["available_at:desc", "observation_id:desc"],
            }
        )

    def _make_cursor(
        self,
        *,
        context: ReadContext,
        query_fingerprint: str,
        anchor_as_of: datetime,
        last_record: ReadRecord,
    ) -> str:
        issued_at = normalise_utc(self._clock())
        payload = {
            "schema_version": READ_SCHEMA_VERSION,
            "query_fingerprint": query_fingerprint,
            "policy_fingerprint": context.policy_fingerprint(),
            "anchor_as_of": utc_timestamp(anchor_as_of),
            "last_available_at": utc_timestamp(last_record.available_at),
            "last_observation_id": last_record.observation_id,
            "issued_at": utc_timestamp(issued_at),
        }
        encoded = _encode_part(_json_bytes(payload))
        signature = hmac.new(
            self._cursor_secret, encoded.encode("ascii"), "sha256"
        ).digest()
        return f"{encoded}.{_encode_part(signature)}"

    def _decode_cursor(self, cursor: str) -> dict[str, Any]:
        try:
            encoded, supplied_signature = cursor.split(".")
        except ValueError as error:
            raise InvalidCursor("cursor has an invalid envelope") from error
        expected_signature = hmac.new(
            self._cursor_secret, encoded.encode("ascii"), "sha256"
        ).digest()
        if not hmac.compare_digest(
            _encode_part(expected_signature), supplied_signature
        ):
            raise InvalidCursor("cursor signature is invalid")
        try:
            decoded = json.loads(_decode_part(encoded).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidCursor("cursor payload is invalid") from error
        if not isinstance(decoded, dict):
            raise InvalidCursor("cursor payload must be an object")
        return decoded

    def _validate_cursor(
        self,
        cursor: dict[str, Any],
        context: ReadContext,
        query_fingerprint: str,
        anchor_as_of: datetime,
    ) -> None:
        if cursor.get("schema_version") != READ_SCHEMA_VERSION:
            raise InvalidCursor("cursor schema is unsupported")
        if cursor.get("query_fingerprint") != query_fingerprint:
            raise InvalidCursor("cursor does not match this query")
        if cursor.get("policy_fingerprint") != context.policy_fingerprint():
            raise InvalidCursor("cursor does not match this context")
        if cursor.get("anchor_as_of") != utc_timestamp(anchor_as_of):
            raise InvalidCursor("cursor anchor is invalid")
        if not isinstance(cursor.get("last_observation_id"), str):
            raise InvalidCursor("cursor sort key is invalid")
        try:
            last_available_at = datetime.fromisoformat(
                cursor["last_available_at"].replace("Z", "+00:00")
            )
            issued_at = datetime.fromisoformat(cursor["issued_at"].replace("Z", "+00:00"))
        except (KeyError, AttributeError, ValueError) as error:
            raise InvalidCursor("cursor timestamps are invalid") from error
        normalise_utc(last_available_at)
        if normalise_utc(self._clock()) - normalise_utc(issued_at) > self._cursor_max_age:
            raise InvalidCursor("cursor has expired")

    @staticmethod
    def _sort_key(record: ReadRecord) -> tuple[datetime, str]:
        return record.available_at, record.observation_id

    @staticmethod
    def _serialised_sort_key(record: ReadRecord) -> tuple[str, str]:
        return utc_timestamp(record.available_at), record.observation_id

    @staticmethod
    def _matches(record: ReadRecord, query: ReadQuery) -> bool:
        if record.query_kind != query.query_kind:
            return False
        for key, wanted in query.filters.items():
            wanted_values = wanted if isinstance(wanted, tuple) else (wanted,)
            if key == "datasource_id":
                actual_values = (record.datasource_id,)
            elif key == "category":
                actual_values = (record.category,)
            elif key == "record_type":
                actual_values = (record.record_type,)
            elif key == "observation_id":
                actual_values = (record.observation_id,)
            elif key == "evidence_id":
                actual_values = record.evidence_ids
            elif key == "source_date_from":
                if record.source_date is None or record.source_date.isoformat() < wanted_values[0]:
                    return False
                continue
            elif key == "source_date_to":
                if record.source_date is None or record.source_date.isoformat() > wanted_values[0]:
                    return False
                continue
            else:
                payload_value = record.payload.get(key)
                if payload_value is None:
                    return False
                actual_values = (str(payload_value),)
            if not any(value in actual_values for value in wanted_values):
                return False
        return True


def query_data_v1(
    service: ReadService,
    context: ReadContext,
    query: ReadQuery,
) -> ReadResponse:
    """Stable function entry point used by CLI, dashboard, and agent adapters."""

    return service.query(context, query)
