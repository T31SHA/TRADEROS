"""Offline, Dukascopy-compatible CSV import boundary.

This adapter deliberately does not download data or decode executable/vendor
binary content.  It accepts an already-preserved local CSV artifact and turns
each row into a provider-neutral quote record for the empirical admission gate.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from traderos.data.bars import MarketBar
from traderos.data.errors import TimestampNormalizationError
from traderos.data.instruments import Instrument
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.time import normalize_timestamp
from traderos.data.timeframes import Timeframe


class TimestampSemantics(StrEnum):
    """The source meaning of its timestamp; it must be declared by the importer."""

    BAR_START = "bar_start"
    BAR_END = "bar_end"


class TimestampFormat(StrEnum):
    """The declared representation of timestamps in the source artifact."""

    ISO_8601 = "iso_8601"
    EPOCH_MILLISECONDS = "epoch_milliseconds"


class QuoteConvention(StrEnum):
    """Which unmodified source side supplies canonical ``MarketBar`` OHLC."""

    BID = "bid"
    ASK = "ask"


class DukascopySchemaError(ValueError):
    """Raised for a malformed or drifting local source schema."""


@dataclass(frozen=True)
class DukascopyImportConfig:
    """Explicit, source-specific interpretation; no timestamp convention is guessed."""

    instrument: Instrument
    timeframe: Timeframe
    source_symbol: str
    source_timezone: str
    timestamp_semantics: TimestampSemantics
    quote_convention: QuoteConvention
    source_version: str | None = None
    timestamp_format: TimestampFormat = TimestampFormat.ISO_8601
    price_tick_size: Decimal | None = None
    quantization_policy_id: str = "dukascopy-fixed-price-quantization"
    quantization_policy_version: str = "v1"

    def __post_init__(self) -> None:
        if not self.source_symbol.strip() or not self.source_timezone.strip():
            raise ValueError("source symbol and source timezone must not be blank")
        if self.price_tick_size is not None and (
            not self.price_tick_size.is_finite() or self.price_tick_size <= 0
        ):
            raise ValueError("price tick size must be finite and positive")
        if not self.quantization_policy_id.strip() or not self.quantization_policy_version.strip():
            raise ValueError("quantization policy identity must not be blank")


@dataclass(frozen=True)
class DukascopyQuoteRecord:
    """Parsed source row. Missing source fields remain ``None``; nothing is invented."""

    timestamp: datetime
    bid_open: Decimal | None
    bid_high: Decimal | None
    bid_low: Decimal | None
    bid_close: Decimal | None
    ask_open: Decimal | None
    ask_high: Decimal | None
    ask_low: Decimal | None
    ask_close: Decimal | None
    bid_volume: Decimal | None
    ask_volume: Decimal | None
    artifact_hash: str
    row_number: int
    quantization_adjusted: bool = False
    quantization_adjustment_fields: tuple[str, ...] = ()
    quantization_raw_values: tuple[tuple[str, str], ...] = ()
    quantization_adjustment_max_ticks: int = 0

    def selected_ohlc(
        self, convention: QuoteConvention
    ) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
        values = (
            (self.bid_open, self.bid_high, self.bid_low, self.bid_close)
            if convention is QuoteConvention.BID
            else (self.ask_open, self.ask_high, self.ask_low, self.ask_close)
        )
        if any(value is None for value in values):
            return None
        return values  # type: ignore[return-value]

    def to_market_bar(
        self,
        *,
        config: DukascopyImportConfig,
        ingestion_timestamp: datetime,
    ) -> MarketBar:
        """Map the declared source side to existing canonical ``MarketBar`` fields.

        The full bid/ask OHLC remains in the immutable normalized JSONL record;
        ``MarketBar`` retains its established single-OHLC contract and receives
        only the explicitly configured, unmodified side.  ``bid`` and ``ask``
        are closing quotes when the source supplied them.
        """

        selected = self.selected_ohlc(config.quote_convention)
        if selected is None:
            raise DukascopySchemaError("configured canonical quote side is unavailable")
        volume = (
            self.bid_volume if config.quote_convention is QuoteConvention.BID else self.ask_volume
        )
        return MarketBar(
            instrument=config.instrument,
            timeframe=config.timeframe,
            adjustment_policy=AdjustmentPolicy.RAW,
            timestamp=self.timestamp,
            open=selected[0],
            high=selected[1],
            low=selected[2],
            close=selected[3],
            volume=volume,
            source="dukascopy",
            currency=config.instrument.trading_currency,
            ingestion_timestamp=ingestion_timestamp,
            bid=self.bid_close,
            ask=self.ask_close,
        )


def _normalise_header(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "time", "datetime", "gmtime"),
    "bid_open": ("bidopen",),
    "bid_high": ("bidhigh",),
    "bid_low": ("bidlow",),
    "bid_close": ("bidclose",),
    "ask_open": ("askopen",),
    "ask_high": ("askhigh",),
    "ask_low": ("asklow",),
    "ask_close": ("askclose",),
    "bid_volume": ("bidvolume", "bidvol"),
    "ask_volume": ("askvolume", "askvol"),
}


def _columns(
    fieldnames: Sequence[str] | None, convention: QuoteConvention
) -> dict[str, str | None]:
    if not fieldnames:
        raise DukascopySchemaError("CSV artifact has no header")
    normalized = {_normalise_header(value): value for value in fieldnames}
    result: dict[str, str | None] = {}
    for name, aliases in _ALIASES.items():
        result[name] = next((normalized[item] for item in aliases if item in normalized), None)
    if result["timestamp"] is None:
        raise DukascopySchemaError("CSV artifact requires a timestamp column")

    # Dukascopy-node emits a bare volume column for a selected BID download.
    # It is deliberately not mapped to ask_volume, even when ASK is selected.
    if convention is QuoteConvention.BID and result["bid_volume"] is None:
        result["bid_volume"] = normalized.get("volume")

    # A single-side CSV is acceptable only when the caller explicitly selected
    # that side.  The source field is not duplicated into the missing side.
    selected = ("bid_open", "bid_high", "bid_low", "bid_close")
    if convention is QuoteConvention.ASK:
        selected = ("ask_open", "ask_high", "ask_low", "ask_close")
    if any(result[name] is None for name in selected):
        simple = {"open": "open", "high": "high", "low": "low", "close": "close"}
        if all(key in normalized for key in simple):
            prefix = "bid" if convention is QuoteConvention.BID else "ask"
            for field, bare in simple.items():
                result[f"{prefix}_{field}"] = normalized[bare]
        else:
            raise DukascopySchemaError(
                "CSV artifact lacks the configured quote side's complete OHLC fields"
            )
    return result


def _decimal(value: str | None, *, field: str, row_number: int) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = Decimal(value.strip())
    except InvalidOperation as exc:
        raise DukascopySchemaError(f"row {row_number}: {field} is not a decimal") from exc
    # Preserve non-finite source values for the quality gate to classify and
    # block. They must not be silently converted into missing values.
    return parsed


def _epoch_milliseconds(value: str, *, row_number: int) -> datetime:
    """Convert a decimal-free Unix epoch-millisecond value to UTC exactly."""

    digits = value[1:] if value[:1] in {"+", "-"} else value
    if not digits or any(character < "0" or character > "9" for character in digits):
        raise DukascopySchemaError(
            f"row {row_number}: timestamp is not an integer epoch-millisecond value"
        )
    try:
        milliseconds = int(value, 10)
        seconds, remainder = divmod(milliseconds, 1000)
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
            seconds=seconds, microseconds=remainder * 1000
        )
    except (OverflowError, ValueError) as exc:
        raise DukascopySchemaError(
            f"row {row_number}: epoch-millisecond timestamp is outside datetime range"
        ) from exc


def _timestamp(value: str | None, config: DukascopyImportConfig, row_number: int) -> datetime:
    if value is None or not value.strip():
        raise DukascopySchemaError(f"row {row_number}: timestamp is missing")
    text = value.strip().replace("Z", "+00:00")
    if config.timestamp_format is TimestampFormat.EPOCH_MILLISECONDS:
        parsed = _epoch_milliseconds(text, row_number=row_number)
    else:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise DukascopySchemaError(f"row {row_number}: timestamp is not ISO-8601") from exc
    # ``normalize_timestamp`` rejects unresolved/ambiguous local wall times.
    try:
        normalized = normalize_timestamp(parsed, config.source_timezone)
    except TimestampNormalizationError as exc:
        raise DukascopySchemaError(f"row {row_number}: {exc}") from exc
    return (
        normalized - config.timeframe.duration
        if config.timestamp_semantics is TimestampSemantics.BAR_END
        else normalized
    )


def iter_dukascopy_csv(
    path: Path,
    *,
    config: DukascopyImportConfig,
    artifact_hash: str,
) -> Iterator[DukascopyQuoteRecord]:
    """Stream an untrusted UTF-8 CSV artifact without network or deserialization."""

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = _columns(reader.fieldnames, config.quote_convention)
        for row_number, row in enumerate(reader, start=2):
            yield DukascopyQuoteRecord(
                timestamp=_timestamp(row[columns["timestamp"]], config, row_number),
                bid_open=_decimal(
                    row[columns["bid_open"]] if columns["bid_open"] else None,
                    field="bid_open",
                    row_number=row_number,
                ),
                bid_high=_decimal(
                    row[columns["bid_high"]] if columns["bid_high"] else None,
                    field="bid_high",
                    row_number=row_number,
                ),
                bid_low=_decimal(
                    row[columns["bid_low"]] if columns["bid_low"] else None,
                    field="bid_low",
                    row_number=row_number,
                ),
                bid_close=_decimal(
                    row[columns["bid_close"]] if columns["bid_close"] else None,
                    field="bid_close",
                    row_number=row_number,
                ),
                ask_open=_decimal(
                    row[columns["ask_open"]] if columns["ask_open"] else None,
                    field="ask_open",
                    row_number=row_number,
                ),
                ask_high=_decimal(
                    row[columns["ask_high"]] if columns["ask_high"] else None,
                    field="ask_high",
                    row_number=row_number,
                ),
                ask_low=_decimal(
                    row[columns["ask_low"]] if columns["ask_low"] else None,
                    field="ask_low",
                    row_number=row_number,
                ),
                ask_close=_decimal(
                    row[columns["ask_close"]] if columns["ask_close"] else None,
                    field="ask_close",
                    row_number=row_number,
                ),
                bid_volume=_decimal(
                    row[columns["bid_volume"]] if columns["bid_volume"] else None,
                    field="bid_volume",
                    row_number=row_number,
                ),
                ask_volume=_decimal(
                    row[columns["ask_volume"]] if columns["ask_volume"] else None,
                    field="ask_volume",
                    row_number=row_number,
                ),
                artifact_hash=artifact_hash,
                row_number=row_number,
            )


def dukascopy_csv_schema(path: Path, *, convention: QuoteConvention) -> tuple[str, ...]:
    """Return a canonical accepted schema signature for cross-artifact drift checks."""

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = _columns(reader.fieldnames, convention)
    return tuple(f"{name}={columns[name] or 'unavailable'}" for name in sorted(columns))


def iter_normalized_quote_records(path: Path) -> Iterator[DukascopyQuoteRecord]:
    """Stream canonical local JSONL records without reparsing any source artifact."""

    fields = (
        "bid_open",
        "bid_high",
        "bid_low",
        "bid_close",
        "ask_open",
        "ask_high",
        "ask_low",
        "ask_close",
        "bid_volume",
        "ask_volume",
    )
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                payload = json.loads(line)
                yield DukascopyQuoteRecord(
                    timestamp=normalize_timestamp(datetime.fromisoformat(payload["timestamp"])),
                    **{
                        name: Decimal(payload[name]) if payload[name] is not None else None
                        for name in fields
                    },
                    artifact_hash=str(payload["raw_artifact_hash"]),
                    row_number=int(payload["raw_row_number"]),
                    quantization_adjusted=bool(payload.get("quantization_adjusted", False)),
                    quantization_adjustment_fields=tuple(
                        str(item) for item in payload.get("quantization_adjustment_fields", [])
                    ),
                    quantization_raw_values=tuple(
                        (str(key), str(value))
                        for key, value in payload.get("quantization_raw_values", {}).items()
                    ),
                    quantization_adjustment_max_ticks=int(
                        payload.get("quantization_adjustment_max_ticks", 0)
                    ),
                )
            except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError) as exc:
                raise DukascopySchemaError(f"normalized row {line_number} is malformed") from exc


def iter_normalized_market_bars(
    path: Path,
    *,
    config: DukascopyImportConfig,
    ingestion_timestamp: datetime,
) -> Iterator[MarketBar]:
    """Read only the local canonical JSONL output; no source website is consulted."""

    for record in iter_normalized_quote_records(path):
        yield record.to_market_bar(config=config, ingestion_timestamp=ingestion_timestamp)


__all__ = [
    "DukascopyImportConfig",
    "DukascopyQuoteRecord",
    "DukascopySchemaError",
    "QuoteConvention",
    "TimestampFormat",
    "TimestampSemantics",
    "dukascopy_csv_schema",
    "iter_dukascopy_csv",
    "iter_normalized_market_bars",
    "iter_normalized_quote_records",
]
