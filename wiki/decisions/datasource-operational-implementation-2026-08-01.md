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
  workflow；PLD、restricted GOV.UK/MPC content、BNP、Rightmove 和 GLA 仍維持
  blocked 或 manual-review。

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

The final local gates passed: `uv run pytest -q` (`290 passed, 15 deselected`),
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
switch that itself grants licence rights. No such approval is committed in this
branch, so ONSPD was deliberately not included in the live persistence drill.

### Minimum approval record

The data-governance owner should record and approve all of the following:

1. **Scope:** `ons.onspd.postcode`, exactly one postcode per on-demand job;
   no scheduled bulk directory, postcode snapshot, or arbitrary ArcGIS query.
2. **Purpose and audience:** internal geography lookup purpose and the users /
   systems permitted to read the resulting evidence and observation.
3. **Retention deadline:** one future UTC ISO-8601 deadline, after which the
   evidence becomes eligible for retention review/deletion procedure.
4. **Licence conditions:** required ONS/OS/Royal Mail attribution and any
   redistribution restriction.
5. **Owner and review date:** the approver, approval date and renewal/review
   responsibility.

Suggested approval text:

```text
Approve ONSPD one-postcode operational evidence retention
Scope: ons.onspd.postcode; one postcode per on-demand request only
Purpose: internal London-office geography lookup
Audience: <approved internal roles/systems>
Retention until: <YYYY-MM-DDTHH:MM:SSZ>
Attribution / redistribution conditions: <record approved conditions>
Approver: <name and role>; approval date: <date>; review owner: <name>
```

### Applying an approved deadline

After the approval exists, a trusted operator (not an agent refresh request)
passes the approved deadline to the daemon:

```sh
cre --config /etc/cre/cre.toml ingest enqueue ons.onspd.postcode \
  --request '{"postcode":"EC2Y 5AS"}'
cre --config /etc/cre/cre.toml daemon once --allow-network \
  --onspd-retention-until 2027-08-01T00:00:00Z
```

The daemon rejects a missing deadline with `RETENTION_APPROVAL_REQUIRED`
before any network or CAS write. A supplied deadline must be timezone-aware
and after the evidence retrieval time; it is stored as
`evidence_artifact.retention_until`. `cre retention dry-run` later lists
eligible evidence without deleting it. Agent-facing refresh contracts cannot
choose the deadline, URL, layer, lane or output geometry.

## References

- [[wiki/architecture/datasource|Datasource Persistence Architecture]]
- [[wiki/research/datasource/13-submarket-geography|子市場地理對照 Data Sources]]
- [`docs/datasource-operations.md`](../../docs/datasource-operations.md)
- [`docs/datasource-acceptance.md`](../../docs/datasource-acceptance.md)
