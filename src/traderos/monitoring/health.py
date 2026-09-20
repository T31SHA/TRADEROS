"""Small, explicit health assertions for local application boundaries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from traderos.data.time import require_utc
from traderos.risk.models import SystemHealthSnapshot, SystemHealthStatus


@dataclass(frozen=True)
class HealthCheck:
    """One concrete prerequisite observed by a local supervisor."""

    name: str
    healthy: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("health check name must not be blank")
        if type(self.healthy) is not bool:
            raise TypeError("health check result must be boolean")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("health check reason must not be blank")


class LocalSystemHealthMonitor:
    """Derive a conservative health snapshot from explicit local checks.

    This monitor does not attest to a broker, network provider, or external
    service. An empty check set remains UNKNOWN; one failed check is
    UNAVAILABLE. The caller supplies the event-time timestamp required by the
    existing risk firewall.
    """

    def snapshot(
        self, timestamp: datetime, checks: Sequence[HealthCheck] = ()
    ) -> SystemHealthSnapshot:
        require_utc(timestamp)
        if not checks:
            status = SystemHealthStatus.UNKNOWN
        elif all(check.healthy for check in checks):
            status = SystemHealthStatus.HEALTHY
        else:
            status = SystemHealthStatus.UNAVAILABLE
        return SystemHealthSnapshot(timestamp, status)


__all__ = ["HealthCheck", "LocalSystemHealthMonitor"]
