## OVERVIEW

Canonical JSON supports domain-separated record, evidence, and watermark identities. Parsers execute in a sandbox; domain workflows turn saved evidence into lifecycle records.

## STRUCTURE

| File | Role |
| --- | --- |
| `canonical.py` | Canonical JSON, deep freeze/thaw, normalized parsing, and domain-separated hashes. |
| `policies.py` | Source-policy checks plus URL, header, and recursive secret redaction. |
| `registry.py` | Versioned packaged datasource and source definition descriptors. |
| `jobs.py` | Durable job, claim, attempt, trigger, catchup, and state contracts. |
| `parser_runner.py` | `sandbox-exec` parser protocol, bounded I/O, and source verification. |
| `bank_rate.py` | Bank Rate CSV parsing, decimal validation, and ingestion records. |
| `official_macro_workflow.py` / `official_macro_lifecycle.py` | Fixed ONS, Nomis, and MPC acquisition contracts; evidence reparse. |
| `file_release_workflow.py` / `file_release_lifecycle.py` | Fixed VOA, ONS hybrid, and EPC file-release contracts; evidence reparse. |
| `onspd_lifecycle.py` | On-demand ONSPD postcode evidence capture and reparse. |
| `submarket_mapping.py` | Validated manual London office submarket mapping input. |

## WHERE TO LOOK

| Task | Location |
| --- | --- |
| Add a datasource workflow | Domain `*_workflow.py`, matching `*_lifecycle.py`, registry definitions, and supervisor binding. |
| Add a parser | Parser function plus `parser_runner.py` binding and limits. |
| Change canonical JSON hashing | `canonical.py`: normalization, serialization, and named domain helper. |
| Add a job state | `jobs.py`, then supervisor dispatch and `OperationalStore` transitions. |
| Change redaction rules | `policies.py`: `redact_secrets`, `redact_url`, `redact_headers`. |

## DOMAIN SEPARATION

| Hash function | Domain prefix | Used for |
| --- | --- | --- |
| `canonical_json` / `hash_canonical` | `nan-fung/<domain>/v1\0` | Deterministic JSON serialization and named-domain identities. |
| `record_key_hash` | `nan-fung/record-key/v1\0` | Datasource, key-version, and natural-key identity. |
| `observation_hash` | `nan-fung/observation/v1\0` | Immutable canonical observation envelope. |
| `source_hash` | `nan-fung/source/v1\0` | Source-definition identity. |
| `watermark_hash` | `nan-fung/watermark/v1\0` | Scheduler and ingestion watermark identity. |

## AUTOMATIC DATASOURCE BINDINGS

| Datasource ID set | Workflow module | Reparse function |
| --- | --- | --- |
| `BANK_RATE_DATASOURCE_ID` | `bank_rate.py` | `nan_fung.workflows.reparse_bank_rate_evidence` |
| `OFFICIAL_MACRO_AUTOMATIC_DATASOURCE_IDS` | `official_macro_workflow.py` | `reparse_official_macro_evidence` |
| `FILE_RELEASE_AUTOMATIC_DATASOURCE_IDS` | `file_release_workflow.py` | `reparse_file_release_evidence` |
| `ons.onspd.postcode` | `onspd_lifecycle.py` | `reparse_onspd_postcode_evidence` |

## ANTI-PATTERNS (THIS DIRECTORY)

- Do not hash JSON without a domain prefix.
- Do not call parsers in-process. Bind them through the `parser_runner.py` sandbox.
- Do not extend `canonical.py` with non-domain-separated hashes.
- Do not add a `JobState` without updating supervisor dispatch and `OperationalStore` transitions.
- Do not parse Bank Rate decimals with `float`. Use `_decimal_text` and `Decimal`.
