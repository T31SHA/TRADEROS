"""Strict Dukascopy tick parsing and deterministic M15 aggregation.

This module is deliberately an offline source boundary.  It accepts only the
five columns emitted by the Dukascopy-node tick CSV and never sorts, fills, or
repairs source observations.  The mutable aggregator is used by the empirical
admission path so a bounded interval can be processed without retaining every
tick in memory.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from traderos.data.bars import MarketBar
from traderos.data.errors import TimestampNormalizationError
from traderos.data.instruments import Instrument
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.time import normalize_timestamp
from traderos.data.timeframes import Timeframe

TICK_COLUMNS = ("timestamp", "askPrice", "bidPrice", "askVolume", "bidVolume")


class TickSchemaError(ValueError):
    """Raised when a raw tick CSV does not have the declared schema."""


class TickValidationError(ValueError):
    """Raised when an ordered tick stream cannot be safely aggregated."""

    def __init__(self, report: TickValidationReport) -> None:
        self.report = report
        super().__init__(
            "tick validation failed: "
            f"{report.invalid_tick_count} invalid ticks, "
            f"{report.non_monotonic_timestamps} non-monotonic timestamps"
        )


@dataclass(frozen=True)
class DukascopyTickImportConfig:
    """Explicit interpretation of a Dukascopy tick artifact."""

    instrument: Instrument
    source_symbol: str
    source_timezone: str = "UTC"
    source_version: str | None = None

    def __post_init__(self) -> None:
        if not self.source_symbol.strip() or not self.source_timezone.strip():
            raise ValueError("source symbol and source timezone must not be blank")
        try:
            ZoneInfo(self.source_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown source timezone: {self.source_timezone}") from exc


@dataclass(frozen=True)
class DukascopyTick:
    """One explicitly parsed source tick; missing values remain missing."""

    timestamp: datetime
    ask_price: Decimal | None
    bid_price: Decimal | None
    ask_volume: Decimal | None
    bid_volume: Decimal | None
    artifact_hash: str
    row_number: int


def normalize_tick_timestamp(value: str | int, *, source_timezone: str = "UTC") -> datetime:
    """Normalize an integer Unix epoch-millisecond timestamp to UTC.

    Dukascopy-node timestamps are instants, so a source timezone never changes
    the result.  It is still validated explicitly to keep the ingestion
    boundary auditable and consistent with the rest of the data engine.
    """

    try:
        ZoneInfo(source_timezone)
    except ZoneInfoNotFoundError as exc:
        raise TimestampNormalizationError(f"Unknown source timezone: {source_timezone}") from exc
    text = str(value).strip()
    digits = text[1:] if text[:1] in {"+", "-"} else text
    if not digits or any(character < "0" or character > "9" for character in digits):
        raise TickSchemaError("tick timestamp must be an integer Unix epoch in milliseconds")
    try:
        milliseconds = int(text, 10)
        seconds, remainder = divmod(milliseconds, 1000)
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            seconds=seconds, microseconds=remainder * 1000
        )
    except (OverflowError, ValueError) as exc:
        raise TickSchemaError("tick timestamp is outside the supported datetime range") from exc


def _decimal(value: str | None, *, field: str, row_number: int) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation as exc:
        raise TickSchemaError(f"row {row_number}: {field} is not a decimal") from exc


def _tick_columns(fieldnames: Sequence[str] | None) -> dict[str, str]:
    if fieldnames is None:
        raise TickSchemaError("tick CSV has no header")
    cleaned = tuple(name.lstrip("\ufeff") for name in fieldnames)
    if cleaned != TICK_COLUMNS:
        raise TickSchemaError(
            "tick CSV must contain exactly: " + ",".join(TICK_COLUMNS)
        )
    return {name: name for name in TICK_COLUMNS}


def iter_dukascopy_ticks(
    path: Path,
    *,
    artifact_hash: str,
    source_timezone: str = "UTC",
) -> Iterator[DukascopyTick]:
    """Stream a local Dukascopy-node tick CSV without network or guessing."""

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = _tick_columns(reader.fieldnames)
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise TickSchemaError(f"row {row_number}: unexpected extra CSV fields")
            yield DukascopyTick(
                timestamp=normalize_tick_timestamp(
                    row[columns["timestamp"]], source_timezone=source_timezone
                ),
                ask_price=_decimal(
                    row[columns["askPrice"]], field="askPrice", row_number=row_number
                ),
                bid_price=_decimal(
                    row[columns["bidPrice"]], field="bidPrice", row_number=row_number
                ),
                ask_volume=_decimal(
                    row[columns["askVolume"]], field="askVolume", row_number=row_number
                ),
                bid_volume=_decimal(
                    row[columns["bidVolume"]], field="bidVolume", row_number=row_number
                ),
                artifact_hash=artifact_hash,
                row_number=row_number,
            )


@dataclass(frozen=True)
class TickViolation:
    row_number: int
    code: str
    message: str
    timestamp: datetime | None

    def canonical(self) -> dict[str, object]:
        return {
            "row_number": self.row_number,
            "code": self.code,
            "message": self.message,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


@dataclass(frozen=True)
class TickValidationReport:
    """Deterministic, non-repairing validation results for raw ticks."""

    row_count: int
    invalid_tick_count: int
    non_monotonic_timestamps: int
    duplicate_timestamps: int
    nonfinite_price_count: int
    nonpositive_price_count: int
    crossed_quote_count: int
    volume_anomaly_count: int
    violations: tuple[TickViolation, ...]

    @property
    def timestamp_violation_count(self) -> int:
        return self.non_monotonic_timestamps

    @property
    def has_errors(self) -> bool:
        return self.invalid_tick_count > 0 or self.non_monotonic_timestamps > 0

    def canonical(self) -> dict[str, object]:
        return {
            "row_count": self.row_count,
            "invalid_tick_count": self.invalid_tick_count,
            "non_monotonic_timestamps": self.non_monotonic_timestamps,
            "duplicate_timestamps": self.duplicate_timestamps,
            "nonfinite_price_count": self.nonfinite_price_count,
            "nonpositive_price_count": self.nonpositive_price_count,
            "crossed_quote_count": self.crossed_quote_count,
            "volume_anomaly_count": self.volume_anomaly_count,
            "violations": [item.canonical() for item in self.violations],
        }


class _TickValidationAccumulator:
    def __init__(self) -> None:
        self.row_count = 0
        self._invalid_rows: set[int] = set()
        self.non_monotonic = 0
        self.duplicates = 0
        self.nonfinite_prices = 0
        self.nonpositive_prices = 0
        self.crossed_quotes = 0
        self.volume_anomalies = 0
        self.violations: list[TickViolation] = []
        self.previous: DukascopyTick | None = None

    def _violate(self, tick: DukascopyTick, code: str, message: str) -> None:
        self._invalid_rows.add(tick.row_number)
        self.violations.append(TickViolation(tick.row_number, code, message, tick.timestamp))

    def observe(self, tick: DukascopyTick) -> bool:
        self.row_count += 1
        ordered = self.previous is None or tick.timestamp > self.previous.timestamp
        if self.previous is not None and tick.timestamp <= self.previous.timestamp:
            self.non_monotonic += 1
            if tick.timestamp == self.previous.timestamp:
                self.duplicates += 1
                self._violate(tick, "duplicate_timestamp", "tick timestamp is duplicated")
            else:
                self._violate(tick, "non_monotonic_timestamp", "tick timestamp is not increasing")

        finite_prices = True
        for name, value in (("bid_price", tick.bid_price), ("ask_price", tick.ask_price)):
            if value is None:
                finite_prices = False
                self._violate(tick, "missing_price", f"{name} is missing")
            elif not value.is_finite():
                finite_prices = False
                self.nonfinite_prices += 1
                self._violate(tick, "nonfinite_price", f"{name} is not finite")
            elif value <= 0:
                finite_prices = False
                self.nonpositive_prices += 1
                self._violate(tick, "nonpositive_price", f"{name} is not positive")
        if (
            tick.bid_price is not None
            and tick.ask_price is not None
            and tick.bid_price.is_finite()
            and tick.ask_price.is_finite()
            and tick.bid_price > tick.ask_price
        ):
            self.crossed_quotes += 1
            self._violate(tick, "crossed_quote", "bid price exceeds ask price")

        for name, value in (("bid_volume", tick.bid_volume), ("ask_volume", tick.ask_volume)):
            if value is None:
                self.volume_anomalies += 1
                self._violate(tick, "missing_volume", f"{name} is missing")
            elif not value.is_finite() or value < 0:
                self.volume_anomalies += 1
                self._violate(tick, "volume_anomaly", f"{name} is not finite and non-negative")

        self.previous = tick
        return ordered and finite_prices and not any(
            value is None or not value.is_finite() or value < 0
            for value in (tick.bid_volume, tick.ask_volume)
        ) and not (
            tick.bid_price is not None
            and tick.ask_price is not None
            and tick.bid_price.is_finite()
            and tick.ask_price.is_finite()
            and tick.bid_price > tick.ask_price
        )

    def report(self) -> TickValidationReport:
        return TickValidationReport(
            row_count=self.row_count,
            invalid_tick_count=len(self._invalid_rows),
            non_monotonic_timestamps=self.non_monotonic,
            duplicate_timestamps=self.duplicates,
            nonfinite_price_count=self.nonfinite_prices,
            nonpositive_price_count=self.nonpositive_prices,
            crossed_quote_count=self.crossed_quotes,
            volume_anomaly_count=self.volume_anomalies,
            violations=tuple(self.violations),
        )


def validate_ticks(ticks: Iterable[DukascopyTick]) -> TickValidationReport:
    """Validate ticks in supplied order, without sorting or repairing them."""

    validator = _TickValidationAccumulator()
    for tick in ticks:
        validator.observe(tick)
    return validator.report()


@dataclass(frozen=True)
class TickSideOHLCV:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def canonical(self) -> dict[str, str]:
        return {
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume),
        }


@dataclass(frozen=True)
class TickM15Bar:
    """A present UTC bar containing exact bid and ask aggregates."""

    timestamp: datetime
    bid: TickSideOHLCV
    ask: TickSideOHLCV
    closing_spread: Decimal
    mean_spread: Decimal
    minimum_spread: Decimal
    maximum_spread: Decimal
    tick_count: int

    @property
    def bar_start(self) -> datetime:
        return self.timestamp

    def to_market_bar(
        self,
        *,
        instrument: Instrument,
        quote_side: str = "bid",
        ingestion_timestamp: datetime,
        source: str = "dukascopy-tick",
    ) -> MarketBar:
        """Map one explicit quote side to the established MarketBar contract."""

        if quote_side == "bid":
            selected = self.bid
        elif quote_side == "ask":
            selected = self.ask
        else:
            raise ValueError("quote_side must be 'bid' or 'ask'")
        return MarketBar(
            instrument=instrument,
            timeframe=Timeframe.M15,
            adjustment_policy=AdjustmentPolicy.RAW,
            timestamp=self.timestamp,
            open=selected.open,
            high=selected.high,
            low=selected.low,
            close=selected.close,
            volume=selected.volume,
            source=source,
            currency=instrument.trading_currency,
            ingestion_timestamp=ingestion_timestamp,
            bid=self.bid.close,
            ask=self.ask.close,
            spread=self.closing_spread,
            # These are closing observations.  They are not available at the
            # bar opening and must not be used to price a bar-open fill.
            quote_timestamp=self.timestamp + Timeframe.M15.duration,
        )

    def canonical(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "bid": self.bid.canonical(),
            "ask": self.ask.canonical(),
            "closing_spread": str(self.closing_spread),
            "mean_spread": str(self.mean_spread),
            "minimum_spread": str(self.minimum_spread),
            "maximum_spread": str(self.maximum_spread),
            "tick_count": self.tick_count,
        }


@dataclass(frozen=True)
class TickAggregationPolicy:
    """Versioned exact policy bound into the empirical dataset identity."""

    policy_id: str = "dukascopy-tick-to-m15"
    policy_version: str = "v1"
    timeframe: Timeframe = Timeframe.M15
    timestamp_semantics: str = "bar_start"
    price_sides: str = "bid+ask"
    volume_sides: str = "bid+ask"
    quote_configuration: str = "bid_ask_close_spread"

    def __post_init__(self) -> None:
        if not self.policy_id.strip() or not self.policy_version.strip():
            raise ValueError("aggregation policy identity must not be blank")
        if self.timeframe is not Timeframe.M15:
            raise ValueError("the empirical tick pipeline currently supports M15 only")
        if self.timestamp_semantics != "bar_start":
            raise ValueError("tick aggregation requires bar_start semantics")

    @property
    def identity(self) -> str:
        payload = {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "timeframe": self.timeframe.value,
            "timestamp_semantics": self.timestamp_semantics,
            "price_sides": self.price_sides,
            "volume_sides": self.volume_sides,
            "quote_configuration": self.quote_configuration,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def canonical(self) -> dict[str, str]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "timeframe": self.timeframe.value,
            "timestamp_semantics": self.timestamp_semantics,
            "price_sides": self.price_sides,
            "volume_sides": self.volume_sides,
            "quote_configuration": self.quote_configuration,
        }


class _SideAccumulator:
    def __init__(self, price: Decimal, volume: Decimal) -> None:
        self.open = price
        self.high = price
        self.low = price
        self.close = price
        self.volume = volume

    def add(self, price: Decimal, volume: Decimal) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume

    def finish(self) -> TickSideOHLCV:
        return TickSideOHLCV(self.open, self.high, self.low, self.close, self.volume)


class _BarAccumulator:
    def __init__(self, tick: DukascopyTick, bucket: datetime) -> None:
        assert tick.bid_price is not None
        assert tick.ask_price is not None
        assert tick.bid_volume is not None
        assert tick.ask_volume is not None
        self.bucket = bucket
        self.bid = _SideAccumulator(tick.bid_price, tick.bid_volume)
        self.ask = _SideAccumulator(tick.ask_price, tick.ask_volume)
        self.spread_sum = tick.ask_price - tick.bid_price
        self.minimum_spread = self.spread_sum
        self.maximum_spread = self.spread_sum
        self.tick_count = 1

    def add(self, tick: DukascopyTick) -> None:
        assert tick.bid_price is not None
        assert tick.ask_price is not None
        assert tick.bid_volume is not None
        assert tick.ask_volume is not None
        spread = tick.ask_price - tick.bid_price
        self.bid.add(tick.bid_price, tick.bid_volume)
        self.ask.add(tick.ask_price, tick.ask_volume)
        self.spread_sum += spread
        self.minimum_spread = min(self.minimum_spread, spread)
        self.maximum_spread = max(self.maximum_spread, spread)
        self.tick_count += 1

    def finish(self) -> TickM15Bar:
        with localcontext() as context:
            context.prec = 50
            mean_spread = self.spread_sum / Decimal(self.tick_count)
        return TickM15Bar(
            timestamp=self.bucket,
            bid=self.bid.finish(),
            ask=self.ask.finish(),
            closing_spread=self.ask.close - self.bid.close,
            mean_spread=mean_spread,
            minimum_spread=self.minimum_spread,
            maximum_spread=self.maximum_spread,
            tick_count=self.tick_count,
        )


def _m15_bucket(timestamp: datetime) -> datetime:
    utc_timestamp = normalize_timestamp(timestamp)
    return utc_timestamp.replace(minute=(utc_timestamp.minute // 15) * 15, second=0, microsecond=0)


@dataclass(frozen=True)
class TickAggregationResult:
    bars: tuple[TickM15Bar, ...]
    validation: TickValidationReport


class TickAggregator:
    """Online ordered aggregator that emits no synthetic empty intervals."""

    def __init__(self, *, policy: TickAggregationPolicy | None = None) -> None:
        self.policy = policy or TickAggregationPolicy()
        self._validator = _TickValidationAccumulator()
        self._current: _BarAccumulator | None = None
        self._bars: list[TickM15Bar] = []

    def add(self, tick: DukascopyTick) -> None:
        valid = self._validator.observe(tick)
        if not valid:
            return
        bucket = _m15_bucket(tick.timestamp)
        if self._current is not None and bucket < self._current.bucket:
            # This is defensive; ordering has already been checked above.
            self._validator._violate(tick, "non_monotonic_timestamp", "tick bucket moved backwards")
            return
        if self._current is None:
            self._current = _BarAccumulator(tick, bucket)
        elif bucket == self._current.bucket:
            self._current.add(tick)
        else:
            self._bars.append(self._current.finish())
            self._current = _BarAccumulator(tick, bucket)

    def finish(self) -> TickAggregationResult:
        if self._current is not None:
            self._bars.append(self._current.finish())
            self._current = None
        return TickAggregationResult(tuple(self._bars), self._validator.report())


def aggregate_ticks(
    ticks: Iterable[DukascopyTick], *, policy: TickAggregationPolicy | None = None
) -> tuple[TickM15Bar, ...]:
    """Aggregate ordered valid ticks or raise with an explicit quality report."""

    aggregator = TickAggregator(policy=policy)
    for tick in ticks:
        aggregator.add(tick)
    result = aggregator.finish()
    if result.validation.has_errors:
        raise TickValidationError(result.validation)
    return result.bars


def _normalized_decimal(value: object, *, field: str, line_number: int) -> Decimal:
    if not isinstance(value, str):
        raise TickSchemaError(f"normalized row {line_number}: {field} is not a string decimal")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise TickSchemaError(f"normalized row {line_number}: {field} is not a decimal") from exc


def iter_normalized_tick_bars(path: Path) -> Iterator[TickM15Bar]:
    """Read the immutable canonical tick-derived JSONL without source access."""

    fields = ("open", "high", "low", "close", "volume")
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                payload = json.loads(line)
                bid_payload = payload["bid"]
                ask_payload = payload["ask"]
                if not isinstance(bid_payload, dict) or not isinstance(ask_payload, dict):
                    raise TypeError
                bid = TickSideOHLCV(
                    **{
                        field: _normalized_decimal(
                            bid_payload[field],
                            field=f"bid.{field}",
                            line_number=line_number,
                        )
                        for field in fields
                    }
                )
                ask = TickSideOHLCV(
                    **{
                        field: _normalized_decimal(
                            ask_payload[field],
                            field=f"ask.{field}",
                            line_number=line_number,
                        )
                        for field in fields
                    }
                )
                timestamp = datetime.fromisoformat(payload["timestamp"])
                timestamp = normalize_timestamp(timestamp)
                tick_count = int(payload["tick_count"])
                if tick_count <= 0:
                    raise TickSchemaError(
                        f"normalized row {line_number}: tick_count must be positive"
                    )
                yield TickM15Bar(
                    timestamp=timestamp,
                    bid=bid,
                    ask=ask,
                    closing_spread=_normalized_decimal(
                        payload["closing_spread"],
                        field="closing_spread",
                        line_number=line_number,
                    ),
                    mean_spread=_normalized_decimal(
                        payload["mean_spread"],
                        field="mean_spread",
                        line_number=line_number,
                    ),
                    minimum_spread=_normalized_decimal(
                        payload["minimum_spread"],
                        field="minimum_spread",
                        line_number=line_number,
                    ),
                    maximum_spread=_normalized_decimal(
                        payload["maximum_spread"],
                        field="maximum_spread",
                        line_number=line_number,
                    ),
                    tick_count=tick_count,
                )
            except (
                KeyError,
                TypeError,
                ValueError,
                InvalidOperation,
                TimestampNormalizationError,
                json.JSONDecodeError,
            ) as exc:
                if isinstance(exc, TickSchemaError):
                    raise
                raise TickSchemaError(f"normalized row {line_number} is malformed") from exc


def iter_normalized_tick_market_bars(
    path: Path,
    *,
    instrument: Instrument,
    quote_side: str = "bid",
    ingestion_timestamp: datetime,
    source: str = "dukascopy-tick",
) -> Iterator[MarketBar]:
    """Map canonical tick bars to the existing single-side MarketBar contract."""

    for bar in iter_normalized_tick_bars(path):
        yield bar.to_market_bar(
            instrument=instrument,
            quote_side=quote_side,
            ingestion_timestamp=ingestion_timestamp,
            source=source,
        )


__all__ = [
    "DukascopyTick",
    "DukascopyTickImportConfig",
    "TICK_COLUMNS",
    "TickAggregationPolicy",
    "TickAggregationResult",
    "TickAggregator",
    "TickM15Bar",
    "TickSchemaError",
    "TickSideOHLCV",
    "TickValidationError",
    "TickValidationReport",
    "TickViolation",
    "aggregate_ticks",
    "iter_dukascopy_ticks",
    "iter_normalized_tick_bars",
    "iter_normalized_tick_market_bars",
    "normalize_tick_timestamp",
    "validate_ticks",
]
