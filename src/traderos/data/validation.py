"""Dedicated candidate normalization and market-data validation."""

from collections.abc import Sequence
from datetime import datetime

from pydantic import ValidationError

from traderos.data.bars import BarCandidate, MarketBar
from traderos.data.errors import DataValidationError, TimestampNormalizationError
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.quality import DataQualityEvent, QualityCode, QualitySeverity
from traderos.data.time import normalize_timestamp
from traderos.data.timeframes import Timeframe


class BarValidationResult:
    """Either a canonical bar or explicit quality events."""

    def __init__(
        self,
        bar: MarketBar | None,
        events: tuple[DataQualityEvent, ...],
    ) -> None:
        self.bar = bar
        self.events = events


def _validation_event(
    candidate: BarCandidate,
    code: QualityCode,
    message: str,
    source: str,
    occurred_at: datetime,
) -> DataQualityEvent:
    bar_timestamp = None
    try:
        bar_timestamp = normalize_timestamp(candidate.timestamp, candidate.source_timezone)
    except TimestampNormalizationError:
        # The event itself must remain recordable even when the timestamp is
        # precisely what failed validation.
        bar_timestamp = None
    return DataQualityEvent(
        occurred_at=normalize_timestamp(occurred_at),
        severity=QualitySeverity.ERROR,
        code=code,
        message=message,
        source=source,
        symbol=candidate.instrument.canonical_symbol,
        timeframe=candidate.timeframe.value,
        bar_timestamp=bar_timestamp,
    )


def normalize_and_validate_candidate(
    candidate: BarCandidate,
    *,
    expected_symbol: str,
    expected_timeframe: Timeframe,
    adjustment_policy: AdjustmentPolicy,
    source: str,
    ingestion_timestamp: datetime,
    occurred_at: datetime,
) -> BarValidationResult:
    """Normalize one candidate and reject invalid data without repairing it."""

    try:
        if candidate.instrument.canonical_symbol != expected_symbol:
            raise DataValidationError("candidate symbol does not match the request")
        if candidate.timeframe is not expected_timeframe:
            raise DataValidationError("candidate timeframe does not match the request")
        if candidate.adjustment_policy is not adjustment_policy:
            raise DataValidationError("candidate adjustment policy does not match the request")
        timestamp = normalize_timestamp(candidate.timestamp, candidate.source_timezone)
        bar = MarketBar(
            instrument=candidate.instrument,
            timeframe=candidate.timeframe,
            adjustment_policy=adjustment_policy,
            timestamp=timestamp,
            open=candidate.open,
            high=candidate.high,
            low=candidate.low,
            close=candidate.close,
            volume=candidate.volume,
            source=source,
            currency=candidate.instrument.trading_currency,
            ingestion_timestamp=normalize_timestamp(ingestion_timestamp),
            bid=candidate.bid,
            ask=candidate.ask,
            quote_timestamp=(
                normalize_timestamp(candidate.quote_timestamp, candidate.source_timezone)
                if candidate.quote_timestamp is not None
                else None
            ),
            spread=candidate.spread,
            adjusted_close=candidate.adjusted_close,
            trade_count=candidate.trade_count,
            vwap=candidate.vwap,
        )
    except (DataValidationError, ValidationError, ValueError) as exc:
        message = str(exc)
        code = QualityCode.INVALID_OHLC
        lowered = message.lower()
        if "timestamp" in lowered or "local" in lowered or "timezone" in lowered:
            code = QualityCode.TIMEZONE_INCONSISTENCY
        elif "volume" in lowered:
            code = QualityCode.NEGATIVE_VOLUME
        elif "price" in lowered or "positive" in lowered or "finite" in lowered:
            code = QualityCode.INVALID_PRICE
        return BarValidationResult(
            bar=None,
            events=(_validation_event(candidate, code, message, source, occurred_at),),
        )
    return BarValidationResult(bar=bar, events=())


def validate_batch(bars: Sequence[MarketBar]) -> None:
    """Assert canonical bars retain the invariant expected by downstream code."""

    for bar in bars:
        if bar.high < max(bar.open, bar.close, bar.low):
            raise DataValidationError(f"invalid high for {bar.symbol} at {bar.timestamp}")
        if bar.low > min(bar.open, bar.close, bar.high):
            raise DataValidationError(f"invalid low for {bar.symbol} at {bar.timestamp}")


__all__ = ["BarValidationResult", "normalize_and_validate_candidate", "validate_batch"]
