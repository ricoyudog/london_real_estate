# Datasource Operations Runbook

This runbook describes the currently executable datasource service, rather
than the intended end state.  Bank Rate, the nine fixed ONS series, the two
fixed London Nomis datasets, and the three dynamic official file-release
workflows (VOA stock, ONS hybrid working, and MHCLG EPC Table A) are bound
end-to-end production workflows.  Their live path is fixed request → streaming
CAS → evidence → isolated parser → canonical promotion.  ONSPD is also bound,
but is deliberately an on-demand one-postcode workflow: it needs an explicit
operator-approved retention deadline before it can capture composite geography
evidence.  It has no scheduled full-directory download.

ONS/Nomis are current-vintage contracts, and the three file-release workflows
only accept their current dynamically discovered edition.  Bounded historical
backfill is explicitly rejected until a separately approved historical endpoint
and record-window policy exist.  Registry entries for other sources are
intentionally visible as legacy adapters, manual-review workflows, or blocked
work; they must not be presented as production ingestion coverage.

`cre` writes exactly one versioned JSON document to stdout per invocation.
Treat its exit status and JSON `ok` field as the automation contract; keep
diagnostic logs on stderr or in the calling service.

## Install and configure a clean host

Build the wheel on the release host, transfer the wheel, and install it into a
Python 3.11+ environment on the operator host:

```sh
uv build
python3.11 -m venv /opt/cre/venv
/opt/cre/venv/bin/python -m pip install dist/nan_fung-0.1.0-py3-none-any.whl
```

The isolated parser boundary currently requires macOS `sandbox-exec`.  A
non-macOS host can install the wheel and run read-only/bootstrap checks, but
`health` reports `operational_ingestion.ready: false` and ingestion must not be
enabled there.  On an operator host, confirm that `parser_isolation.available`
and `operational_ingestion.ready` are both true before enabling a daemon.

Use an explicit private configuration in production.  The configuration file
must be mode `0600`; the program rejects group- or world-readable files.

```toml
[runtime]
data_dir = "/var/lib/cre"
backup_dir = "/var/backups/cre"
cursor_secret_file = "/etc/cre/cursor-hmac.key"
environment = "production"
instance_id = "cre-a"
operator_role = "admin"
timezone = "Europe/London"
```

The effective precedence is CLI values, the applicable `CRE_CONFIG`,
`CRE_DATA_DIR`, `CRE_BACKUP_DIR`, `CRE_CURSOR_SECRET_FILE`, `CRE_ENVIRONMENT`,
or `CRE_INSTANCE_ID` environment value, the TOML file, then development
defaults.  `operator_role` is intentionally read only from the private TOML;
it has no environment or command-line override.  Production requires
`data_dir`, `backup_dir`, `operator_role`, and a private 32--4096 byte
cursor-HMAC secret file.  The state directory contains
`operational.sqlite3`, immutable evidence under `evidence/`, and `writer.lock`;
give it to one service account and do not share it with another writer process.

`read` can run inspection and bounded canonical-read commands; `write` also
can enqueue, operate jobs, run the scheduler/daemon, import evidence, review,
and publish projections; `admin` also can migrate, sync/approve the registry,
and create or restore backups.  Roles are hierarchical and belong to separate
host-managed private configuration files or service accounts.  `cre` is a
local operator surface, not the agent boundary: agents receive the typed
`ReadContext` and bounded refresh contracts instead.

Bootstrap and confirm the installed wheel:

```sh
/opt/cre/venv/bin/cre --config /etc/cre/cre.toml db migrate
/opt/cre/venv/bin/cre --config /etc/cre/cre.toml health
```

For an ephemeral or development state directory, pass `--data-dir` on every
command instead of relying on the default `data/` directory:

```sh
cre --data-dir /srv/cre/state db migrate
cre --data-dir /srv/cre/state health
```

## Bootstrap, registry, and health

`db migrate` applies numbered migrations and synchronizes the packaged,
versioned registry.  Run it before a new deployment serves work.  When an
existing database has pending migrations, the command first creates and
verifies a complete SQLite-plus-referenced-CAS backup beneath `backup_dir`; it
does not alter the source database if that backup cannot be made.

```sh
cre --config /etc/cre/cre.toml db migrate
cre --config /etc/cre/cre.toml registry diff
cre --config /etc/cre/cre.toml datasource status
cre --config /etc/cre/cre.toml health
cre --config /etc/cre/cre.toml metrics
```

`datasource status` is the registry-status command; `datasource list` returns
the same current status data.  Inspect `runtime_ready`, missing bindings,
definition status, and policy fields before enqueueing work.  `registry sync`
is a mutating re-sync operation; use it only while no other writer is active.
`health` separately reports parser-isolation availability and whether this host
can execute bound production ingestion; a database-integrity `ready` state is
not enough to enable collectors.

```sh
cre --config /etc/cre/cre.toml registry sync
```

Definitions are immutable.  `registry approve` deliberately returns
`IMMUTABLE_REGISTRY_REQUIRES_NEW_VERSION`; it is not an operator override for
licence, retention, or automation approval.

## Jobs, schedule materialization, and acquisition

Enqueue creates a durable job but does not fetch anything.  All timestamps
passed to `--scheduled-for`, `--from`, `--to`, or `--at` must be ISO-8601 with
a timezone.

```sh
cre --config /etc/cre/cre.toml ingest enqueue boe.bank_rate.iudbedr \
  --request '{"series":"IUDBEDR"}'
cre --config /etc/cre/cre.toml backfill boe.bank_rate.iudbedr \
  --from 2026-07-01T00:00:00Z --to 2026-07-31T23:59:59Z
cre --config /etc/cre/cre.toml jobs list
cre --config /etc/cre/cre.toml jobs get JOB_ID
```

Use a scheduler tick to materialize due schedules and recover expired leases.
It does not perform network acquisition.

```sh
cre --config /etc/cre/cre.toml scheduler tick
```

`daemon once` is deliberately finite: it materializes schedules, claims at
most one due job, and executes it.  It refuses to run until the operator gives
the explicit network gate on that invocation.

```sh
cre --config /etc/cre/cre.toml daemon once --allow-network
```

Invoke that bounded command from the host scheduler at the chosen cadence.  A
direct `ingest bank-rate --live` call is rejected; live Bank Rate, ONS,
Nomis, VOA, ONS hybrid working, and EPC acquisition must use the daemon so the
fixed source contract, host allowlist, redirect checks, durable throttle, CAS
persistence, and job lifecycle apply.  A 429 response honors `Retry-After`
and places the job in retry wait without a busy sleep.

For a resident operator service, use the same explicit network gate.  Each
tick completes its claimed attempt before the next shutdown check; `SIGINT` or
`SIGTERM` stops the loop between ticks and records a `stopping` heartbeat.

```sh
cre --config /etc/cre/cre.toml daemon run --allow-network \
  --poll-interval-seconds 30
```

ONSPD is never a broad/scheduled collector.  An operator may enqueue exactly
one postcode, then supply the retention decision at the trusted daemon boundary:

```sh
cre --config /etc/cre/cre.toml ingest enqueue ons.onspd.postcode \
  --request '{"postcode":"EC2Y 5AS"}'
cre --config /etc/cre/cre.toml daemon once --allow-network \
  --onspd-retention-until 2026-10-30T23:59:59Z
```

Agent-facing refresh requests use a trusted profile that requires exactly one
`postcode` scope value; they cannot select a URL, layer, output SRID, lane, or
retention deadline.  A missing deadline closes the job as
`RETENTION_APPROVAL_REQUIRED` before network or CAS activity.

### Competition ONSPD tool budget

For the competition host, `request_refresh_v1` for `ons.onspd.postcode` has a
durable global budget of **20 newly queued agent refresh jobs per
`Europe/London` calendar day**. It is enforced by SQLite, so a daemon or broker
restart cannot reset it. Status reads, idempotent replays, and cooldown
deduplications do not create another job and therefore do not consume budget.

The 21st new request returns `confirmation_required` with a confirmation code
that expires in 10 minutes. The host must make a second call using the same
principal, `request_instance_id`, postcode scope, and confirmation code before
the job is queued. The trusted host may use the fixed principal
`competition-agent`; no staff or role directory is required. This restriction
applies to the agent-facing refresh tool, not to local operator commands,
which must never be exposed to the competition agent.

`2026-10-30T23:59:59Z` is the proposed 90-day ONSPD retention deadline for the
competition. It is not an activated permission: confirm the exact timestamp
before passing it to a live daemon. Preserve the current ONS / OS / Royal Mail
attribution and reuse conditions described in the [ONS Geography licences](https://www.ons.gov.uk/methodology/geography/licences).

For an offline fixture drill, the direct Bank Rate command is allowed and has
the same ingestion lifecycle after acquisition:

```sh
cre --config /etc/cre/cre.toml ingest bank-rate \
  --fixture /srv/cre/fixtures/bank-rate.csv
```

The offline reparse command is bound to Bank Rate, the fixed ONS/Nomis
workflows, the three official file releases, and ONSPD.  It reads saved CAS
evidence only; it does not perform network I/O.  For a file release use the
primary release evidence ID; for ONSPD use the postcode-query evidence ID so
the replay can load its companion layer metadata.  Obtain the `evidence_id`
from the preceding JSON result or from an approved operational record.

```sh
cre --config /etc/cre/cre.toml ingest reparse boe.bank_rate.iudbedr \
  --evidence-id EVIDENCE_ID
cre --config /etc/cre/cre.toml ingest reparse ons.gdp.ecyx \
  --evidence-id EVIDENCE_ID
cre --config /etc/cre/cre.toml ingest reparse nomis.nm_59_1.london_lfs \
  --evidence-id EVIDENCE_ID
cre --config /etc/cre/cre.toml ingest reparse voa.ndr_office_stock \
  --evidence-id EVIDENCE_ID
cre --config /etc/cre/cre.toml ingest reparse ons.onspd.postcode \
  --evidence-id POSTCODE_QUERY_EVIDENCE_ID
```

Retry only a terminal failed, dead-letter, or cancelled job.  Cancellation
only affects queued, retry-wait, or claimed work; it cannot cancel an already
running attempt.

```sh
cre --config /etc/cre/cre.toml jobs retry JOB_ID
cre --config /etc/cre/cre.toml jobs cancel JOB_ID
```

## Manual evidence and promotion

Manual/report sources must use the evidence-review route, never a scraper or
an arbitrary CAS write.  Supply the source-specific media type, provenance
information where applicable, and the approved retention deadline when the
source policy requires one.

```sh
cre --config /etc/cre/cre.toml evidence import \
  bnp.central_london_office_report /srv/cre/inbox/report.pdf \
  --media-type application/pdf \
  --source-url https://www.realestate.bnpparibas.co.uk/report.pdf \
  --attestation 'terms reviewed' \
  --retention-until 2030-01-01T00:00:00Z
cre --config /etc/cre/cre.toml review list --state open
cre --config /etc/cre/cre.toml review decide REVIEW_ID approved \
  --reason 'licence and provenance checked'
cre --config /etc/cre/cre.toml review promote REVIEW_ID \
  --reason 'operator promotion after review'
```

The internal submarket mapping is the only manual source that creates a
canonical observation after review.  It must be a bounded JSON mapping with a
non-empty attestation; BNP and Rightmove imports remain evidence-only.

```sh
cre --config /etc/cre/cre.toml evidence import \
  custom.london_office_submarkets /srv/cre/inbox/submarkets.json \
  --media-type application/json \
  --attestation 'mapping checked against approved rules'
```

An approved review does not promote data by itself.  Promotion is a second,
audited, idempotent action and remains subject to the datasource definition's
production status and promotion policy.  A rejected review must remain
rejected; do not re-import the same item to bypass the decision.

## Read and rebuild derived data

Read canonical projections through the bounded operator query.  Use the
keyset `cursor` returned in the JSON response for the next page.

```sh
cre --config /etc/cre/cre.toml observations latest --kind metrics --limit 100
cre --config /etc/cre/cre.toml observations as-of \
  --kind metrics --as-of 2026-08-01T00:00:00Z
cre --config /etc/cre/cre.toml projections rebuild
```

`projections rebuild` rewrites only deterministic SQLite projections under the
writer lease.  `projections publish` performs the canonical-only delivery
pipeline and records durable output lineage: `wiki/market.md`, daily and
weekly JSON snapshots, deterministic alerts, and an audit manifest below the
operator-supplied output directory.  The source wiki remains read-only; choose
a generated output directory owned by the publisher.

```sh
cre --config /etc/cre/cre.toml projections publish \
  --output-directory /var/lib/cre/published \
  --as-of 2026-08-01T00:00:00Z
```

To deliver through the durable worker rather than a foreground command, enqueue
the bounded system job and let `daemon once` or `daemon run` claim it:

```sh
cre --config /etc/cre/cre.toml projections enqueue \
  --output-directory /var/lib/cre/published \
  --as-of 2026-08-01T00:00:00Z
```

The package intentionally does not install a cron framework.  A trusted host
timer must enqueue this fixed job at the chosen daily cadence with an explicit
UTC `--as-of` timestamp; each successful delivery writes both that daily file
and its corresponding ISO-weekly snapshot.  Retain the enqueue and worker JSON
records with the operational log so a missed timer can be replayed from the
same canonical anchor.

## Integrity, retention, and evidence drift

Run these checks before and after maintenance, a restore, or an unexpected
daemon failure:

```sh
cre --config /etc/cre/cre.toml db integrity
cre --config /etc/cre/cre.toml evidence verify
cre --config /etc/cre/cre.toml retention dry-run \
  --as-of 2026-08-01T00:00:00Z
```

`evidence verify` checks every database-referenced CAS object and reports
`unreferenced` objects left by a failed publication window.  Preserve and
investigate those objects; this CLI intentionally exposes no CAS-delete or
orphan-reconciliation command.  `retention dry-run` is read-only and lists
eligible evidence only—it never deletes evidence.

## Backup and restore drill

Create each backup in a previously nonexistent directory.  The set contains a
SQLite backup, each referenced CAS object, and a verified manifest.

```sh
cre --config /etc/cre/cre.toml backup create /var/backups/cre/2026-08-01
cre --config /etc/cre/cre.toml backup verify /var/backups/cre/2026-08-01
cre --config /etc/cre/cre.toml backup restore \
  /var/backups/cre/2026-08-01 /srv/cre/restore-drill-2026-08-01
cre --data-dir /srv/cre/restore-drill-2026-08-01 db integrity
cre --data-dir /srv/cre/restore-drill-2026-08-01 evidence verify
```

Restore also requires a previously nonexistent target and never overwrites a
live state directory.  Verify the restored target before deliberately making
it active; do not point a running daemon at it during the drill.

## Lease recovery and orderly operation

Only one mutation process may use a data directory.  A concurrent daemon,
CLI mutation, backup, or projection rebuild fails rather than racing the
writer; let the active process finish and release `writer.lock` instead of
removing that file.

For an orderly stop of `cre daemon run`, send `SIGINT` or `SIGTERM` and wait for
its final JSON result.  The daemon stops between bounded ticks rather than
abandoning the current attempt.  If a host dies mid-run, do not force a stale
process to continue: a subsequent `scheduler tick`, `daemon once`, or resident
daemon recovers an expired claim, clears its token, and moves it to bounded
retry or dead letter.  Inspect `jobs get JOB_ID` and `health` before a manual
retry.

## Source-policy boundary

The service is fail-closed by design:

- Bank Rate, the nine fixed ONS series, the two fixed London Nomis datasets,
  and the dynamic VOA/ONS-hybrid/MHCLG-EPC releases are bound for live
  operational ingestion.
- ONSPD is bound only for one-postcode, retention-approved, on-demand work.
  It never claims a complete postcode directory or location snapshot.
- Other registered definitions may have legacy fetch adapters, need manual
  review, or are policy blocked.  A scheduled unbound datasource is reported
  as blocked, and an explicitly queued one is closed with `WORKER_UNBOUND`.
- Discovery and ad-hoc lanes are not a route to canonical production data.
  Do not use them to bypass source approval, retention, or promotion policy.
- Keep PLD, BNP, Rightmove, MPC/GOV.UK content, and any other source with an
  unapproved policy in its declared blocked or manual workflow until a new,
  approved registry definition and bound lifecycle are delivered.

## Test gates

The default test command is the offline gate; `pyproject.toml` excludes every
`network` marker by default.  `live` is reserved for an approved operational
evidence-to-promotion smoke workflow.  Legacy direct-fetch probes and
policy-restricted probes have their own markers and must never be reported as
production workflow coverage.

```sh
uv run pytest
uv build
```

Use [`datasource-acceptance.md`](datasource-acceptance.md) for the TC-01 to
TC-10 release matrix.  A fixture-backed engineering pass does not turn a
policy-blocked source into product coverage.

Run live smoke tests separately, only from an approved network environment and
only after the offline gate passes:

```sh
uv run pytest -m live
```

For a recorded smoke run, invoke the daemon against a dedicated state directory
and retain its JSON result (including run and evidence IDs) with the release
record.  The pytest smoke uses a temporary directory and is only a transport
and lifecycle check.

For a wheel-install drill, repeat `db migrate` and `health` using the installed
wheel and a new temporary data directory.  Keep this check separate from an
operator's live state directory.
