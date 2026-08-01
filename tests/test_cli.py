from __future__ import annotations

import json
from pathlib import Path

from nan_fung.cli import main
from nan_fung.storage.db import connect_database
from nan_fung.supervisor import SupervisorRun


def _invoke(capsys: object, *arguments: str) -> tuple[int, dict[str, object]]:
    status = main(arguments)
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    return status, json.loads(output)


def _role_config(tmp_path: Path, role: str) -> Path:
    config_path = tmp_path / "cre.toml"
    config_path.write_text(
        "[runtime]\n"
        f"data_dir = {str(tmp_path / 'state')!r}\n"
        f"operator_role = {role!r}\n",
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    return config_path


def test_cli_migrate_health_and_registry_are_single_json_documents(
    tmp_path: Path, capsys: object
) -> None:
    data_dir = tmp_path / "state"

    status, migrated = _invoke(capsys, "--data-dir", str(data_dir), "db", "migrate")
    health_status, health = _invoke(capsys, "--data-dir", str(data_dir), "health")
    diff_status, diff = _invoke(capsys, "--data-dir", str(data_dir), "registry", "diff")

    assert status == health_status == diff_status == 0
    assert migrated["ok"] is True
    assert health["result"]["integrity_ok"] is True  # type: ignore[index]
    assert "parser_isolation" in health["result"]  # type: ignore[operator]
    assert "operational_ingestion" in health["result"]  # type: ignore[operator]
    assert health["result"]["production_runtime_unbound"] == []  # type: ignore[index]
    assert diff["result"]["missing_in_store"] == []  # type: ignore[index]


def test_cli_datasource_status_returns_packaged_registry_definitions(
    tmp_path: Path, capsys: object
) -> None:
    status, output = _invoke(
        capsys,
        "--data-dir",
        str(tmp_path / "state"),
        "datasource",
        "status",
    )

    assert status == 0
    datasources = output["result"]["datasources"]  # type: ignore[index]
    bank_rate = next(
        item for item in datasources if item["datasource_id"] == "boe.bank_rate.iudbedr"
    )
    assert bank_rate["runtime_ready"] is True


def test_cli_read_role_is_denied_mutations_without_bootstrapping_state(
    tmp_path: Path, capsys: object
) -> None:
    config_path = _role_config(tmp_path, "read")

    migrate_status, migrate = _invoke(capsys, "--config", str(config_path), "db", "migrate")
    enqueue_status, enqueue = _invoke(
        capsys,
        "--config",
        str(config_path),
        "ingest",
        "enqueue",
        "boe.bank_rate.iudbedr",
    )
    status_status, status = _invoke(
        capsys, "--config", str(config_path), "datasource", "status"
    )

    assert migrate_status == enqueue_status == 2
    assert migrate["error"]["code"] == "CLI_ACCESS_DENIED"  # type: ignore[index]
    assert enqueue["error"]["code"] == "CLI_ACCESS_DENIED"  # type: ignore[index]
    assert status_status == 0
    assert status["result"]["datasources"]  # type: ignore[index]
    assert not (tmp_path / "state").exists()


def test_cli_ingests_fixture_and_reads_canonical_metric(
    tmp_path: Path, capsys: object
) -> None:
    data_dir = tmp_path / "state"
    fixture = tmp_path / "bank-rate.csv"
    fixture.write_bytes(b"DATE,IUDBEDR\n31 Jul 2026,3.75\n")

    status, ingested = _invoke(
        capsys,
        "--data-dir",
        str(data_dir),
        "ingest",
        "bank-rate",
        "--fixture",
        str(fixture),
    )
    read_status, read = _invoke(
        capsys,
        "--data-dir",
        str(data_dir),
        "observations",
        "latest",
    )

    assert status == read_status == 0
    assert ingested["result"]["status"] == "succeeded"  # type: ignore[index]
    records = read["result"]["response"]["records"]  # type: ignore[index]
    assert records[0]["payload"]["bank_rate_percent"] == "3.75"  # type: ignore[index]


def test_cli_refuses_daemon_network_execution_without_explicit_flag(
    tmp_path: Path, capsys: object
) -> None:
    status, output = _invoke(capsys, "--data-dir", str(tmp_path / "state"), "daemon", "once")

    assert status == 2
    assert output["error"]["code"] == "CLI_INVALID_REQUEST"  # type: ignore[index]


def test_cli_accepts_an_explicit_onspd_retention_deadline_for_daemon_execution(
    tmp_path: Path, capsys: object
) -> None:
    status, output = _invoke(
        capsys,
        "--data-dir",
        str(tmp_path / "state"),
        "daemon",
        "once",
        "--allow-network",
        "--onspd-retention-until",
        "2030-01-01T00:00:00Z",
    )

    assert status == 0
    assert output["result"]["state"] == "idle"  # type: ignore[index]


def test_cli_runs_resident_daemon_with_a_bounded_poll_interval(
    tmp_path: Path, capsys: object, monkeypatch: object
) -> None:
    received: dict[str, object] = {}

    def run_until(self: object, **kwargs: object) -> SupervisorRun:
        received.update(kwargs)
        return SupervisorRun(
            "supervisor_run.v1", 1, "idle", None, "stopping"
        )

    monkeypatch.setattr("nan_fung.cli.DatasourceSupervisor.run_until", run_until)  # type: ignore[attr-defined]

    status, output = _invoke(
        capsys,
        "--data-dir",
        str(tmp_path / "state"),
        "daemon",
        "run",
        "--allow-network",
        "--poll-interval-seconds",
        "12.5",
    )

    assert status == 0
    assert output["result"]["shutdown_state"] == "stopping"  # type: ignore[index]
    assert received["poll_interval_seconds"] == 12.5


def test_cli_rejects_direct_live_bank_rate_bypass(tmp_path: Path, capsys: object) -> None:
    status, output = _invoke(
        capsys,
        "--data-dir",
        str(tmp_path / "state"),
        "ingest",
        "bank-rate",
        "--live",
    )

    assert status == 2
    assert output["error"]["code"] == "CLI_INVALID_REQUEST"  # type: ignore[index]


def test_cli_imports_manual_evidence_without_accepting_a_path_in_json(
    tmp_path: Path, capsys: object
) -> None:
    evidence = tmp_path / "report.pdf"
    evidence.write_bytes(b"%PDF-1.7\n%%EOF\n")

    status, output = _invoke(
        capsys,
        "--data-dir",
        str(tmp_path / "state"),
        "evidence",
        "import",
        "bnp.central_london_office_report",
        str(evidence),
        "--media-type",
        "application/pdf",
        "--retention-until",
        "2030-01-01T00:00:00Z",
    )

    assert status == 0
    assert output["result"]["review_id"] is not None  # type: ignore[index]


def test_cli_promotes_only_an_approved_manual_review_once(
    tmp_path: Path, capsys: object
) -> None:
    data_dir = tmp_path / "state"
    evidence = tmp_path / "submarkets.json"
    evidence.write_bytes(b'{"name":"West End","locations":["Mayfair"]}')

    imported_status, imported = _invoke(
        capsys,
        "--data-dir",
        str(data_dir),
        "evidence",
        "import",
        "custom.london_office_submarkets",
        str(evidence),
        "--media-type",
        "application/json",
        "--attestation",
        "mapping checked",
    )
    review_id = imported["result"]["review_id"]  # type: ignore[index]
    decided_status, _ = _invoke(
        capsys,
        "--data-dir",
        str(data_dir),
        "review",
        "decide",
        review_id,
        "approved",
    )
    promoted_status, promoted = _invoke(
        capsys,
        "--data-dir",
        str(data_dir),
        "review",
        "promote",
        review_id,
        "--reason",
        "operator checked",
    )
    repeated_status, repeated = _invoke(
        capsys,
        "--data-dir",
        str(data_dir),
        "review",
        "promote",
        review_id,
    )

    assert imported_status == decided_status == promoted_status == repeated_status == 0
    assert promoted["result"]["command"] == "review.promote"  # type: ignore[index]
    assert promoted["result"]["created"] is True  # type: ignore[index]
    assert repeated["result"]["created"] is False  # type: ignore[index]
    assert repeated["result"]["promotion_id"] == promoted["result"]["promotion_id"]  # type: ignore[index]


def test_cli_revoke_requires_a_known_promoted_run(tmp_path: Path, capsys: object) -> None:
    status, output = _invoke(
        capsys,
        "--data-dir",
        str(tmp_path / "state"),
        "review",
        "revoke",
        "run_missing",
    )

    assert status == 2
    assert output["error"]["code"] == "CLI_OPERATION_FAILED"  # type: ignore[index]


def test_cli_publishes_canonical_projection_outputs_with_durable_lineage(
    tmp_path: Path, capsys: object
) -> None:
    data_dir = tmp_path / "state"
    fixture = tmp_path / "bank-rate.csv"
    fixture.write_bytes(b"DATE,IUDBEDR\n31 Jul 2026,3.75\n")
    ingested_status, _ = _invoke(
        capsys,
        "--data-dir",
        str(data_dir),
        "ingest",
        "bank-rate",
        "--fixture",
        str(fixture),
    )
    status, output = _invoke(
        capsys,
        "--data-dir",
        str(data_dir),
        "projections",
        "publish",
        "--output-directory",
        str(tmp_path / "published"),
        "--as-of",
        "2099-01-01T00:00:00Z",
    )
    connection = connect_database(data_dir / "operational.sqlite3", read_only=True)
    try:
        output_count = connection.execute("SELECT count(*) FROM output_artifact").fetchone()[0]
    finally:
        connection.close()

    assert ingested_status == status == 0
    assert output["result"]["command"] == "projections.publish"  # type: ignore[index]
    assert output["result"]["canonical_only"] is True  # type: ignore[index]
    assert output_count == 5


def test_cli_enqueues_a_bounded_projection_system_job(tmp_path: Path, capsys: object) -> None:
    status, output = _invoke(
        capsys,
        "--data-dir",
        str(tmp_path / "state"),
        "projections",
        "enqueue",
        "--output-directory",
        str(tmp_path / "published"),
        "--as-of",
        "2026-08-01T00:00:00Z",
    )

    assert status == 0
    assert output["result"]["command"] == "projections.enqueue"  # type: ignore[index]
    assert output["result"]["state"] == "queued"  # type: ignore[index]
