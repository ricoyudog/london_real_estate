# Datasource Acceptance Matrix

This is the datasource engineering acceptance record for the ten product test
cases in [`tests/Test case.md`](../tests/Test%20case.md).  It deliberately
separates a fixture-backed engineering result from a production-coverage
claim.  A passing engineering gate proves the immutable evidence, canonical
promotion, read, or projection contract named below; it does **not** assert
that all of the market data needed to answer the user question is licensed or
available.

## Status vocabulary

- **Engineering PASS** means the named local-fixture contract is covered by
  the cited test(s), including its canonical/discovery boundary where
  applicable.
- **Product BLOCKED** means the expected end-user answer must not be generated
  as if its required data were complete.  The named owner must supply the
  approval, source, or reporting capability before this can become a product
  pass.
- A `blocked` or `manual_review` registry state is intentional fail-closed
  behaviour, not a failed attempt to scrape the source.  The registry coverage
  check is [`tests/test_datasource_catalog.py`](../tests/test_datasource_catalog.py).

## TC-01 to TC-10

| Test case | Engineering status and available foundation | Product status / blocking evidence | Owner and closure evidence |
| --- | --- | --- | --- |
| TC-01 Prime rent lookup | **Engineering PASS:** restricted reports can be imported as immutable manual evidence and sent to review; a discovery-lane report cannot be promoted. [`tests/test_operational_controls.py`](../tests/test_operational_controls.py) | **Product BLOCKED:** no approved, production canonical prime-rent series exists. `bnp.central_london_office_report` is discovery/manual review with restricted retention, so it cannot be presented as a current City rent. [`src/nan_fung/ingestion/registry.py`](../src/nan_fung/ingestion/registry.py) | **Data-governance owner + market-data owner:** approve licence/retention and publish a new production definition with a bounded parser and fixture; then validate a City rent record, period, unit, and citation. |
| TC-02 Vacancy-rate comparison | **Engineering PASS:** canonical records expose source, freshness, and degraded state needed to warn about missing or stale inputs. [`tests/test_read_freshness.py`](../tests/test_read_freshness.py) | **Product BLOCKED:** neither City nor West End has an approved canonical vacancy-rate datasource; the BNP workflow is evidence/review only. | **Market-data owner:** provide approved comparable City and West End series, record-key and period-comparability rules. **Data-governance owner:** approve any report retention. |
| TC-03 Recent market news | **Engineering PASS:** the registry makes restricted MPC/GOV.UK discovery sources fail closed before canonical evidence/promotion. [`tests/test_official_macro_lifecycle.py`](../tests/test_official_macro_lifecycle.py), [`tests/test_datasource_catalog.py`](../tests/test_datasource_catalog.py) | **Product BLOCKED:** GOV.UK search/content are `blocked` pending access/review; MPC RSS is discovery-only and MPC content requires approval. No ranked production news feed exists. | **Data-governance owner:** approve access, retention, and permitted transformations. **News product owner:** deliver a source-specific canonical event workflow and relevance/dedup acceptance fixture. |
| TC-04 Future supply pipeline | **Engineering PASS:** production/discovery lane separation prevents unapproved planning evidence from becoming canonical. [`tests/test_supervisor.py`](../tests/test_supervisor.py), [`tests/test_datasource_catalog.py`](../tests/test_datasource_catalog.py) | **Product PARTIAL:** the `london-planning-activity` capability is now supported on Crown-copyright `planning.data.gov.uk` data, surfacing monthly planning-application counts per London authority. Project supply with proposed floorspace remains BLOCKED because no public source supplies that field; see `.omo/evidence/london-supply-unlock/task-1-pld-probe.md`. | **Market-data owner:** confirm whether planning-activity counts are an acceptable proxy for the original TC-04 intent or whether TC-04 must be re-scoped. **Data-governance owner:** no change — OGL v3 / Crown copyright confirmed. |
| TC-05 Interest-rate impact analysis | **Engineering PASS:** Bank Rate and fixed ONS/Nomis macro contracts persist evidence before isolated parsing, revision, promotion, and as-of reads. [`tests/test_bank_rate_lifecycle.py`](../tests/test_bank_rate_lifecycle.py), [`tests/test_official_macro_lifecycle.py`](../tests/test_official_macro_lifecycle.py), [`tests/test_read_freshness.py`](../tests/test_read_freshness.py) | **Product BLOCKED:** the macro facts foundation is available, but the required current rent and investment-transaction inputs are not approved canonical sources. The AI inference/report contract is also outside the datasource service. | **Market-data owner:** provide approved rent and transaction sources. **Agent/reporting owner:** implement fact/inference, confidence, and non-causation evaluation using the cited canonical records. |
| TC-06 Material-event alert | **Engineering PASS:** canonical-only projection delivery creates deterministic alert, daily/weekly, wiki, and audit outputs with durable output lineage. [`tests/test_projection_delivery.py`](../tests/test_projection_delivery.py) | **Product BLOCKED:** the alert delivery mechanism does not supply approved planning, transaction, lease, or news events. It must yield no alert/degraded coverage rather than invent an event. | **News and supply-data owners:** deliver approved canonical event feeds. **Alert product owner:** define materiality thresholds and evaluate triggered and explicit no-alert cases on those feeds. |
| TC-07 Vacancy anomaly detection | **Engineering PASS:** read-health reports stale/unknown observation and retrieval freshness so a downstream anomaly workflow can reject incomplete series. [`tests/test_read_freshness.py`](../tests/test_read_freshness.py) | **Product BLOCKED:** no approved multi-period Canary Wharf vacancy series or baseline exists, so no normal/anomalous classification is valid. | **Market-data owner:** license and bind the series, then add a versioned baseline/threshold contract and fixture including missing-period handling. |
| TC-08 Flight-to-quality and ESG demand | **Engineering PASS:** approved ONS hybrid-working and MHCLG EPC release workflows have fixed release contracts and offline reparse support. [`tests/test_file_release_lifecycle.py`](../tests/test_file_release_lifecycle.py), [`tests/test_esg.py`](../tests/test_esg.py), [`tests/test_hybrid.py`](../tests/test_hybrid.py) | **Product BLOCKED:** these are proxies; no approved Grade A/non-Grade A rent/vacancy differential, leasing-transaction, or tenant-statement evidence set is canonical. | **Market-data owner:** define Grade A taxonomy and license market/transaction data. **Data-governance owner:** approve qualitative-evidence retention. **Agent/reporting owner:** evaluate supported/partly supported/insufficient outcomes. |
| TC-09 Daily market brief | **Engineering PASS:** canonical data can be rebuilt into deterministic daily/weekly snapshots and a canonical-only market projection with audit manifest. [`tests/test_projection_delivery.py`](../tests/test_projection_delivery.py), [`tests/test_projection_rebuild.py`](../tests/test_projection_rebuild.py) | **Product BLOCKED:** a one-page brief and its machine-readable counterpart need current news, market metrics, supply, and transaction coverage, plus the downstream reporting component; the datasource output is not such a brief. | **Reporting owner:** add a versioned brief renderer/evaluator. **Data owners:** close the TC-01--TC-04 and TC-06 source gates, then test freshness labels and citations in the rendered output. |
| TC-10 Quarterly executive submarket report | **Engineering PASS:** an approved internal submarket mapping can be manually reviewed and promoted once as canonical geography; delivery keeps lineage deterministic. [`tests/test_submarket_mapping.py`](../tests/test_submarket_mapping.py), [`tests/test_projection_delivery.py`](../tests/test_projection_delivery.py) | **Product BLOCKED:** mapping alone does not provide the required City/West End/Canary Wharf rents, vacancy, leasing, supply, and transactions; no quarterly report generator is in the datasource service. | **Market-data owner:** close the comparable submarket series and coverage gaps. **Reporting owner:** create the executive-report renderer and fixture-backed source inventory, chart, and fact/inference checks. |

## Source-policy disposition

The engineering result is that every seeded datasource has an explicit
operational, legacy-adapter, manual-review, or blocked disposition.  The bound
production paths are Bank Rate; fixed ONS and Nomis macro series; VOA, ONS
hybrid-working, and MHCLG EPC releases; and a bounded, retention-gated ONSPD
postcode lookup. ONSPD requires a competition-project-approved deadline at the
trusted daemon boundary before any live capture; it does not require a
personnel directory. PLD, GOV.UK content/search, GLA
boundaries, and restricted
MPC content remain blocked; BNP and Rightmove remain manual/review workflows.
Those policy states are part of the acceptance result and must be preserved
until an approved, versioned replacement is delivered.

## Release decision

The datasource system may be released as an **engineering-complete, bounded
data foundation** after the offline and packaging gates pass.  It is not a
product-coverage release for any TC marked Product BLOCKED.  A future product
release must attach the named owner evidence to each blocked row, add the
source-specific fixtures and tests, and rerun the relevant TC end-to-end
without treating discovery, ad-hoc, or manual evidence as automatic coverage.
