"""Data-quality event models and timeframe-aware inspection."""

from collections import Counter
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from traderos.data.bars import MarketBar
from traderos.data.calendars import MarketCalendar
from traderos.data.time import normalize_timestamp


class QualitySeverity(StrEnum):
    """Severity levels for observable quality findings."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class QualityCode(StrEnum):
    """Stable codes for querying and aggregating quality findings."""

    MISSING_BAR = "missing_bar"
    DUPLICATE_BAR = "duplicate_bar"
    INVALID_OHLC = "invalid_ohlc"
    NEGATIVE_VOLUME = "negative_volume"
    INVALID_PRICE = "invalid_price"
    SUSPICIOUS_JUMP = "suspicious_jump"
    STALE_DATA = "stale_data"
    UNEXPECTED_FREQUENCY = "unexpected_frequency"
    TIMEZONE_INCONSISTENCY = "timezone_inconsistency"
    PROVIDER_INCONSISTENCY = "provider_inconsistency"


class DataQualityEvent(BaseModel):
    """A queryable, append-only quality finding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    occurred_at: datetime
    severity: QualitySeverity
    code: QualityCode
    message: str = Field(min_length=1)
    source: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    bar_timestamp: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at", "bar_timestamp")
    @classmethod
    def require_utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
            raise ValueError("quality-event timestamps must be UTC")
        return value


class QualityReport(BaseModel):
    """Summary of inspection results for a bounded bar set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    events: tuple[DataQualityEvent, ...] = ()
    checked_bars: int = Field(default=0, ge=0)

    @property
    def error_count(self) -> int:
        return sum(event.severity is QualitySeverity.ERROR for event in self.events)

    @property
    def warning_count(self) -> int:
        return sum(event.severity is QualitySeverity.WARNING for event in self.events)

    @property
    def counts_by_code(self) -> dict[str, int]:
        return dict(Counter(event.code.value for event in self.events))


def _event(
    severity: QualitySeverity,
    code: QualityCode,
    message: str,
    *,
    bar: MarketBar | None = None,
    occurred_at: datetime,
    metadata: dict[str, Any] | None = None,
) -> DataQualityEvent:
    return DataQualityEvent(
        occurred_at=normalize_timestamp(occurred_at),
        severity=severity,
        code=code,
        message=message,
        source=bar.source if bar else None,
        symbol=bar.symbol if bar else None,
        timeframe=bar.timeframe.value if bar else None,
        bar_timestamp=bar.timestamp if bar else None,
        metadata=metadata or {},
    )


class DataQualityEngine:
    """Detect duplicates, calendar-aware gaps, frequency anomalies, and jumps."""

    def __init__(self, suspicious_jump_fraction: Decimal = Decimal("0.20")) -> None:
        if suspicious_jump_fraction <= 0:
            raise ValueError("suspicious_jump_fraction must be positive")
        self._suspicious_jump_fraction = suspicious_jump_fraction

    def inspect(
        self,
        bars: tuple[MarketBar, ...] | list[MarketBar],
        *,
        calendar: MarketCalendar,
        start: datetime,
        end: datetime,
        occurred_at: datetime,
    ) -> QualityReport:
        """Inspect a bounded series without deleting or repairing any bar."""

        ordered = sorted(bars, key=lambda bar: bar.timestamp)
        events: list[DataQualityEvent] = []
        seen: set[tuple[str, str, datetime, str, str]] = set()
        unique: list[MarketBar] = []
        reference = (
            (
                ordered[0].symbol,
                ordered[0].timeframe,
                ordered[0].source,
                ordered[0].adjustment_policy,
            )
            if ordered
            else None
        )
        for bar in ordered:
            if (
                reference is not None
                and (
                    bar.symbol,
                    bar.timeframe,
                    bar.source,
                    bar.adjustment_policy,
                )
                != reference
            ):
                events.append(
                    _event(
                        QualitySeverity.ERROR,
                        QualityCode.PROVIDER_INCONSISTENCY,
                        "Quality inspection received bars from multiple logical series",
                        bar=bar,
                        occurred_at=occurred_at,
                    )
                )
            key = (
                bar.symbol,
                bar.timeframe.value,
                bar.timestamp,
                bar.source,
                bar.adjustment_policy.value,
            )
            if key in seen:
                events.append(
                    _event(
                        QualitySeverity.WARNING,
                        QualityCode.DUPLICATE_BAR,
                        "Duplicate logical bar detected for "
                        f"{bar.symbol} at {bar.timestamp.isoformat()}",
                        bar=bar,
                        occurred_at=occurred_at,
                    )
                )
            else:
                seen.add(key)
                unique.append(bar)

        if unique:
            timeframe = unique[0].timeframe
            expected = set(calendar.expected_bar_timestamps(start, end, timeframe))
            actual = {bar.timestamp for bar in unique}
            for missing in sorted(expected - actual):
                events.append(
                    _event(
                        QualitySeverity.ERROR,
                        QualityCode.MISSING_BAR,
                        f"Expected bar is missing at {missing.isoformat()}",
                        occurred_at=occurred_at,
                        metadata={"timestamp": missing.isoformat()},
                    )
                )
            for unexpected in sorted(actual - expected):
                bar = next(item for item in unique if item.timestamp == unexpected)
                events.append(
                    _event(
                        QualitySeverity.ERROR,
                        QualityCode.UNEXPECTED_FREQUENCY,
                        "Bar timestamp is outside the expected calendar/frequency: "
                        f"{unexpected.isoformat()}",
                        bar=bar,
                        occurred_at=occurred_at,
                    )
                )

            for previous, current in zip(unique, unique[1:], strict=False):
                if previous.close == 0:
                    continue
                jump = abs(current.open - previous.close) / previous.close
                if jump > self._suspicious_jump_fraction:
                    events.append(
                        _event(
                            QualitySeverity.WARNING,
                            QualityCode.SUSPICIOUS_JUMP,
                            f"Opening price moved {jump:.2%} from the prior close",
                            bar=current,
                            occurred_at=occurred_at,
                            metadata={"fraction": str(jump)},
                        )
                    )
        return QualityReport(events=tuple(events), checked_bars=len(bars))

    def check_freshness(
        self,
        latest_bar: MarketBar | None,
        *,
        expected_latest: datetime,
        occurred_at: datetime,
    ) -> DataQualityEvent | None:
        """Return a stale-data event when the latest bar is behind expectation."""

        if latest_bar is None or latest_bar.timestamp < normalize_timestamp(expected_latest):
            return _event(
                QualitySeverity.ERROR,
                QualityCode.STALE_DATA,
                "Latest available market data is older than the expected latest bar",
                bar=latest_bar,
                occurred_at=occurred_at,
                metadata={
                    "expected_latest": normalize_timestamp(expected_latest).isoformat(),
                    "actual_latest": latest_bar.timestamp.isoformat() if latest_bar else None,
                },
            )
        return None


__all__ = [
    "DataQualityEvent",
    "DataQualityEngine",
    "QualityCode",
    "QualityReport",
    "QualitySeverity",
]
