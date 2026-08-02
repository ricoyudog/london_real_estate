## OVERVIEW

`nan-fung-agent-tools` is a one-shot subprocess contract: one tool selector and one JSON request in, one JSON result out. Python code enforces the boundary; packaged JSON defines its wire surface and product authority.

## STRUCTURE

| File | Role |
|---|---|
| `cli.py` | Console entrypoint, `FacadeExecutor` protocol, `run_cli`; validates one selector, one request, one result. |
| `facade.py` | `AgentToolFacade` dispatch and selector sets: `MODEL_TOOL_NAMES`, `HOST_TOOL_NAMES`. |
| `host.py` | Trusted `AgentToolHost`: fixed child argv, FD 3 setup, timeout, I/O bounds, process cleanup. |
| `handles.py` | HMAC-scoped handles; `load_handle_secret_from_fd()` consumes and closes FD 3. |
| `manifest.py` | Capability and refresh-profile models/catalogs; validates and loads product manifests. |
| `protocol.py` | Stable errors, bounded JSON I/O, duplicate-key rejection, request/result validation. |
| `tool_contracts.py` | Immutable selector-contract catalog; checks facade selector sets and contract policies. |
| `agent_tool_contracts.v1.json` | Selector wire catalog: audience, refresh identity policy, argument and success schemas. |
| `agent_tool_request.v1.schema.json` | Request envelope schema: request ID, arguments, trusted host context. |
| `agent_tool_result.v1.schema.json` | Result envelope schema: status, data, warnings, fixed safe errors. |
| `agent_tool_contract_catalog.v1.schema.json` | Schema for the six-entry selector catalog. |
| `capabilities.v1.json` | Product capability authority: status, query templates, data scope, refresh profiles. |
| `refresh-profiles.v1.json` | Approved refresh profiles: datasource, scope limits, lane, promotion, polling. |

## SELECTOR SURFACE

| Tool selector | Audience | What it does |
|---|---|---|
| `describe_market_data` | model | Lists allowed product capabilities and availability. |
| `query_market_data` | model | Runs an allowed, bounded market-data query. |
| `get_citation_metadata` | model | Resolves citation references to source metadata. |
| `request_data_refresh` | model | Requests an approved bounded refresh; requires `refresh_request_id`. |
| `get_refresh_status` | model | Returns refresh job state and promotion outcome. |
| `approve_refresh` | host | Approves or denies a pending refresh request. |

## WHERE TO LOOK

| Task | Location |
|---|---|
| Add a tool | `facade.py`, `agent_tool_contracts.v1.json`, `tool_contracts.py`, `cli.py`, `host.py` |
| Change wire schema | `agent_tool_request.v1.schema.json`, `agent_tool_result.v1.schema.json`, `protocol.py` |
| Change refresh profile | `refresh-profiles.v1.json`, `manifest.py`, `cli.py` |
| Change capability manifest | `capabilities.v1.json`, `manifest.py`, `facade.py` |
| Change subprocess timeout | `host.py` (`AgentToolHost.timeout_seconds`) |

## ANTI-PATTERNS (THIS DIRECTORY)

- Do NOT reuse `cre`'s argparse. This protocol intentionally has its own one-selector parser.
- Do NOT pass the handle secret by argv or environment. FD 3 only.
- Do NOT render exception messages into results. `safe_message` is fixed.
- Do NOT call `json.loads` for protocol input without duplicate-key rejection.
- Do NOT modify packaged `.json` without bumping `schema_version` and cross-checking its Python loader.
