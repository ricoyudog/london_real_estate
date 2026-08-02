## OVERVIEW

Pytest suite: 47 modules and about 396 tests. Offline by default, integration-heavy, with real temporary SQLite and CAS state.

## STRUCTURE

- Agent tools: `test_agent_tool_contracts.py`, `test_agent_tool_capabilities.py`, `test_agent_tool_host_scopes.py`, `test_agent_tool_cli_dependencies.py`, `test_agent_tool_query_citations.py`, `test_agent_tool_refresh.py`, `test_agent_tool_protocol.py`, `test_agent_tool_data_plane.py`, `test_agent_tool_process.py`.
- Operational, storage, migrations, and backups: `test_operational_store.py`, `test_operational_controls.py`, `test_operational_schema.py`, `test_storage_db.py`, `test_storage_artifacts.py`, `test_migration_backup.py`, `test_backups.py`.
- Ingestion and lifecycles: `test_ingestion_core.py`, `test_bank_rate_lifecycle.py`, `test_file_release_lifecycle.py`, `test_official_macro.py`, `test_official_macro_workflow.py`, `test_official_macro_lifecycle.py`, `test_onspd_lifecycle.py`, `test_submarket_mapping.py`.
- Reads, refresh, and projections: `test_read_api_contracts.py`, `test_read_freshness.py`, `test_sqlite_read_pagination.py`, `test_refresh_api_contracts.py`, `test_projections_contracts.py`, `test_projection_rebuild.py`, `test_projection_delivery.py`.
- Datasource adapters: `test_macro.py`, `test_market.py`, `test_news.py`, `test_esg.py`, `test_planning.py`, `test_geography.py`, `test_hybrid.py`, `test_common.py`, `test_datasource_catalog.py`, `test_acquisition.py`.
- CLI and runtime boundaries: `test_cli.py`, `test_supervisor.py`, `test_parser_runner.py`, `test_host_throttle.py`, `test_config.py`, `test_pytest_markers.py`.

## SHARED FIXTURES

Only shared fixtures live in `fixtures/agent_tools/v1/`.

- `requests.json`: valid versioned tool requests.
- `invalid-requests.json`: malformed request envelopes and policy injection.
- `results.json`: valid `ok`, `partial`, and error result shapes.
- `invalid-results.json`: unsafe or unknown result fields.
- `result-envelope.json`: minimal serializable result envelope.
- `tool-contract-fixtures.json`: valid and invalid tool argument and response matrices.

## LOCAL CONVENTIONS

- No `conftest.py`. Keep setup in its owning module.
- Use small helpers named `_store`, `_seed_store`, `_fixture`, or `_invoke`.
- Standard fixtures: `tmp_path`, `monkeypatch`, and `capsys`.
- Close every explicit SQLite connection in `try`/`finally`.
- Use `pytest.mark.parametrize` for boundary and contract matrices.
- Mock offline seams at the module boundary with `monkeypatch.setattr`.

## MARKERS

| Marker | Meaning | When to use |
| --- | --- | --- |
| `network` | Requires network access. | Any test that calls an external service. |
| `live` | Approved live smoke test. | Opt-in checks against a real datasource. |
| `legacy_live_probe` | Historical live probe. | Preserve legacy external verification only. |
| `restricted_live_probe` | Live probe with tighter approval. | Restricted-source checks only. |

`test_pytest_markers.py` locks the default exclusion of `live` and `network`.

## RUNNING

```bash
uv run pytest
uv run pytest -m live
uv run pytest tests/test_operational_store.py
```

## ANTI-PATTERNS (THIS DIRECTORY)

- Do not add `conftest.py`.
- Do not add a shared fixture factory.
- Do not delete failing tests to pass. Current known failure: `test_submarket_mapping.py::test_approved_manual_mapping_becomes_a_canonical_geography_record`.
- Do not add real-network tests without a marker.
- Do not instantiate `OperationalStore` without `tmp_path`.
- Do not leave SQLite connections open.
