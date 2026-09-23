"""Trusted execution authority passed across the operational paper boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ExecutionAuthority:
    """One lease generation that may authorize operational paper mutations.

    The token is an opaque capability to application code.  The database is
    authoritative: every operational financial transaction locks the lease
    row and compares all four identities plus the generation and expiry.
    """

    workload_id: str
    lease_key: str
    owner_id: str
    generation: int
    observed_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not all(
            value.strip() for value in (self.workload_id, self.lease_key, self.owner_id)
        ):
            raise ValueError("execution authority identity must not be blank")
        if self.generation <= 0:
            raise ValueError("execution authority generation must be positive")


__all__ = ["ExecutionAuthority"]
