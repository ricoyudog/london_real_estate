"""The ``cre`` operator CLI with one versioned JSON document on stdout."""

from __future__ import annotations

import argparse
from dataclasses import asdict, fields, is_dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
import json
import os
from pathlib import Path
import signal
import stat
import sys
import threading
from typing import Any, Mapping, Sequence

from nan_fung.backups import create_backup_set, restore_backup_set, verify_backup_set
from nan_fung.config import AppConfig, ConfigurationError, load_config, load_cursor_secret
from nan_fung.ingestion.bank_rate import AcquiredArtifact
from nan_fung.ingestion.file_release_lifecycle import (
    reparse_file_release_evidence,
)
from nan_fung.ingestion.file_release_workflow import (
    FILE_RELEASE_AUTOMATIC_DATASOURCE_IDS,
)
from nan_fung.ingestion.official_macro_lifecycle import (
    OFFICIAL_MACRO_AUTOMATIC_DATASOURCE_IDS,
    reparse_official_macro_evidence,
)
from nan_fung.ingestion.onspd_lifecycle import (
    ONSPD_DATASOURCE_ID,
    reparse_onspd_postcode_evidence,
)
from nan_fung.operational import OperationalError, OperationalStore
from nan_fung.read_api import ReadContext, ReadQuery, ReadService, SQLiteReadRepository
from nan_fung.storage.db import integrity_check
from nan_fung.supervisor import DatasourceSupervisor, SupervisorRun
from nan_fung.workflows import (
    ingest_bank_rate_artifact,
    reparse_bank_rate_evidence,
)


CLI_SCHEMA_VERSION = "cre_cli.v1"


class CliError(ValueError):
    code = "CLI_INVALID_REQUEST"


class CliAuthorizationError(CliError):
    code = "CLI_ACCESS_DENIED"


class JsonArgumentParser(argparse.ArgumentParser):
    def add_subparsers(self, **kwargs: object):
        kwargs.setdefault("parser_class", JsonArgumentParser)
        return super().add_subparsers(**kwargs)

    def error(self, message: str) -> None:
        raise CliError(message)


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one bounded operator command and emit exactly one JSON document."""

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        config = load_config(
            config_path=args.config,
            data_dir=args.data_dir,
            environment=args.environment,
            instance_id=args.instance_id,
        )
        _authorize_command(args, config.operator_role)
        store = OperationalStore(config.data_dir, backup_dir=config.backup_dir)
        payload = _dispatch(args, store, config.instance_id, config)
    except (CliError, ConfigurationError, OperationalError, FileExistsError, ValueError) as error:
        _emit(
            {
                "schema_version": CLI_SCHEMA_VERSION,
                "ok": False,
                "error": {"code": getattr(error, "code", "CLI_OPERATION_FAILED"), "type": type(error).__name__},
            }
        )
        return 2
    except Exception as error:  # Keep operational stdout bounded and non-sensitive.
        _emit(
            {
                "schema_version": CLI_SCHEMA_VERSION,
                "ok": False,
                "error": {"code": "CLI_UNEXPECTED_ERROR", "type": type(error).__name__},
            }
        )
        return 1
    _emit({"schema_version": CLI_SCHEMA_VERSION, "ok": True, "result": payload})
    return 0


def _build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="cre", add_help=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--environment", choices=("development", "test", "production"))
    parser.add_argument("--instance-id")
    commands = parser.add_subparsers(dest="command", required=True)

    db = commands.add_parser("db")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser("migrate")
    db_commands.add_parser("integrity")

    registry = commands.add_parser("registry")
    registry_commands = registry.add_subparsers(dest="registry_command", required=True)
    registry_commands.add_parser("sync")
    registry_commands.add_parser("diff")
    approve = registry_commands.add_parser("approve")
    approve.add_argument("datasource_id")
    approve.add_argument("--definition-version", type=int)

    datasource = commands.add_parser("datasource")
    datasource_commands = datasource.add_subparsers(dest="datasource_command", required=True)
    datasource_commands.add_parser("list")
    datasource_commands.add_parser("status")

    ingest = commands.add_parser("ingest")
    ingest_commands = ingest.add_subparsers(dest="ingest_command", required=True)
    enqueue = ingest_commands.add_parser("enqueue")
    enqueue.add_argument("datasource_id")
    enqueue.add_argument("--request", default="{}")
    enqueue.add_argument("--lane", choices=("production_ingestion", "source_discovery", "ad_hoc_research"))
    enqueue.add_argument("--scheduled-for")
    bank_rate = ingest_commands.add_parser("bank-rate")
    source = bank_rate.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", type=Path)
    source.add_argument("--live", action="store_true")
    bank_rate.add_argument("--lane", default="production_ingestion", choices=("production_ingestion", "source_discovery", "ad_hoc_research"))
    bank_rate.add_argument("--source-url", default="https://www.bankofengland.co.uk/offline-fixture.csv")
    reparse = ingest_commands.add_parser("reparse")
    reparse.add_argument("datasource_id")
    reparse.add_argument("--evidence-id", required=True)
    reparse.add_argument("--lane", choices=("production_ingestion", "source_discovery", "ad_hoc_research"))

    backfill = commands.add_parser("backfill")
    backfill.add_argument("datasource_id")
    backfill.add_argument("--from", dest="window_start", required=True)
    backfill.add_argument("--to", dest="window_end", required=True)
    backfill.add_argument("--lane", choices=("production_ingestion", "source_discovery", "ad_hoc_research"))

    jobs = commands.add_parser("jobs")
    jobs_commands = jobs.add_subparsers(dest="jobs_command", required=True)
    jobs_commands.add_parser("list")
    job_get = jobs_commands.add_parser("get")
    job_get.add_argument("job_id")
    job_retry = jobs_commands.add_parser("retry")
    job_retry.add_argument("job_id")
    job_cancel = jobs_commands.add_parser("cancel")
    job_cancel.add_argument("job_id")

    scheduler = commands.add_parser("scheduler")
    scheduler_commands = scheduler.add_subparsers(dest="scheduler_command", required=True)
    tick = scheduler_commands.add_parser("tick")
    tick.add_argument("--at")

    daemon = commands.add_parser("daemon")
    daemon_commands = daemon.add_subparsers(dest="daemon_command", required=True)
    once = daemon_commands.add_parser("once")
    once.add_argument("--at")
    once.add_argument("--allow-network", action="store_true")
    once.add_argument("--onspd-retention-until")
    run = daemon_commands.add_parser("run")
    run.add_argument("--allow-network", action="store_true")
    run.add_argument("--onspd-retention-until")
    run.add_argument("--poll-interval-seconds", type=float, default=30.0)

    commands.add_parser("health")
    commands.add_parser("metrics")
    evidence = commands.add_parser("evidence")
    evidence_commands = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_commands.add_parser("verify")
    evidence_import = evidence_commands.add_parser("import")
    evidence_import.add_argument("datasource_id")
    evidence_import.add_argument("file", type=Path)
    evidence_import.add_argument("--media-type", required=True)
    evidence_import.add_argument("--source-url")
    evidence_import.add_argument("--attestation")
    evidence_import.add_argument("--retention-until")

    retention = commands.add_parser("retention")
    retention_commands = retention.add_subparsers(dest="retention_command", required=True)
    retention_dry = retention_commands.add_parser("dry-run")
    retention_dry.add_argument("--as-of")

    review = commands.add_parser("review")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_list = review_commands.add_parser("list")
    review_list.add_argument("--state", choices=("open", "approved", "rejected", "cancelled"))
    review_decide = review_commands.add_parser("decide")
    review_decide.add_argument("review_id")
    review_decide.add_argument("decision", choices=("approved", "rejected"))
    review_decide.add_argument("--reason")
    review_promote = review_commands.add_parser("promote")
    review_promote.add_argument("review_id")
    review_promote.add_argument("--reason")
    review_revoke = review_commands.add_parser("revoke")
    review_revoke.add_argument("run_id")
    review_revoke.add_argument("--reason")

    backup = commands.add_parser("backup")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    backup_create = backup_commands.add_parser("create")
    backup_create.add_argument("directory", type=Path)
    backup_verify = backup_commands.add_parser("verify")
    backup_verify.add_argument("directory", type=Path)
    backup_restore = backup_commands.add_parser("restore")
    backup_restore.add_argument("source", type=Path)
    backup_restore.add_argument("target_data_dir", type=Path)

    projections = commands.add_parser("projections")
    projections_commands = projections.add_subparsers(dest="projections_command", required=True)
    projections_commands.add_parser("rebuild")
    projection_enqueue = projections_commands.add_parser("enqueue")
    projection_enqueue.add_argument("--output-directory", type=Path, required=True)
    projection_enqueue.add_argument("--as-of", required=True)
    projection_publish = projections_commands.add_parser("publish")
    projection_publish.add_argument("--output-directory", type=Path, required=True)
    projection_publish.add_argument("--as-of", required=True)

    observations = commands.add_parser("observations")
    observations_commands = observations.add_subparsers(dest="observations_command", required=True)
    for name in ("latest", "as-of"):
        query = observations_commands.add_parser(name)
        query.add_argument("--kind", default="metrics", choices=("metrics", "supply", "events", "geographies", "health"))
        query.add_argument("--as-of")
        query.add_argument("--cursor")
        query.add_argument("--limit", type=int, default=100)
        query.add_argument("--datasource-id")
        query.add_argument("--category")
    return parser


def _dispatch(
    args: argparse.Namespace,
    store: OperationalStore,
    instance_id: str,
    config: AppConfig,
) -> object:
    if args.command == "db":
        if args.db_command == "migrate":
            return {
                "command": "db.migrate",
                "migrations": list(store.migrate()),
                "registry": store.sync_registry(),
            }
        report = integrity_check(store.database_path)
        return {
            "command": "db.integrity",
            "ok": report.ok,
            "quick_check": list(report.quick_check),
            "integrity_check": list(report.integrity_check),
            "foreign_key_violations": [list(row) for row in report.foreign_key_violations],
        }
    if args.command == "registry":
        if args.registry_command == "sync":
            return {"command": "registry.sync", **store.sync_registry()}
        if args.registry_command == "diff":
            return {"command": "registry.diff", **store.registry_diff()}
        return {
            "command": "registry.approve",
            "datasource_id": args.datasource_id,
            "definition_version": args.definition_version,
            "state": "blocked",
            "code": "IMMUTABLE_REGISTRY_REQUIRES_NEW_VERSION",
        }
    if args.command == "datasource":
        status = list(store.registry_status())
        return {"command": f"datasource.{args.datasource_command}", "datasources": status}
    if args.command == "ingest":
        return _ingest_command(args, store)
    if args.command == "backfill":
        result = store.enqueue_backfill(
            args.datasource_id,
            window_start=_parse_time(args.window_start),
            window_end=_parse_time(args.window_end),
            lane=args.lane,
        )
        return {"command": "backfill", **asdict(result)}
    if args.command == "jobs":
        if args.jobs_command == "list":
            return {"command": "jobs.list", "jobs": list(store.jobs())}
        if args.jobs_command == "get":
            return {"command": "jobs.get", "job": store.get_job(args.job_id)}
        if args.jobs_command == "retry":
            return {"command": "jobs.retry", **asdict(store.retry(args.job_id))}
        return {"command": "jobs.cancel", "cancelled": store.cancel(args.job_id)}
    if args.command == "scheduler":
        return {
            "command": "scheduler.tick",
            **store.scheduler_tick(now=_parse_time(args.at) if args.at else None),
        }
    if args.command == "daemon":
        if not args.allow_network:
            raise CliError("daemon execution requires --allow-network")
        supervisor = DatasourceSupervisor(
            store,
            worker_id=instance_id,
            allow_network=True,
            onspd_retention_until=(
                _parse_time(args.onspd_retention_until)
                if args.onspd_retention_until
                else None
            ),
        )
        if args.daemon_command == "once":
            result = supervisor.run_once(
                now=_parse_time(args.at) if args.at else None
            )
            return {"command": "daemon.once", **asdict(result)}
        return {"command": "daemon.run", **asdict(_run_daemon(supervisor, args))}
    if args.command == "health":
        return {"command": "health", **store.health()}
    if args.command == "metrics":
        return {"command": "metrics", **store.metrics()}
    if args.command == "evidence":
        if args.evidence_command == "verify":
            return {"command": "evidence.verify", **store.verify_evidence()}
        result = store.import_manual_evidence(
            args.datasource_id,
            _read_fixture(args.file),
            media_type=args.media_type,
            source_url=args.source_url,
            attestation=args.attestation,
            retention_until=(
                _parse_time(args.retention_until) if args.retention_until else None
            ),
            actor_id=instance_id,
        )
        return {"command": "evidence.import", **asdict(result)}
    if args.command == "retention":
        return {
            "command": "retention.dry_run",
            **store.retention_dry_run(as_of=_parse_time(args.as_of) if args.as_of else None),
        }
    if args.command == "review":
        if args.review_command == "list":
            return {"command": "review.list", "reviews": list(store.review_tasks(state=args.state))}
        if args.review_command == "decide":
            return {
                "command": "review.decide",
                "changed": store.decide_review(
                    args.review_id,
                    decision=args.decision,
                    actor_id=instance_id,
                    reason=args.reason,
                ),
            }
        if args.review_command == "revoke":
            return {
                "command": "review.revoke",
                **asdict(store.revoke_promotion(
                    args.run_id,
                    actor_id=instance_id,
                    reason=args.reason,
                )),
            }
        return {
            "command": "review.promote",
            **asdict(store.promote_review(
                args.review_id,
                actor_id=instance_id,
                reason=args.reason,
            )),
        }
    if args.command == "backup":
        if args.backup_command == "create":
            return {"command": "backup.create", **create_backup_set(store, args.directory).as_json()}
        if args.backup_command == "verify":
            return {"command": "backup.verify", **verify_backup_set(args.directory).as_json()}
        return {
            "command": "backup.restore",
            **restore_backup_set(args.source, args.target_data_dir).as_json(),
        }
    if args.command == "projections":
        if args.projections_command == "rebuild":
            return {"command": "projections.rebuild", **store.rebuild_projections().as_json()}
        if args.projections_command == "enqueue":
            result = store.enqueue_projection_delivery(
                args.output_directory,
                as_of_at=_parse_time(args.as_of),
            )
            return {"command": "projections.enqueue", **asdict(result)}
        report = store.publish_projections(
            args.output_directory,
            as_of_at=_parse_time(args.as_of),
            actor_id=instance_id,
        )
        return {"command": "projections.publish", **report.as_json()}
    if args.command == "observations":
        return _observation_command(args, store, config)
    raise CliError("unknown command")


_ROLE_RANK = {"read": 1, "write": 2, "admin": 3}


def _authorize_command(args: argparse.Namespace, operator_role: str) -> None:
    """Apply the config-owned local-operator capability boundary.

    The role is deliberately not a command-line option: production config is
    private and the host account controls access to it.  Agent-facing callers
    use the typed read/refresh APIs instead of this local operator boundary.
    """

    if args.command == "db":
        required = "admin" if args.db_command == "migrate" else "read"
    elif args.command == "registry":
        required = "read" if args.registry_command == "diff" else "admin"
    elif args.command in {"datasource", "health", "metrics", "retention", "observations"}:
        required = "read"
    elif args.command == "ingest":
        required = "write"
    elif args.command in {"backfill", "scheduler", "daemon"}:
        required = "write"
    elif args.command == "jobs":
        required = "read" if args.jobs_command in {"list", "get"} else "write"
    elif args.command == "evidence":
        required = "read" if args.evidence_command == "verify" else "write"
    elif args.command == "review":
        required = "read" if args.review_command == "list" else "write"
    elif args.command == "backup":
        required = "read" if args.backup_command == "verify" else "admin"
    elif args.command == "projections":
        required = "write"
    else:
        raise CliError("unknown command")
    if _ROLE_RANK[operator_role] < _ROLE_RANK[required]:
        raise CliAuthorizationError("operator role does not allow this command")


def _ingest_command(args: argparse.Namespace, store: OperationalStore) -> object:
    if args.ingest_command == "enqueue":
        request = _json_object(args.request, "--request")
        result = store.enqueue(
            args.datasource_id,
            request=request,
            lane=args.lane,
            scheduled_for=_parse_time(args.scheduled_for) if args.scheduled_for else None,
        )
        return {"command": "ingest.enqueue", **asdict(result)}
    if args.ingest_command == "bank-rate":
        if args.live:
            raise CliError(
                "live Bank Rate ingestion is daemon-only; use daemon once --allow-network"
            )
        else:
            assert args.fixture is not None
            artifact = AcquiredArtifact(
                body=_read_fixture(args.fixture),
                source_url=args.source_url,
                retrieved_at=datetime.now(UTC),
            )
        result = ingest_bank_rate_artifact(store, artifact, lane=args.lane)
        return {"command": "ingest.bank_rate", **asdict(result)}
    if args.datasource_id == "boe.bank_rate.iudbedr":
        result = reparse_bank_rate_evidence(
            store, args.evidence_id, lane=args.lane
        )
    elif args.datasource_id in OFFICIAL_MACRO_AUTOMATIC_DATASOURCE_IDS:
        result = reparse_official_macro_evidence(
            store, args.datasource_id, args.evidence_id, lane=args.lane
        )
    elif args.datasource_id in FILE_RELEASE_AUTOMATIC_DATASOURCE_IDS:
        result = reparse_file_release_evidence(
            store, args.datasource_id, args.evidence_id, lane=args.lane
        )
    elif args.datasource_id == ONSPD_DATASOURCE_ID:
        result = reparse_onspd_postcode_evidence(
            store, args.evidence_id, lane=args.lane
        )
    else:
        raise CliError("offline reparse is not bound for this datasource")
    return {"command": "ingest.reparse", **asdict(result)}


def _observation_command(
    args: argparse.Namespace, store: OperationalStore, config: AppConfig
) -> object:
    as_of = _parse_time(args.as_of) if args.as_of else datetime.now(UTC)
    filters: dict[str, object] = {}
    if args.datasource_id:
        filters["datasource_id"] = args.datasource_id
    if args.category:
        filters["category"] = args.category
    service = ReadService(
        SQLiteReadRepository(store.database_path),
        cursor_secret=load_cursor_secret(config),
    )
    response = service.query(
        ReadContext(config.instance_id, frozenset({"open"})),
        ReadQuery(
            args.kind,
            filters=filters,
            as_of=as_of,
            cursor=args.cursor,
            limit=args.limit,
        ),
    )
    return {"command": f"observations.{args.observations_command}", "response": response}


def _run_daemon(
    supervisor: DatasourceSupervisor, args: argparse.Namespace
) -> SupervisorRun:
    """Run the resident worker until SIGINT/SIGTERM requests a clean stop."""

    stop_event = threading.Event()

    def request_stop(_signal_number: int, _frame: object) -> None:
        stop_event.set()

    previous_handlers: dict[int, object] = {}
    try:
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signal_number] = signal.signal(signal_number, request_stop)
    except ValueError as error:
        for signal_number, previous in previous_handlers.items():
            signal.signal(signal_number, previous)  # type: ignore[arg-type]
        raise CliError("daemon run must execute in the main thread") from error
    try:
        return supervisor.run_until(
            should_stop=stop_event.is_set,
            wait=stop_event.wait,
            poll_interval_seconds=args.poll_interval_seconds,
        )
    finally:
        for signal_number, previous in previous_handlers.items():
            signal.signal(signal_number, previous)  # type: ignore[arg-type]


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise CliError("timestamp must be ISO-8601 with timezone") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CliError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _json_object(value: str, name: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise CliError(f"{name} must be JSON") from error
    if not isinstance(parsed, dict):
        raise CliError(f"{name} must be a JSON object")
    return parsed


def _read_fixture(path: Path, *, max_bytes: int = 4 * 1024 * 1024) -> bytes:
    """Open an explicitly supplied fixture without following a symlink."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise CliError("fixture must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as input_file:
            body = input_file.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise CliError("fixture exceeds the Bank Rate artifact limit")
        return body
    finally:
        os.close(descriptor)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, frozenset, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Enum):
        return value.value
    return value


def _emit(value: Mapping[str, object]) -> None:
    sys.stdout.write(
        json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )
