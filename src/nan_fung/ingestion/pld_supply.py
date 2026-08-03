"""Planning-application activity capture-before-parse vertical slice.

Mirrors ``nan_fung.ingestion.bank_rate`` but for the planning.data.gov.uk
planning-application dataset.  The module owns no SQLite or filesystem state.
"""

from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .canonical import (
    content_sha256,
    format_date,
    format_timestamp,
    freeze_json,
    new_id,
    observation_hash,
    request_hash,
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
from .registry import (
    DatasourceRegistry,
    RuntimeBindings,
    default_registry,
    default_runtime_bindings,
)
from nan_fung.storage.artifacts import StoredArtifact

# ---------------------------------------------------------------------------
# Datasource identity
# ---------------------------------------------------------------------------

PLD_APPLICATIONS_SEARCH_DATASOURCE_ID = "pld.applications_search"
PLD_APPLICATION_DATASOURCE_ID = "pld.application"

PLD_APPLICATIONS_SEARCH_URL = "https://files.planning.data.gov.uk/dataset/planning-application.csv"

PLD_SOURCE_POLICY = SourcePolicy(
    allowed_hosts=("files.planning.data.gov.uk",),
    allowed_methods=("GET",),
    allowed_query_keys=(),
    artifact=ArtifactPolicy(
        max_bytes=200 * 1024 * 1024,  # Full CSV is large; allow 200 MiB.
        allowed_media_types=(
            "text/csv",
            "application/csv",
            "application/octet-stream",
            "text/plain",
        ),
    ),
)

# Canonical mapping of London planning authorities to short borough names.
# Source: planning.data.gov.uk local-authority.csv (verified 2026-08-03).
# entity IDs are stable strings here so they survive canonical JSON hashing.
LONDON_AUTHORITY_ENTITY_IDS: frozenset[str] = frozenset(
    {
        "41", "42", "43", "48", "65", "90", "100", "115", "126", "150",
        "162", "163", "167", "169", "170", "174", "175", "181", "182",
        "188", "192", "198", "203", "217", "246", "261", "266", "319",
        "329", "350", "366", "376", "387",
    }
)

LONDON_AUTHORITY_NAMES: Mapping[str, str] = {
    "41": "Barking and Dagenham",
    "42": "Brent",
    "43": "Bexley",
    "48": "Barnet",
    "65": "Bromley",
    "90": "Camden",
    "100": "Croydon",
    "115": "Ealing",
    "126": "Enfield",
    "150": "Greenwich",
    "162": "Havering",
    "163": "Hackney",
    "167": "Hillingdon",
    "169": "Hammersmith and Fulham",
    "170": "Hounslow",
    "174": "Harrow",
    "175": "Haringey",
    "181": "Islington",
    "182": "Kensington and Chelsea",
    "188": "Kingston upon Thames",
    "192": "Lambeth",
    "198": "Lewisham",
    "203": "City of London",
    "217": "Merton",
    "246": "Newham",
    "261": "Redbridge",
    "266": "Richmond upon Thames",
    "319": "Sutton",
    "329": "Southwark",
    "350": "Tower Hamlets",
    "366": "Waltham Forest",
    "376": "Wandsworth",
    "387": "Westminster",
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PLDError(ValueError):
    """Base error for PLD acquisition, parse, or lifecycle failures."""


class PLDParseError(PLDError):
    """Raised when a persisted PLD CSV is malformed."""


# ---------------------------------------------------------------------------
# Acquired artifact (mirror of bank_rate.AcquiredArtifact)
# ---------------------------------------------------------------------------


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
            raise PLDError("acquired body must be bytes")
        if self.status != 200:
            raise PLDError("PLD lifecycle requires a complete HTTP 200 response")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise PLDError("retrieved_at must be timezone-aware")
        object.__setattr__(self, "retrieved_at", self.retrieved_at.astimezone(UTC))


# Backwards-compat alias matching bank_rate's BankRateArtifact naming.
PLDArtifact = AcquiredArtifact


@dataclass(frozen=True, slots=True)
class StoredAcquiredArtifact:
    """Live PLD metadata for a body already verified in the CAS."""

    artifact: StoredArtifact
    request_url: str
    source_url: str
    retrieved_at: datetime
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    media_type: str = "text/csv"

    def __post_init__(self) -> None:
        if self.status != 200:
            raise PLDError("PLD lifecycle requires a complete HTTP 200 response")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise PLDError("retrieved_at must be timezone-aware")
        try:
            validate_source_url(self.request_url, PLD_SOURCE_POLICY, resolver=None)
            validate_source_url(self.source_url, PLD_SOURCE_POLICY, resolver=None)
        except PolicyError as error:
            raise PLDError("PLD artifact has unapproved provenance") from error
        object.__setattr__(self, "retrieved_at", self.retrieved_at.astimezone(UTC))
        object.__setattr__(self, "request_url", redact_url(self.request_url))
        object.__setattr__(self, "source_url", redact_url(self.source_url))
        object.__setattr__(self, "headers", freeze_json(redact_headers(self.headers)))


# ---------------------------------------------------------------------------
# Canonical record (monthly count per London authority)
# ---------------------------------------------------------------------------


def _decimal_text(value: int | str) -> str:
    """Normalize an integer count to a canonical decimal-string form.

    Mirrors bank_rate._decimal_text but for integer counts.  Counts are stored
    as strings per the canonical JSON convention (``numeric_value_type`` =
    ``decimal_string``); they must be non-negative and finite.
    """

    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise PLDParseError("empty numeric value")
    else:
        text = str(int(value))
    if text.startswith("-"):
        raise PLDParseError("counts must be non-negative")
    try:
        decimal = Decimal(text)
    except (InvalidOperation, ValueError) as cause:
        raise PLDParseError("count is not a valid decimal") from cause
    if not decimal.is_finite() or "E" in text.upper() or "e" in text:
        raise PLDParseError("exponent notation is not allowed in counts")
    if decimal < 0:
        raise PLDParseError("counts must be non-negative")
    # Strip trailing zeros but preserve integer form.
    normalized = format(decimal, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if not normalized:
        normalized = "0"
    return normalized


@dataclass(frozen=True, slots=True)
class PLDRecord:
    """Canonical monthly count record for one London authority."""

    organisation_entity: str
    borough: str
    period_year: int
    period_month: int
    planning_application_count: str

    def __post_init__(self) -> None:
        if self.organisation_entity not in LONDON_AUTHORITY_ENTITY_IDS:
            raise PLDParseError(
                f"organisation-entity {self.organisation_entity!r} is not a London authority"
            )
        expected_borough = LONDON_AUTHORITY_NAMES.get(self.organisation_entity)
        if expected_borough is None or self.borough != expected_borough:
            raise PLDParseError(
                f"borough {self.borough!r} does not match entity {self.organisation_entity!r}"
            )
        if not (1 <= self.period_month <= 12):
            raise PLDParseError("period_month must be 1..12")
        if self.period_year < 2000 or self.period_year > 2100:
            raise PLDParseError("period_year out of supported range")
        object.__setattr__(
            self, "planning_application_count", _decimal_text(self.planning_application_count)
        )

    @property
    def record_key(self) -> tuple[str, ...]:
        return (self.organisation_entity, f"{self.period_year:04d}-{self.period_month:02d}")

    @property
    def payload(self) -> Mapping[str, object]:
        return {
            "organisation_entity": self.organisation_entity,
            "borough": self.borough,
            "period_year": f"{self.period_year:04d}",
            "period_month": f"{self.period_month:02d}",
            "planning_application_count": self.planning_application_count,
            "metric_id": "planning_application_count",
        }


# ---------------------------------------------------------------------------
# Pure parser (sandbox-safe; module-level required by parser_runner.py)
# ---------------------------------------------------------------------------


def _parse_year_month(date_text: str) -> tuple[int, int]:
    """Parse YYYY-MM-DD or YYYY-MM into (year, month)."""

    if not date_text:
        raise PLDParseError("decision-date is empty")
    parts = date_text.strip().split("-")
    if len(parts) < 2:
        raise PLDParseError(f"unparseable decision-date {date_text!r}")
    try:
        year = int(parts[0])
        month = int(parts[1])
    except ValueError as cause:
        raise PLDParseError(f"decision-date {date_text!r} has non-numeric year/month") from cause
    return year, month


def parse_planning_applications_csv(payload: bytes) -> tuple[PLDRecord, ...]:
    """Parse a planning-application CSV payload into monthly count records.

    Groups rows by ``(organisation-entity, decision-date YYYY-MM)`` and emits
    one record per London authority per month.  Rows whose organisation-entity
    is not in :data:`LONDON_AUTHORITY_ENTITY_IDS` are silently skipped (they
    are non-London and outside this datasource's scope).
    """

    if not isinstance(payload, (bytes, bytearray)):
        raise PLDParseError("parser payload must be bytes")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as cause:
        raise PLDParseError("payload is not valid UTF-8") from cause
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise PLDParseError("CSV has no header row")
    fields = {name.strip() if name else "" for name in reader.fieldnames}
    required = {"organisation-entity", "decision-date"}
    missing = required - fields
    if missing:
        raise PLDParseError(f"CSV missing required columns: {sorted(missing)}")
    counts: dict[tuple[str, int, int], int] = defaultdict(int)
    seen: Counter[str] = Counter()
    skipped_non_london = 0
    skipped_invalid = 0
    for row in reader:
        entity = (row.get("organisation-entity") or "").strip()
        if not entity:
            skipped_invalid += 1
            continue
        if entity not in LONDON_AUTHORITY_ENTITY_IDS:
            skipped_non_london += 1
            continue
        decision = (row.get("decision-date") or "").strip()
        if not decision:
            # Undecided applications are not counted; they are not "decided".
            continue
        try:
            year, month = _parse_year_month(decision)
        except PLDParseError:
            skipped_invalid += 1
            continue
        if not (1 <= month <= 12) or not (2000 <= year <= 2100):
            skipped_invalid += 1
            continue
        counts[(entity, year, month)] += 1
        seen[entity] += 1
    records: list[PLDRecord] = []
    for (entity, year, month), count in counts.items():
        try:
            records.append(
                PLDRecord(
                    organisation_entity=entity,
                    borough=LONDON_AUTHORITY_NAMES[entity],
                    period_year=year,
                    period_month=month,
                    planning_application_count=str(count),
                )
            )
        except PLDParseError:
            skipped_invalid += 1
    if not records:
        raise PLDParseError(
            "no London planning-application rows with decision-date found in payload"
        )
    records.sort(key=lambda r: (r.organisation_entity, r.period_year, r.period_month))
    return tuple(records)


def parse_planning_applications_csv_isolated(payload: bytes) -> tuple[PLDRecord, ...]:
    """Run the pure CSV parser in the bounded artifact-parser child protocol."""

    from .parser_runner import ParserLimits, run_bounded_parser

    # The full planning-application CSV is ~45 MB; lift the default 32 MiB input
    # limit to 200 MiB to accommodate national growth.  Output is monthly-count
    # aggregates per London authority (33 entries per year), so 4 MiB is plenty.
    limits = ParserLimits(
        timeout_seconds=120.0,
        max_input_bytes=200 * 1024 * 1024,
        max_output_bytes=8 * 1024 * 1024,
    )
    parsed = run_bounded_parser(parse_planning_applications_csv, payload, limits=limits)
    if not isinstance(parsed, list):
        raise PLDParseError("isolated PLD parser returned invalid records")
    records: list[PLDRecord] = []
    for item in parsed:
        if not isinstance(item, Mapping):
            raise PLDParseError("isolated PLD parser returned invalid record")
        try:
            records.append(
                PLDRecord(
                    organisation_entity=str(item["organisation_entity"]),
                    borough=str(item["borough"]),
                    period_year=int(item["period_year"]),
                    period_month=int(item["period_month"]),
                    planning_application_count=str(item["planning_application_count"]),
                )
            )
        except (KeyError, ValueError, TypeError) as cause:
            raise PLDParseError("isolated PLD parser returned malformed record") from cause
    return tuple(records)


# ---------------------------------------------------------------------------
# Record-key binding
# ---------------------------------------------------------------------------


def pld_applications_search_record_key(
    record: PLDRecord | Mapping[str, object],
) -> tuple[str, ...]:
    """Natural key for an aggregated planning-activity record."""

    if isinstance(record, PLDRecord):
        return record.record_key
    entity = str(record["organisation_entity"])
    year_text = str(record["period_year"]).strip()
    month_text = str(record["period_month"]).strip()
    try:
        year = int(year_text)
        month = int(month_text)
    except ValueError as cause:
        raise PLDParseError("period_year/period_month must be integers") from cause
    if not (1 <= month <= 12):
        raise PLDParseError("period_month must be 1..12")
    return (entity, f"{year:04d}-{month:02d}")


# ---------------------------------------------------------------------------
# Collector (delegates to injected acquisition boundary)
# ---------------------------------------------------------------------------


def collect_planning_applications(
    acquire: Callable[..., AcquiredArtifact],
) -> AcquiredArtifact:
    """Invoke an injected acquisition boundary, never direct network I/O."""

    artifact = acquire(PLD_APPLICATIONS_SEARCH_URL, method="GET")
    if not isinstance(artifact, AcquiredArtifact):
        raise PLDError("acquisition boundary must return AcquiredArtifact")
    return artifact


# ---------------------------------------------------------------------------
# Persistence protocol and lifecycle (mirror bank_rate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PLDRun:
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
            raise PLDError("run requested_at must be timezone-aware")
        object.__setattr__(self, "requested_at", self.requested_at.astimezone(UTC))
        object.__setattr__(self, "request", freeze_json(self.request))


@dataclass(frozen=True, slots=True)
class PersistedEvidence:
    evidence_id: str
    content_sha256: str


class PLDPersistence(Protocol):
    """Minimal ingestion repository contract; implementation is deliberately local."""

    def create_run(self, run: PLDRun) -> str: ...

    def persist_evidence(self, run_id: str, artifact: PLDArtifact) -> PersistedEvidence: ...

    def read_evidence(self, evidence: PersistedEvidence) -> bytes: ...

    def persist_observation(
        self,
        run_id: str,
        evidence: PersistedEvidence,
        record: PLDRecord,
        *,
        lane: str,
    ) -> str: ...

    def promote(self, run_id: str, observation_ids: Sequence[str], *, lane: str) -> bool: ...

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        error: Mapping[str, str] | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PLDLifecycleResult:
    run_id: str
    evidence_id: str
    observation_ids: tuple[str, ...]
    status: str
    canonical_changed: bool


class PLDApplicationsSearchLifecycle:
    """Registry -> persisted evidence -> parser -> observations -> promotion."""

    DATASOURCE_ID = PLD_APPLICATIONS_SEARCH_DATASOURCE_ID

    def __init__(
        self,
        persistence: PLDPersistence,
        *,
        registry: DatasourceRegistry | None = None,
        bindings: RuntimeBindings | None = None,
        record_parser: Callable[[bytes], Sequence[PLDRecord]] = parse_planning_applications_csv,
    ) -> None:
        self._persistence = persistence
        self._registry = registry or default_registry()
        self._bindings = bindings or default_runtime_bindings()
        self._record_parser = record_parser

    def ingest(
        self,
        artifact: PLDArtifact,
        *,
        lane: str = "production_ingestion",
        requested_at: datetime | None = None,
        definition_version: int | None = None,
    ) -> PLDLifecycleResult:
        """Ingest a pre-acquired artifact while enforcing capture-before-parse."""

        if lane not in {"production_ingestion", "source_discovery", "ad_hoc_research"}:
            raise PLDError(f"unsupported lane: {lane!r}")
        definition = self._registry.lookup(self.DATASOURCE_ID, definition_version)
        self._bindings.validate(definition, operation="ingest").require()
        now = (requested_at or artifact.retrieved_at).astimezone(UTC)
        request = {
            "method": "GET",
            "url": artifact.source_url,
            "source_url": artifact.source_url,
        }
        run = PLDRun(
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
                raise PLDError("saved evidence hash does not match evidence handle")
            records = tuple(self._record_parser(saved_bytes))
            observation_ids: list[str] = []
            for record in records:
                observation_ids.append(
                    self._persistence.persist_observation(
                        run_id, evidence, record, lane=lane
                    )
                )
            if not records:
                self._persistence.finish_run(run_id, status="empty")
                return PLDLifecycleResult(
                    run_id, evidence.evidence_id, (), "empty", False
                )
            canonical_changed = False
            if lane == "production_ingestion":
                canonical_changed = self._persistence.promote(
                    run_id, observation_ids, lane=lane
                )
            self._persistence.finish_run(run_id, status="succeeded")
            return PLDLifecycleResult(
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


# ---------------------------------------------------------------------------
# Record validation (mirrors bank_rate.validate_bank_rate_record)
# ---------------------------------------------------------------------------


def validate_planning_application_record(record: PLDRecord) -> None:
    """Validate that a parsed record carries a non-negative count."""

    count = Decimal(record.planning_application_count)
    if count < 0:
        raise PLDParseError("planning_application_count must be non-negative")


class InMemoryPLDPersistence:
    """Small reference adapter proving lifecycle ordering in offline tests."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.runs: dict[str, PLDRun] = {}
        self.run_status: dict[str, str] = {}
        self._evidence_bytes: dict[str, bytes] = {}
        self.evidence: dict[str, PersistedEvidence] = {}
        self.observations: dict[str, PLDRecord] = {}
        self.canonical: dict[tuple[str, ...], str] = {}

    def create_run(self, run: PLDRun) -> str:
        self.events.append("create_run")
        self.runs[run.run_id] = run
        self.run_status[run.run_id] = "running"
        return run.run_id

    def persist_evidence(self, run_id: str, artifact: PLDArtifact) -> PersistedEvidence:
        self.events.append("persist_evidence")
        if run_id not in self.runs:
            raise PLDError("unknown run")
        evidence = PersistedEvidence(new_id("ev"), content_sha256(artifact.body))
        self.evidence[evidence.evidence_id] = evidence
        if isinstance(artifact, StoredArtifact):
            self._evidence_bytes[evidence.evidence_id] = artifact.path.read_bytes()
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
        record: PLDRecord,
        *,
        lane: str,
    ) -> str:
        self.events.append("persist_observation")
        if evidence.evidence_id not in self.evidence:
            raise PLDError("unknown evidence")
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


__all__ = [
    "AcquiredArtifact",
    "InMemoryPLDPersistence",
    "LONDON_AUTHORITY_ENTITY_IDS",
    "LONDON_AUTHORITY_NAMES",
    "PLD_APPLICATION_DATASOURCE_ID",
    "PLD_APPLICATIONS_SEARCH_DATASOURCE_ID",
    "PLD_APPLICATIONS_SEARCH_URL",
    "PLD_SOURCE_POLICY",
    "PLDArtifact",
    "PLDError",
    "PLDLifecycleResult",
    "PLDApplicationsSearchLifecycle",
    "PLDParseError",
    "PLDPersistence",
    "PLDRecord",
    "PLDRun",
    "PersistedEvidence",
    "StoredAcquiredArtifact",
    "collect_planning_applications",
    "parse_planning_applications_csv",
    "parse_planning_applications_csv_isolated",
    "pld_applications_search_record_key",
    "validate_planning_application_record",
]


# ---------------------------------------------------------------------------
# Module-level self-check (per ponytail convention for non-trivial parsers).
# Runs only when invoked explicitly: ``python -m nan_fung.ingestion.pld_supply``
# ---------------------------------------------------------------------------


def _self_check() -> None:
    sample_header = "organisation-entity,decision-date\n"
    sample_rows = [
        "41,2025-03-12\n",  # Barking and Dagenham
        "41,2025-03-25\n",  # Barking and Dagenham again
        "41,2025-04-01\n",
        "203,2025-03-10\n",  # City of London
        "9999,2025-03-01\n",  # Non-London, must be skipped
        "41,\n",  # No decision-date, must be skipped
    ]
    payload = (sample_header + "".join(sample_rows)).encode("utf-8")
    records = parse_planning_applications_csv(payload)
    assert len(records) == 3, f"expected 3 records, got {len(records)}: {records}"
    by_key = {r.record_key: r for r in records}
    assert by_key[("41", "2025-03")].planning_application_count == "2"
    assert by_key[("41", "2025-04")].planning_application_count == "1"
    assert by_key[("203", "2025-03")].planning_application_count == "1"
    # Validation passes for all positive counts.
    for record in records:
        validate_planning_application_record(record)
    # Record-key binding round-trips.
    for record in records:
        assert pld_applications_search_record_key(record) == record.record_key
    print(f"self-check OK: {len(records)} records parsed and validated")


if __name__ == "__main__":
    _self_check()
