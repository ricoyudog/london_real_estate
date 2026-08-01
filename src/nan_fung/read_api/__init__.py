"""Typed, read-only datasource API.  This package is not an HTTP server."""

from .access import AccessClass, ReadContext, most_restrictive_access
from .contracts import (
    ALLOWED_FILTERS,
    QUERY_KINDS,
    READ_SCHEMA_VERSION,
    AccessDenied,
    InMemoryReadRepository,
    InvalidCursor,
    InvalidReadRequest,
    PagedReadRepository,
    ReadApiError,
    ReadPage,
    ReadQuery,
    ReadRecord,
    ReadRepository,
    ReadResponse,
)
from .service import ReadService, query_data_v1
from .sqlite_repository import SQLiteReadRepository

__all__ = [
    "ALLOWED_FILTERS",
    "QUERY_KINDS",
    "READ_SCHEMA_VERSION",
    "AccessClass",
    "AccessDenied",
    "InMemoryReadRepository",
    "InvalidCursor",
    "InvalidReadRequest",
    "PagedReadRepository",
    "ReadApiError",
    "ReadPage",
    "ReadContext",
    "ReadQuery",
    "ReadRecord",
    "ReadRepository",
    "ReadResponse",
    "ReadService",
    "SQLiteReadRepository",
    "most_restrictive_access",
    "query_data_v1",
]
