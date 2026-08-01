"""Trusted local adapter from the agent-safe refresh broker to SQLite jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from nan_fung.operational import (
    OperationalStore,
    RefreshRequestAccessError,
    RefreshRequestReplayError,
)

from .contracts import (
    REFRESH_SCHEMA_VERSION,
    BackendSubmitResult,
    InvalidRefreshRequest,
    RefreshBackend,
    RefreshAccessDenied,
    RefreshDisposition,
    RefreshStatus,
    RefreshSubmission,
)


class OperationalRefreshBackend(RefreshBackend):
    """Enqueue fixed profile requests without exposing a collector or SQL API."""

    def __init__(self, store: OperationalStore) -> None:
        self._store = store

    def submit(self, submission: RefreshSubmission) -> BackendSubmitResult:
        definition = self._store.registry.lookup(
            submission.datasource_id, submission.definition_version
        )
        if submission.promotion_policy not in {
            "registry_selected",
            definition.promotion_policy,
        }:
            raise InvalidRefreshRequest(
                "refresh profile promotion policy does not match its definition"
            )
        try:
            result = self._store.submit_agent_refresh(
                request_id=submission.request_id,
                principal=submission.principal,
                request_fingerprint=submission.request_fingerprint,
                dedupe_key=submission.dedupe_key,
                datasource_id=submission.datasource_id,
                definition_version=submission.definition_version,
                request_profile=submission.request_profile,
                lane=submission.effective_lane,
                bounded_scope=submission.bounded_scope,
                intent=submission.intent,
                submitted_at=submission.submitted_at,
                cooldown_until=submission.cooldown_until,
            )
        except RefreshRequestAccessError as error:
            raise RefreshAccessDenied(str(error)) from error
        except RefreshRequestReplayError as error:
            raise InvalidRefreshRequest(str(error)) from error
        disposition = RefreshDisposition(result.disposition)
        return BackendSubmitResult(
            disposition,
            result.job_id,
            result.initial_state,
            submitted_at=result.submitted_at,
        )

    def get_status(
        self,
        job_id: str,
        *,
        principal: str,
        wait_deadline: datetime | None = None,
    ) -> RefreshStatus | None:
        # A status read intentionally does not hold a transaction while waiting.
        try:
            job = self._store.get_agent_refresh_job(job_id, principal=principal)
        except RefreshRequestAccessError as error:
            raise RefreshAccessDenied(str(error)) from error
        if job is None:
            return None
        attempts = job["attempts"]
        latest_attempt = attempts[-1] if attempts else None
        run = job["run"]
        promotions = job["promotions"]
        error = job["last_error"]
        return RefreshStatus(
            schema_version=REFRESH_SCHEMA_VERSION,
            job_id=job_id,
            job_state=str(job["state"]),
            latest_attempt_status=(
                str(latest_attempt["status"]) if latest_attempt is not None else None
            ),
            terminal_run_id=str(run["run_id"]) if run and job["completed_at"] else None,
            terminal_error=error if isinstance(error, Mapping) else None,
            promotion_status=(
                str(promotions[-1]["decision"]) if promotions else None
            ),
            canonical_changed=bool(promotions) if promotions else None,
            result_ref=(
                str(run["run_id"])
                if run
                and job["lane"] in {"source_discovery", "ad_hoc_research"}
                and job["state"] in {"succeeded", "empty", "failed", "dead_letter"}
                else None
            ),
        )
