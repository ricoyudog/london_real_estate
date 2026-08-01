---
type: wiki
updated: 2026-08-01
status: accepted
source: "[[wiki/architecture/datasource|Datasource Persistence Architecture: Observation + Evidence Store]]"
---

# Datasource Operational Implementation Status — 2026-08-01

## Decision

Datasource operational foundation 已完成工程交付：正式 ingestion 必經固定
request、streaming CAS evidence、isolated parser、SQLite observation revision 和
canonical promotion。這不等於所有市場資料均已取得 licence、retention 或
automation approval；產品 coverage 仍以 source policy fail-closed。

## Delivered

- Versioned registry、forward-only SQLite migrations、immutable CAS evidence、run /
  evidence / observation / promotion audit lineage。
- Durable job scheduler、lease / heartbeat / retry / dead-letter、backfill and
  reparse controls，以及 `cre` operator CLI。
- Canonical latest/as-of reads、bounded refresh broker、deterministic projection /
  snapshot / alert delivery、backup and restore controls。
- 正式 automatic paths：Bank Rate、9 個 ONS series、2 個 Nomis London datasets、
  VOA office stock、ONS hybrid-working proxy 和 MHCLG EPC Table A proxy。
- Policy-gated path：ONSPD 只允許 one-postcode、on-demand、explicit-retention
  workflow。competition agent tool 以 Europe/London 日曆日計算，最多建立 20 個
  新 refresh job；第 21 個必須以同一 request identity 回傳的確認碼再提交一次。
  PLD、restricted GOV.UK/MPC content、BNP、Rightmove 和 GLA 仍維持 blocked 或
  manual-review。

## Recorded verification

所有實際資料驗證均使用全新的 temporary data directory，而非 fixture。每次均
檢查 CAS hash、SQLite run/observation、`canonical_latest_v1` 和 promotion。

| Source group | Actual persisted result |
| --- | --- |
| Bank Rate | 399 accepted records、1 evidence、automatic promotion approved |
| 9 ONS + 2 Nomis contracts | 11/11 succeeded，合共 4,406 accepted records，0 rejected |
| VOA office stock | 1 accepted record、3-evidence collection → release-page → ZIP lineage |
| ONS hybrid working | 160 accepted records、2 evidence |
| MHCLG EPC Table A | 71 accepted records、2 evidence；latest `2026/2` has 3,630 lodgements |
| ONSPD one-postcode | `EC2Y 5AS`：1 accepted record、2 immutable ArcGIS evidence、automatic promotion approved；both evidence rows retain until `2026-08-31T00:00:00Z` |

The final local gates passed: `uv run pytest -q` (`291 passed, 15 deselected`),
`uv run pytest -q -m live` (`1 passed, 304 deselected`), `uv build`, and clean
wheel install followed by `cre db migrate` and `cre health`.

## ONSPD retention approval

### Why the gate exists

`ons.onspd.postcode` is technically bound, but its registry policy is
`composite_geodata` with ONS/OS/Royal Mail attribution. A lookup writes two
immutable raw ArcGIS artifacts (layer metadata and postcode query) plus a
normalized geography observation. Because evidence cannot be silently
overwritten or forgotten, the service requires an explicit retention deadline
before it starts network or CAS activity.

This is an internal data-governance approval, not an API key or a technical
switch that itself grants licence rights. ONSPD postcode products have their
own ONS / OS / Royal Mail attribution and reuse conditions; the project must
continue to follow the current [ONS Geography licences](https://www.ons.gov.uk/methodology/geography/licences)
and [ONS postcode-products guidance](https://www.ons.gov.uk/methodology/geography/geographicalproducts/postcodeproducts).

### Competition decision recorded

The requester made the following competition-mode decision on 2026-08-01:

1. **Scope:** one postcode per on-demand `ons.onspd.postcode` request only;
   no postcode-directory snapshot or scheduled collection.
2. **Tool budget:** at most 20 newly queued agent refresh jobs per
   `Europe/London` calendar day. Status reads, idempotent replays and cooldown
   deduplications do not consume an additional network-bound job budget.
3. **Over-budget action:** the 21st new job returns
   `confirmation_required` and a request-bound confirmation code valid for 10
   minutes. The same principal, request ID and bounded postcode request must
   submit that code in a second call before a job is queued. The SQLite record
   and audit event survive broker/process restart.
4. **No personnel system:** the trusted host can use one fixed principal such
   as `competition-agent`; audit ownership is the competition project rather
   than a staff directory.
5. **Licence-condition acknowledgement:** the requester approved retaining the
   required ONS / OS / Royal Mail attribution and not treating this tool as a
   way to bypass source redistribution conditions. This records project intent;
   the upstream terms remain authoritative.

### Approved competition retention deadline

The requester approved a retention period of no more than 30 days. The trusted
daemon used `2026-08-31T00:00:00Z`, which is safely within 30 days of the
2026-08-01 decision time. This keeps enough evidence for a competition demo,
reparse and restore drill without creating a long-lived geography archive.

On 2026-08-01 the live run `run_89abe0721f0f42cb814fe276229197ee` fetched the
official ONSPD layer metadata and the exact `EC2Y 5AS` query into two CAS
artifacts (`ev_2b6b5a0a8bda4049ac94dd8f93137506` and
`ev_07c7dc01ea2f48d0b7ed422e22fc3bcc`). It persisted and automatically promoted
`obs_fa76edeb75824066b116234c60fac5ca`; a canonical as-of read at
`2026-08-01T10:24:08Z` returned the City of London geography record. Integrity
and evidence hash verification both passed.

Any later live ONSPD run must still be supplied a future deadline. Before this
deadline expires, renew it with a new explicit project decision or stop live
capture and let the evidence enter the existing retention-review process.

### Minimum approval record

The competition project should record and approve all of the following:

1. **Scope:** `ons.onspd.postcode`, exactly one postcode per on-demand job;
   no scheduled bulk directory, postcode snapshot, or arbitrary ArcGIS query.
2. **Purpose and audience:** internal geography lookup purpose and the users /
   systems permitted to read the resulting evidence and observation.
3. **Retention deadline:** one future UTC ISO-8601 deadline, after which the
   evidence becomes eligible for retention review/deletion procedure.
4. **Licence conditions:** required ONS/OS/Royal Mail attribution and any
   redistribution restriction.
5. **Owner and review date:** for this competition, `competition-project` is
   sufficient; record the approval date and a review date without creating a
   personnel directory.

Suggested approval text:

```text
Approve ONSPD one-postcode operational evidence retention
Scope: ons.onspd.postcode; one postcode per on-demand request only
Purpose: competition London-office geography lookup
Audience: competition-agent and its trusted operator host only
Retention until: 2026-08-31T00:00:00Z
Attribution / redistribution conditions: preserve ONS / OS / Royal Mail terms
Approver: competition-project; approval date: 2026-08-01; review owner: competition-project
```

### Applying an approved deadline

After the approval exists, a trusted operator (not an agent refresh request)
passes the approved deadline to the daemon:

```sh
cre --config /etc/cre/cre.toml ingest enqueue ons.onspd.postcode \
  --request '{"postcode":"EC2Y 5AS"}'
cre --config /etc/cre/cre.toml daemon once --allow-network \
  --onspd-retention-until 2026-08-31T00:00:00Z
```

The daemon rejects a missing deadline with `RETENTION_APPROVAL_REQUIRED`
before any network or CAS write. A supplied deadline must be timezone-aware
and after the evidence retrieval time; it is stored as
`evidence_artifact.retention_until`. `cre retention dry-run` later lists
eligible evidence without deleting it. Agent-facing refresh contracts cannot
choose the deadline, URL, layer, lane or output geometry. The documented
deadline is approved for the competition; do not extend it without a new
project decision.

## References

- [[wiki/architecture/datasource|Datasource Persistence Architecture]]
- [[wiki/research/datasource/13-submarket-geography|子市場地理對照 Data Sources]]
- [`docs/datasource-operations.md`](../../docs/datasource-operations.md)
- [`docs/datasource-acceptance.md`](../../docs/datasource-acceptance.md)
