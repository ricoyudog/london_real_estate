"""Deterministic, canonical-only Markdown renderer for ``wiki/market`` pages.

This module renders content only.  The workflow/outbox layer owns atomic file
writes, which keeps a discovery/manual run from gaining a write path here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import html
import json

from nan_fung.read_api import AccessClass

from .models import PROJECTION_SCHEMA_VERSION, ProjectionError, ProjectionRow, projection_access_class


WIKI_RENDER_SCHEMA_VERSION = "market_wiki.v1"


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProjectionError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _markdown_cell(value: object) -> str:
    text = html.escape(str(value), quote=False)
    return text.replace("|", "\\|").replace("\n", " ")


@dataclass(frozen=True)
class RenderedMarketWikiPage:
    schema_version: str
    page_id: str
    content: str
    canonical_anchor: datetime
    source_hash: str
    access_class: AccessClass | None
    observation_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    degraded: bool


def render_market_wiki(
    rows: Iterable[ProjectionRow],
    *,
    page_id: str,
    title: str,
    canonical_anchor: datetime,
) -> RenderedMarketWikiPage:
    """Return deterministic Markdown derived solely from canonical rows."""

    if not page_id or not title:
        raise ProjectionError("page_id and title must be non-empty")
    if canonical_anchor.tzinfo is None or canonical_anchor.utcoffset() is None:
        raise ProjectionError("canonical_anchor must be timezone-aware")
    ordered_rows = tuple(
        sorted(
            rows,
            key=lambda row: (
                row.projection_kind,
                row.datasource_id,
                row.observation_id,
            ),
        )
    )
    if any(not row.canonical or row.lane != "production_ingestion" for row in ordered_rows):
        raise ProjectionError("Wiki rendering accepts only canonical production rows")
    anchor = canonical_anchor.astimezone(UTC)
    if any(row.available_at > anchor for row in ordered_rows):
        raise ProjectionError("Wiki page cannot include data unavailable at its anchor")
    semantic_rows = [
        {
            "kind": row.projection_kind,
            "observation_id": row.observation_id,
            "datasource_id": row.datasource_id,
            "access_class": str(row.access_class),
            "available_at": _timestamp(row.available_at),
            "source_date": row.source_date.isoformat() if row.source_date else None,
            "unit": row.unit,
            "definition": row.definition,
            "period_label": row.period_label,
            "evidence_ids": list(row.evidence_ids),
            "fields": dict(row.fields),
            "degraded": row.degraded,
        }
        for row in ordered_rows
    ]
    source_hash = sha256(
        json.dumps(
            {
                "schema_version": WIKI_RENDER_SCHEMA_VERSION,
                "projection_schema_version": PROJECTION_SCHEMA_VERSION,
                "page_id": page_id,
                "anchor": _timestamp(anchor),
                "rows": semantic_rows,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    observation_ids = tuple(row.observation_id for row in ordered_rows)
    evidence_ids = tuple(
        sorted({evidence_id for row in ordered_rows for evidence_id in row.evidence_ids})
    )
    access_class = projection_access_class(ordered_rows)
    degraded = any(row.degraded for row in ordered_rows)
    lines = [
        "---",
        "generated: true",
        f"page_id: {_markdown_cell(page_id)}",
        f"canonical_anchor: {_timestamp(anchor)}",
        f"projection_schema_version: {PROJECTION_SCHEMA_VERSION}",
        f"source_hash: {source_hash}",
        f"access_class: {access_class or ''}",
        f"degraded: {str(degraded).lower()}",
        "observation_ids: "
        + json.dumps(list(observation_ids), ensure_ascii=False, separators=(",", ":")),
        "evidence_ids: "
        + json.dumps(list(evidence_ids), ensure_ascii=False, separators=(",", ":")),
        "---",
        "",
        f"# {_markdown_cell(title)}",
        "",
        "| Kind | Datasource | Observation | Source date | Unit | Evidence | Data |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in ordered_rows:
        data = json.dumps(dict(row.fields), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(row.projection_kind),
                    _markdown_cell(row.datasource_id),
                    _markdown_cell(row.observation_id),
                    _markdown_cell(row.source_date.isoformat() if row.source_date else ""),
                    _markdown_cell(row.unit or ""),
                    _markdown_cell(", ".join(row.evidence_ids)),
                    _markdown_cell(data),
                )
            )
            + " |"
        )
    return RenderedMarketWikiPage(
        schema_version=WIKI_RENDER_SCHEMA_VERSION,
        page_id=page_id,
        content="\n".join(lines) + "\n",
        canonical_anchor=anchor,
        source_hash=source_hash,
        access_class=access_class,
        observation_ids=observation_ids,
        evidence_ids=evidence_ids,
        degraded=degraded,
    )
