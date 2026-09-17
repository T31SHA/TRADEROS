"""Fail-closed historical-dataset qualification for Phase 9.

This module deliberately validates data; it never repairs, fills, adjusts, or
otherwise changes it.  Phase 1 remains the owner of bar validation and market
calendar semantics.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from traderos.data.bars import MarketBar
from traderos.data.calendars import MarketCalendar
from traderos.data.quality import DataQualityEngine, QualitySeverity
from traderos.data.time import require_utc
from traderos.research.models import DatasetManifest, ResearchError


class DatasetQualificationStatus(StrEnum):
    """A qualified dataset is the only dataset eligible for research."""

    QUALIFIED = "qualified"
    REJECTED = "rejected"


class ResearchDatasetEligibility(StrEnum):
    """Separate architecture fixtures from data eligible for empirical claims."""

    DETERMINISTIC_TEST_FIXTURE = "deterministic_test_fixture"
    EMPIRICALLY_QUALIFIED_DATASET = "empirically_qualified_dataset"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class QualificationPolicy:
    """Versioned, explicit requirements rather than silent data repair."""

    policy_id: str
    policy_version: str
    require_quotes: bool = False
    reject_warnings: bool = False
    require_equity_history: bool = True

    def __post_init__(self) -> None:
        if not self.policy_id.strip() or not self.policy_version.strip():
            raise ResearchError("dataset qualification policy identity must not be blank")

    @property
    def identity(self) -> str:
        payload = {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "require_quotes": self.require_quotes,
            "reject_warnings": self.reject_warnings,
            "require_equity_history": self.require_equity_history,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class DatasetQualification:
    """Machine-readable audit of a particular immutable manifest."""

    manifest: DatasetManifest
    policy_identity: str
    status: DatasetQualificationStatus
    checked_at: datetime
    bar_count: int
    missing_bars: int
    duplicate_timestamps: int
    quote_coverage: float
    quality_events: tuple[str, ...]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        require_utc(self.checked_at)
        if self.bar_count < 1 or self.missing_bars < 0 or self.duplicate_timestamps < 0:
            raise ResearchError("dataset qualification counts must be non-negative")
        if not 0 <= self.quote_coverage <= 1:
            raise ResearchError("dataset quote coverage must be in [0, 1]")
        if self.status is DatasetQualificationStatus.QUALIFIED and self.reasons:
            raise ResearchError("a qualified dataset cannot retain rejection reasons")
        if self.status is DatasetQualificationStatus.REJECTED and not self.reasons:
            raise ResearchError("a rejected dataset requires explicit reasons")

    @property
    def identity(self) -> str:
        payload = {
            "dataset_hash": self.manifest.dataset_hash,
            "policy_identity": self.policy_identity,
            "status": self.status.value,
            "bar_count": self.bar_count,
            "missing_bars": self.missing_bars,
            "duplicate_timestamps": self.duplicate_timestamps,
            "quote_coverage": self.quote_coverage,
            "quality_events": self.quality_events,
            "reasons": self.reasons,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def require_qualified(self) -> None:
        if self.status is not DatasetQualificationStatus.QUALIFIED:
            raise ResearchError("dataset qualification failed closed: " + "; ".join(self.reasons))

    def canonical(self) -> dict[str, object]:
        return {
            "qualification_id": self.identity,
            "dataset_version": self.manifest.dataset_version,
            "dataset_hash": self.manifest.dataset_hash,
            "policy_identity": self.policy_identity,
            "status": self.status.value,
            "checked_at": self.checked_at.isoformat(),
            "bar_count": self.bar_count,
            "missing_bars": self.missing_bars,
            "duplicate_timestamps": self.duplicate_timestamps,
            "quote_coverage": self.quote_coverage,
            "quality_events": list(self.quality_events),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class DatasetEligibilityReport:
    """Explicit empirical gate; fixtures can validate code but not strategies."""

    qualification: DatasetQualification
    eligibility: ResearchDatasetEligibility
    source_version: str | None
    calendar_identity: str | None
    quote_convention: str | None
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.eligibility is ResearchDatasetEligibility.EMPIRICALLY_QUALIFIED_DATASET and (
            self.blockers or not self.source_version or not self.calendar_identity
        ):
            raise ResearchError(
                "eligible empirical datasets require source, calendar, and no blockers"
            )
        if self.eligibility is ResearchDatasetEligibility.BLOCKED and not self.blockers:
            raise ResearchError("blocked dataset eligibility requires explicit blockers")

    @property
    def empirically_eligible(self) -> bool:
        return self.eligibility is ResearchDatasetEligibility.EMPIRICALLY_QUALIFIED_DATASET


def assess_empirical_eligibility(
    *,
    qualification: DatasetQualification,
    deterministic_test_fixture: bool,
    source_version: str | None,
    calendar_identity: str | None,
    quote_convention: str | None = None,
) -> DatasetEligibilityReport:
    """Fail closed for missing immutable empirical-data metadata."""

    if deterministic_test_fixture:
        return DatasetEligibilityReport(
            qualification,
            ResearchDatasetEligibility.DETERMINISTIC_TEST_FIXTURE,
            source_version,
            calendar_identity,
            quote_convention,
        )
    blockers: list[str] = []
    if qualification.status is not DatasetQualificationStatus.QUALIFIED:
        blockers.append("dataset_qualification_failed")
    if not source_version:
        blockers.append("source_version_missing")
    if not calendar_identity:
        blockers.append("calendar_identity_missing")
    if qualification.manifest.equity_bias_unresolved:
        blockers.append("survivorship_or_corporate_action_integrity_unverified")
    if any(symbol for symbol in qualification.manifest.symbols) and quote_convention is None:
        blockers.append("quote_or_price_convention_missing")
    return DatasetEligibilityReport(
        qualification,
        (
            ResearchDatasetEligibility.BLOCKED
            if blockers
            else ResearchDatasetEligibility.EMPIRICALLY_QUALIFIED_DATASET
        ),
        source_version,
        calendar_identity,
        quote_convention,
        tuple(blockers),
    )


def qualify_dataset(
    *,
    manifest: DatasetManifest,
    bars: Sequence[MarketBar],
    policy: QualificationPolicy,
    checked_at: datetime,
    calendar: MarketCalendar | None = None,
) -> DatasetQualification:
    """Qualify an exact bar sequence without mutating the historical record.

    Calendar checks are delegated to the Phase 1 quality engine where a
    calendar is available.  A caller that needs session completeness must pass
    the appropriate asset-specific calendar; no generic calendar is invented.
    """

    require_utc(checked_at)
    ordered = tuple(sorted(bars, key=lambda bar: (bar.timestamp, bar.symbol, bar.source)))
    reasons: list[str] = []
    events: list[str] = []
    if not ordered:
        raise ResearchError("dataset qualification requires bars")
    if (
        DatasetManifest.from_bars(
            dataset_version=manifest.dataset_version,
            bars=ordered,
            quality_status=manifest.quality_status,
            historical_membership_available=manifest.historical_membership_available,
            delistings_available=manifest.delistings_available,
            corporate_actions_verified=manifest.corporate_actions_verified,
        ).dataset_hash
        != manifest.dataset_hash
    ):
        raise ResearchError("bars do not match the immutable dataset manifest")
    timestamps = [(bar.symbol, bar.timestamp) for bar in ordered]
    duplicate_count = sum(count - 1 for count in Counter(timestamps).values() if count > 1)
    if duplicate_count:
        reasons.append("duplicate timestamps")
    if len(manifest.symbols) == 1 and any(
        bar.timestamp >= next_bar.timestamp
        for bar, next_bar in zip(ordered, ordered[1:], strict=False)
    ):
        # Cross-symbol rows can share a time; identity duplication is checked above.
        reasons.append("timestamps are not strictly monotonic")
    quote_coverage = sum(bar.bid is not None and bar.ask is not None for bar in ordered) / len(
        ordered
    )
    if policy.require_quotes and quote_coverage < 1:
        reasons.append("bid/ask availability is incomplete")
    if policy.require_equity_history and manifest.equity_bias_unresolved:
        reasons.append("equity survivorship or corporate-action lineage is unresolved")

    missing = 0
    if calendar is not None:
        quality = DataQualityEngine().inspect(
            list(ordered),
            calendar=calendar,
            start=manifest.period.start,
            end=manifest.period.end,
            occurred_at=checked_at,
        )
        missing = sum(event.code.value == "missing_bar" for event in quality.events)
        events.extend(f"{event.severity.value}:{event.code.value}" for event in quality.events)
        if any(event.severity is QualitySeverity.ERROR for event in quality.events):
            reasons.append("calendar or quality validation reported errors")
        if policy.reject_warnings and any(
            event.severity is QualitySeverity.WARNING for event in quality.events
        ):
            reasons.append("quality validation reported warnings under strict policy")
    status = (
        DatasetQualificationStatus.REJECTED if reasons else DatasetQualificationStatus.QUALIFIED
    )
    return DatasetQualification(
        manifest=manifest,
        policy_identity=policy.identity,
        status=status,
        checked_at=checked_at,
        bar_count=len(ordered),
        missing_bars=missing,
        duplicate_timestamps=duplicate_count,
        quote_coverage=quote_coverage,
        quality_events=tuple(sorted(events)),
        reasons=tuple(sorted(set(reasons))),
    )


__all__ = [
    "DatasetQualification",
    "DatasetQualificationStatus",
    "DatasetEligibilityReport",
    "QualificationPolicy",
    "ResearchDatasetEligibility",
    "assess_empirical_eligibility",
    "qualify_dataset",
]
