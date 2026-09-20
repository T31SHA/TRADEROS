"""Fail-closed local system-health assertions."""

from datetime import UTC, datetime

import pytest

from traderos.monitoring import HealthCheck, LocalSystemHealthMonitor
from traderos.risk import SystemHealthStatus

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def test_empty_health_checks_remain_unknown() -> None:
    snapshot = LocalSystemHealthMonitor().snapshot(BASE)

    assert snapshot.status is SystemHealthStatus.UNKNOWN


def test_all_explicit_checks_are_healthy() -> None:
    snapshot = LocalSystemHealthMonitor().snapshot(
        BASE,
        (HealthCheck("database", True), HealthCheck("lease", True)),
    )

    assert snapshot.status is SystemHealthStatus.HEALTHY


def test_one_failed_check_is_unavailable() -> None:
    snapshot = LocalSystemHealthMonitor().snapshot(
        BASE,
        (HealthCheck("database", True), HealthCheck("reconciliation", False)),
    )

    assert snapshot.status is SystemHealthStatus.UNAVAILABLE


def test_invalid_health_check_is_rejected() -> None:
    with pytest.raises(ValueError, match="health check name"):
        HealthCheck("", True)
