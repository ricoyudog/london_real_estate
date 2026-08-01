"""Bank Rate's minimal capture-before-parse vertical slice.

The module owns no SQLite or filesystem implementation.  It accepts an
ingestion persistence protocol so the same workflow is usable with a test
memory store or a storage-backed repository without importing either one.
"""

from __future__ import annotations

import csv
import _strptime  # Preload datetime parsing before the parser child denies imports.
import io
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urlencode

from .canonical import (
    content_sha256,
    format_date,
    format_timestamp,
    freeze_json,
    new_id,
    observation_hash,
    thaw_json,
)
from .policies import (
    ArtifactPolicy,
    PolicyError,
    SourcePolicy,
    redact_headers,
    redact_url,
    validate_source_url,
)
from .registry import DatasourceRegistry, RuntimeBindings, default_registry, default_runtime_bindings
from nan_fung.storage.artifacts import StoredArtifact


BANK_RATE_DATASOURCE_ID = "boe.bank_rate.iudbedr"
BOE_BANK_RATE_URL = (
    "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp"
)
BANK_RATE_PARAMS = {
    "csv.x": "yes",
    "Datefrom": "01/Jan/2025",
    "Dateto": "now",
    "SeriesCodes": "IUDBEDR",
    "CSVF": "TN",
    "UsingCodes": "Y",
    "VPD": "Y",
    "VFD": "N",
}
BANK_RATE_SOURCE_POLICY = SourcePolicy(
    allowed_hosts=("www.bankofengland.co.uk",),
    allowed_methods=("GET",),
    allowed_query_keys=tuple(BANK_RATE_PARAMS),
    artifact=ArtifactPolicy(
        max_bytes=4 * 1024 * 1024,
        allowed_media_types=(
            "text/csv",
            "application/csv",
            "application/octet-stream",
        ),
    ),
)


class BankRateError(ValueError):
    """Base error for Bank Rate acquisition, parse, or lifecycle failures."""


class BankRateParseError(BankRateError):
    """Raised when a persisted Bank Rate CSV is malformed."""


@dataclass(frozen=True, slots=True)
class AcquiredArtifact:
    """Typed successful acquisition output before it is persisted as evidence."""

    body: bytes
    source_url: str
    retrieved_at: datetime
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    media_type: str = "text/csv"

    def __post_init__(self) -> None:
        if not isinstance(self.body, bytes):
            raise BankRateError("acquired body must be bytes")
        if self.status != 200 or _has_content_range(self.headers):
            raise BankRateError("Bank Rate lifecycle requires a complete HTTP 200 response")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise BankRateError("retrieved_at must be timezone-aware")
        object.__setattr__(self, "retrieved_at", self.retrieved_at.astimezone(UTC))
        try:
            # A fixture has no network transport to re-check, but it must still
            # carry the immutable Bank Rate source identity.  Live acquisition
            # performs the public-address check before it constructs this type.
            validate_source_url(
                self.source_url,
                BANK_RATE_SOURCE_POLICY,
                resolver=None,
            )
        except PolicyError as error:
            raise BankRateError("Bank Rate artifact has unapproved provenance") from error
        object.__setattr__(self, "source_url", redact_url(self.source_url))
        object.__setattr__(self, "headers", freeze_json(redact_headers(self.headers)))

    @property
    def content_sha256(self) -> str:
        return content_sha256(self.body)


@dataclass(frozen=True, slots=True)
class StoredAcquiredArtifact:
    """Live Bank Rate metadata for a body already verified in the CAS."""

    artifact: StoredArtifact
    request_url: str
    source_url: str
    retrieved_at: datetime
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    media_type: str = "text/csv"

    def __post_init__(self) -> None:
        if self.status != 200 or _has_content_range(self.headers):
            raise BankRateError("Bank Rate lifecycle requires a complete HTTP 200 response")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise BankRateError("retrieved_at must be timezone-aware")
        try:
            validate_source_url(
                self.request_url,
                BANK_RATE_SOURCE_POLICY,
                resolver=None,
            )
            validate_source_url(
                self.source_url,
                BANK_RATE_SOURCE_POLICY,
                resolver=None,
            )
        except PolicyError as error:
            raise BankRateError("Bank Rate artifact has unapproved provenance") from error
        object.__setattr__(self, "retrieved_at", self.retrieved_at.astimezone(UTC))
        object.__setattr__(self, "request_url", redact_url(self.request_url))
        object.__setattr__(self, "source_url", redact_url(self.source_url))
        object.__setattr__(self, "headers", freeze_json(redact_headers(self.headers)))

    @property
    def content_sha256(self) -> str:
        return self.artifact.content_sha256


BankRateArtifact = AcquiredArtifact | StoredAcquiredArtifact


def _has_content_range(headers: Mapping[str, str]) -> bool:
    return any(name.lower() == "content-range" for name in headers)


@dataclass(frozen=True, slots=True)
class BankRateRecord:
    """A normalized decimal-string record parsed from one persisted CSV row."""

    effective_date: str
    rate_percent: str
    row_number: int
    series: str = "IUDBEDR"

    def __post_init__(self) -> None:
        try:
            date.fromisoformat(self.effective_date)
        except (TypeError, ValueError) as error:
            raise BankRateParseError("effective_date must be ISO calendar date") from error
        if self.series != "IUDBEDR":
            raise BankRateParseError("Bank Rate series must be IUDBEDR")
        if self.row_number < 2:
            raise BankRateParseError("CSV record row must be at least two")
        normalized = _decimal_text(self.rate_percent)
        object.__setattr__(self, "rate_percent", normalized)

    @property
    def record_key(self) -> tuple[str, str]:
        return (self.series, self.effective_date)

    @property
    def payload(self) -> dict[str, str]:
        return {
            "series": self.series,
            "date": self.effective_date,
            "bank_rate_percent": self.rate_percent,
        }

    @property
    def locator(self) -> dict[str, Any]:
        return {
            "kind": "csv_row",
            "row": self.row_number,
            "columns": {"date": "DATE", "value": "IUDBEDR"},
        }

    @property
    def record_hash(self) -> str:
        return observation_hash(
            datasource_id=BANK_RATE_DATASOURCE_ID,
            record_type="bank_rate",
            schema_version="v1",
            revision_action="upsert",
            record_key=self.record_key,
            payload=self.payload,
            source_date=self.effective_date,
            unit="percent",
            data_kind="direct",
            confidence="high",
            definition="Official Bank of England Bank Rate series IUDBEDR",
            limitations=("Current-vintage official series",),
        )


def _decimal_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BankRateParseError("Bank Rate value is required")
    try:
        decimal = Decimal(value.strip())
    except InvalidOperation as error:
        raise BankRateParseError(f"invalid Bank Rate decimal: {value!r}") from error
    if not decimal.is_finite():
        raise BankRateParseError("Bank Rate decimal must be finite")
    if decimal.is_zero():
        return "0"
    normalized = format(decimal.normalize(), "f")
    if "e" in normalized.lower() or normalized == "-0":
        raise BankRateParseError("Bank Rate decimal cannot use exponent or -0")
    return normalized


def _parse_effective_date(value: str) -> str:
    value = value.strip()
    for pattern in ("%d %b %Y", "%Y-%m-%d"):
        try:
            return format_date(datetime.strptime(value, pattern).date())
        except ValueError:
            pass
    raise BankRateParseError(f"invalid Bank Rate date: {value!r}")


def parse_bank_rate_csv(payload: bytes) -> tuple[BankRateRecord, ...]:
    """Parse Bank Rate CSV bytes that have already been persisted as evidence."""

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise BankRateParseError("Bank Rate CSV must be UTF-8") from error
    reader = csv.DictReader(io.StringIO(text))
    fields = {field.strip() for field in reader.fieldnames or ()}
    if {"DATE", "IUDBEDR"} - fields:
        raise BankRateParseError("Bank Rate CSV must contain DATE and IUDBEDR columns")
    records: list[BankRateRecord] = []
    seen_keys: set[tuple[str, str]] = set()
    for row_number, row in enumerate(reader, start=2):
        raw_date = (row.get("DATE") or "").strip()
        raw_rate = (row.get("IUDBEDR") or "").strip()
        if not raw_date and not raw_rate:
            continue
        if not raw_date or not raw_rate:
            raise BankRateParseError(f"incomplete Bank Rate CSV row {row_number}")
        record = BankRateRecord(
            effective_date=_parse_effective_date(raw_date),
            rate_percent=raw_rate,
            row_number=row_number,
        )
        if record.record_key in seen_keys:
            raise BankRateParseError(f"duplicate Bank Rate date: {record.effective_date}")
        seen_keys.add(record.record_key)
        records.append(record)
    return tuple(records)


def bank_rate_record_key(record: BankRateRecord | Mapping[str, Any]) -> tuple[str, str]:
    """Runtime key binding for a normalized Bank Rate record."""

    if isinstance(record, BankRateRecord):
        return record.record_key
    try:
        series = str(record["series"])
        effective_date = str(record.get("date", record.get("effective_date")))
    except (AttributeError, KeyError) as error:
        raise BankRateParseError("record does not have Bank Rate key fields") from error
    return (series, _parse_effective_date(effective_date))


def validate_bank_rate_record(record: BankRateRecord | Mapping[str, Any]) -> None:
    """Runtime validator binding for a sensible official Bank Rate record."""

    if isinstance(record, Mapping):
        series, effective_date = bank_rate_record_key(record)
        rate = str(record.get("bank_rate_percent", record.get("rate_percent", "")))
        candidate = BankRateRecord(effective_date, rate, row_number=2, series=series)
    else:
        candidate = record
    rate = Decimal(candidate.rate_percent)
    if rate < Decimal("0") or rate > Decimal("20"):
        raise BankRateParseError("Bank Rate must be between 0 and 20 percent")


def collect_bank_rate(
    acquire: Callable[..., AcquiredArtifact],
    *,
    date_from: str = "01/Jan/2025",
    date_to: str = "now",
) -> AcquiredArtifact:
    """Invoke an injected acquisition boundary, never direct network I/O."""

    params = {**BANK_RATE_PARAMS, "Datefrom": date_from, "Dateto": date_to}
    artifact = acquire(BOE_BANK_RATE_URL, params=params, method="GET")
    if not isinstance(artifact, AcquiredArtifact):
        raise BankRateError("acquisition boundary must return AcquiredArtifact")
    return artifact


@dataclass(frozen=True, slots=True)
class BankRateRun:
    """Ingestion-owned lifecycle metadata passed to a persistence adapter."""

    run_id: str
    datasource_id: str
    definition_version: int
    definition_hash: str
    lane: str
    requested_at: datetime
    request: Any
    request_hash: str

    def __post_init__(self) -> None:
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
            raise BankRateError("run requested_at must be timezone-aware")
        object.__setattr__(self, "requested_at", self.requested_at.astimezone(UTC))
        object.__setattr__(self, "request", freeze_json(self.request))


@dataclass(frozen=True, slots=True)
class PersistedEvidence:
    """Opaque evidence handle plus metadata necessary to re-open saved bytes."""

    evidence_id: str
    content_sha256: str


class BankRatePersistence(Protocol):
    """Minimal ingestion repository contract; implementation is deliberately local."""

    def create_run(self, run: BankRateRun) -> str:
        """Persist a running run and return its identity."""

    def persist_evidence(self, run_id: str, artifact: BankRateArtifact) -> PersistedEvidence:
        """Safely persist raw bytes before any parse occurs."""

    def read_evidence(self, evidence: PersistedEvidence) -> bytes:
        """Read only the saved evidence object used as parser input."""

    def persist_observation(
        self,
        run_id: str,
        evidence: PersistedEvidence,
        record: BankRateRecord,
        *,
        lane: str,
    ) -> str:
        """Persist an immutable normalized revision and return its ID."""

    def promote(self, run_id: str, observation_ids: Sequence[str], *, lane: str) -> bool:
        """Promote eligible production observations, returning canonical change."""

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        error: Mapping[str, str] | None = None,
    ) -> None:
        """Terminally persist run status without overwriting prior observations."""


@dataclass(frozen=True, slots=True)
class BankRateLifecycleResult:
    run_id: str
    evidence_id: str
    observation_ids: tuple[str, ...]
    status: str
    canonical_changed: bool


class BankRateLifecycle:
    """Registry -> persisted evidence -> parser -> observations -> promotion."""

    def __init__(
        self,
        persistence: BankRatePersistence,
        *,
        registry: DatasourceRegistry | None = None,
        bindings: RuntimeBindings | None = None,
        record_parser: Callable[[bytes], Sequence[BankRateRecord]] = parse_bank_rate_csv,
    ) -> None:
        self._persistence = persistence
        self._registry = registry or default_registry()
        self._bindings = bindings or default_runtime_bindings()
        self._record_parser = record_parser

    def ingest(
        self,
        artifact: BankRateArtifact,
        *,
        lane: str = "production_ingestion",
        requested_at: datetime | None = None,
        definition_version: int | None = None,
    ) -> BankRateLifecycleResult:
        """Ingest a pre-acquired artifact while enforcing capture-before-parse."""

        if lane not in {"production_ingestion", "source_discovery", "ad_hoc_research"}:
            raise BankRateError(f"unsupported lane: {lane!r}")
        definition = self._registry.lookup(BANK_RATE_DATASOURCE_ID, definition_version)
        self._bindings.validate(definition, operation="ingest").require()
        now = (requested_at or artifact.retrieved_at).astimezone(UTC)
        request = {
            "method": "GET",
            "url": artifact.source_url,
            "series": "IUDBEDR",
            "source_url": artifact.source_url,
        }
        from .canonical import request_hash  # Avoid widening the top-level import surface.

        run = BankRateRun(
            run_id=new_id("run"),
            datasource_id=definition.datasource_id,
            definition_version=definition.definition_version,
            definition_hash=definition.definition_hash,
            lane=lane,
            requested_at=now,
            request=request,
            request_hash=request_hash(request),
        )
        run_id = self._persistence.create_run(run)
        try:
            evidence = self._persistence.persist_evidence(run_id, artifact)
            saved_bytes = self._persistence.read_evidence(evidence)
            if content_sha256(saved_bytes) != evidence.content_sha256:
                raise BankRateError("saved evidence hash does not match evidence handle")
            records = tuple(self._record_parser(saved_bytes))
            observation_ids: list[str] = []
            for record in records:
                validate_bank_rate_record(record)
                observation_ids.append(
                    self._persistence.persist_observation(
                        run_id, evidence, record, lane=lane
                    )
                )
            if not records:
                self._persistence.finish_run(run_id, status="empty")
                return BankRateLifecycleResult(
                    run_id, evidence.evidence_id, (), "empty", False
                )
            canonical_changed = False
            if lane == "production_ingestion":
                canonical_changed = self._persistence.promote(
                    run_id, observation_ids, lane=lane
                )
            self._persistence.finish_run(run_id, status="succeeded")
            return BankRateLifecycleResult(
                run_id,
                evidence.evidence_id,
                tuple(observation_ids),
                "succeeded",
                canonical_changed,
            )
        except Exception as error:
            self._persistence.finish_run(
                run_id,
                status="failed",
                error={"code": type(error).__name__},
            )
            raise


def parse_bank_rate_csv_isolated(payload: bytes) -> tuple[BankRateRecord, ...]:
    """Run the pure CSV parser in the bounded artifact-parser child protocol."""

    from .parser_runner import run_bounded_parser

    parsed = run_bounded_parser(parse_bank_rate_csv, payload)
    if not isinstance(parsed, list):
        raise BankRateParseError("isolated Bank Rate parser returned invalid records")
    records: list[BankRateRecord] = []
    for item in parsed:
        if not isinstance(item, Mapping):
            raise BankRateParseError("isolated Bank Rate parser returned invalid record")
        try:
            records.append(
                BankRateRecord(
                    effective_date=str(item["effective_date"]),
                    rate_percent=str(item["rate_percent"]),
                    row_number=int(item["row_number"]),
                    series=str(item.get("series", "IUDBEDR")),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise BankRateParseError("isolated Bank Rate parser returned invalid record") from error
    return tuple(records)


class InMemoryBankRatePersistence:
    """Small reference adapter proving lifecycle ordering in offline tests."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.runs: dict[str, BankRateRun] = {}
        self.run_status: dict[str, str] = {}
        self._evidence_bytes: dict[str, bytes] = {}
        self.evidence: dict[str, PersistedEvidence] = {}
        self.observations: dict[str, BankRateRecord] = {}
        self.canonical: dict[tuple[str, str], str] = {}

    def create_run(self, run: BankRateRun) -> str:
        self.events.append("create_run")
        self.runs[run.run_id] = run
        self.run_status[run.run_id] = "running"
        return run.run_id

    def persist_evidence(self, run_id: str, artifact: BankRateArtifact) -> PersistedEvidence:
        self.events.append("persist_evidence")
        if run_id not in self.runs:
            raise BankRateError("unknown run")
        evidence = PersistedEvidence(new_id("ev"), artifact.content_sha256)
        self.evidence[evidence.evidence_id] = evidence
        if isinstance(artifact, StoredAcquiredArtifact):
            self._evidence_bytes[evidence.evidence_id] = artifact.artifact.path.read_bytes()
        else:
            self._evidence_bytes[evidence.evidence_id] = artifact.body
        return evidence

    def read_evidence(self, evidence: PersistedEvidence) -> bytes:
        self.events.append("read_evidence")
        return self._evidence_bytes[evidence.evidence_id]

    def persist_observation(
        self,
        run_id: str,
        evidence: PersistedEvidence,
        record: BankRateRecord,
        *,
        lane: str,
    ) -> str:
        self.events.append("persist_observation")
        if evidence.evidence_id not in self.evidence:
            raise BankRateError("unknown evidence")
        observation_id = new_id("obs")
        self.observations[observation_id] = record
        return observation_id

    def promote(self, run_id: str, observation_ids: Sequence[str], *, lane: str) -> bool:
        self.events.append("promote")
        if lane != "production_ingestion":
            return False
        changed = False
        for observation_id in observation_ids:
            record = self.observations[observation_id]
            previous = self.canonical.get(record.record_key)
            if previous != observation_id:
                changed = True
            self.canonical[record.record_key] = observation_id
        return changed

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        error: Mapping[str, str] | None = None,
    ) -> None:
        self.events.append(f"finish_run:{status}")
        self.run_status[run_id] = status
