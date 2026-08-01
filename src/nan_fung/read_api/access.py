"""Access policy primitives for the typed read surface.

Contexts are deliberately constructed outside this package by a trusted host
adapter.  Tool arguments must never be converted into a ``ReadContext``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
from typing import Iterable


class AccessClass(StrEnum):
    """The access classes defined by the datasource decision record."""

    OPEN = "open"
    INTERNAL = "internal"
    RESTRICTED = "restricted"
    REFERENCE_ONLY = "reference_only"


_ACCESS_RANK = {
    AccessClass.OPEN: 0,
    AccessClass.INTERNAL: 1,
    AccessClass.RESTRICTED: 2,
    AccessClass.REFERENCE_ONLY: 3,
}


def most_restrictive_access(
    access_classes: Iterable[AccessClass | str],
) -> AccessClass | None:
    """Return the output class required by a set of cited inputs.

    ``reference_only`` is intentionally the strictest class.  This makes a
    projection unable to accidentally lower an evidence restriction merely by
    combining it with open data.
    """

    values = tuple(AccessClass(value) for value in access_classes)
    if not values:
        return None
    return max(values, key=_ACCESS_RANK.__getitem__)


@dataclass(frozen=True)
class ReadContext:
    """Trusted caller identity and its fixed read capability.

    ``allowed_access_classes`` is an explicit set rather than a caller-chosen
    maximum level.  A host can therefore grant exactly the policy it intended.
    ``allowed_result_refs`` contains opaque, host-issued capabilities for
    run-scoped non-canonical results.
    """

    principal: str
    allowed_access_classes: frozenset[AccessClass | str]
    allowed_result_refs: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.principal or not self.principal.strip():
            raise ValueError("ReadContext.principal must be non-empty")
        classes = frozenset(AccessClass(value) for value in self.allowed_access_classes)
        if not classes:
            raise ValueError("ReadContext must grant at least one access class")
        result_refs = frozenset(self.allowed_result_refs)
        if any(not ref or not ref.strip() for ref in result_refs):
            raise ValueError("ReadContext result references must be non-empty")
        object.__setattr__(self, "allowed_access_classes", classes)
        object.__setattr__(self, "allowed_result_refs", result_refs)

    def allows(self, access_class: AccessClass | str) -> bool:
        return AccessClass(access_class) in self.allowed_access_classes

    def policy_fingerprint(self) -> str:
        """Stable cursor binding; it is not an authorization token."""

        payload = {
            "principal": self.principal,
            "access_classes": sorted(str(value) for value in self.allowed_access_classes),
            "result_refs": sorted(self.allowed_result_refs),
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return sha256(encoded).hexdigest()
