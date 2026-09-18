"""Offline Twelve Data Forex OHLCV CSV import boundary.

The caller acquires and preserves the CSV outside TRADEROS.  This module only
inspects local bytes, validates the declared Twelve Data schema, and emits the
provider-neutral empirical bar record consumed by the common admission gate.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from traderos.data.calendars import MarketCalendar
from traderos.data.empirical import (
    AdmissionPolicy,
    DatasetAdmission,
    EmpiricalOHLCVRecord,
    OHLCVAdmissionConfig,
    RawArtifactMetadata,
    admit_ohlcv_csv,
)
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.temporal import TimestampFormat, TimestampSemantics
from traderos.data.timeframes import Timeframe


class TwelveDataSchemaError(ValueError):
    """Raised when a local Twelve Data artifact is malformed or drifts."""


@dataclass(frozen=True)
class TwelveDataImportConfig:
    """Explicit Twelve Data interpretation; no source convention is inferred."""

    instrument: Instrument
    timeframe: Timeframe = Timeframe.M15
    source_symbol: str = "EUR/USD"
    source_timezone: str = "UTC"
    interval: str = "15min"
    timestamp_format: TimestampFormat = TimestampFormat.ISO_8601
    timestamp_semantics: TimestampSemantics = TimestampSemantics.BAR_START
    source_version: str | None = None
    volume_semantics: str = "source-provided Forex volume"

    def __post_init__(self) -> None:
        if self.instrument.asset_class is not AssetClass.FOREX:
            raise ValueError("Twelve Data Forex import requires a Forex instrument")
        if self.timeframe is not Timeframe.M15 or self.interval != "15min":
            raise ValueError("Twelve Data Forex admission is limited to the 15min interval")
        if self.source_symbol != "EUR/USD":
            raise ValueError("the governed Twelve Data adapter is limited to EUR/USD")
        if self.source_timezone != "UTC":
            raise ValueError("Twelve Data import requires explicit UTC timezone")
        if self.timestamp_format is not TimestampFormat.ISO_8601:
            raise ValueError("Twelve Data import requires ISO_8601 timestamps")
        if self.timestamp_semantics is not TimestampSemantics.BAR_START:
            raise ValueError("Twelve Data import requires BAR_START timestamps")
        if not self.volume_semantics.strip():
            raise ValueError("volume semantics must not be blank")

    def admission_config(self) -> OHLCVAdmissionConfig:
        return OHLCVAdmissionConfig(
            instrument=self.instrument,
            timeframe=self.timeframe,
            source="twelve_data",
            source_symbol=self.source_symbol,
            source_timezone=self.source_timezone,
            timestamp_format=self.timestamp_format,
            timestamp_semantics=self.timestamp_semantics,
            volume_semantics=self.volume_semantics,
            source_version=self.source_version,
            artifact_timeframe=self.interval,
        )


_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("datetime", "timestamp", "time"),
    "open": ("open",),
    "high": ("high",),
    "low": ("low",),
    "close": ("close",),
    "volume": ("volume", "vol"),
}


def _normalise_header(value: str) -> str:
    return "".join(character for character in value.lstrip("\ufeff").lower() if character.isalnum())


def _columns(fieldnames: Sequence[str] | None) -> dict[str, str | None]:
    if not fieldnames or any(not value.strip() for value in fieldnames):
        raise TwelveDataSchemaError("CSV artifact has no usable header")
    normalized: dict[str, str] = {}
    for value in fieldnames:
        key = _normalise_header(value)
        if key in normalized:
            raise TwelveDataSchemaError(f"CSV header has duplicate column {value!r}")
        normalized[key] = value
    result = {
        name: next((normalized[alias] for alias in aliases if alias in normalized), None)
        for name, aliases in _ALIASES.items()
    }
    required = ("timestamp", "open", "high", "low", "close")
    missing = tuple(name for name in required if result[name] is None)
    if missing:
        raise TwelveDataSchemaError(
            "Twelve Data CSV requires columns: " + ", ".join(required) + f"; missing {missing}"
        )
    return result


def twelve_data_csv_schema(path: Path) -> tuple[str, ...]:
    """Return the exact normalized header signature used for drift detection."""

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = _columns(reader.fieldnames)
        assert reader.fieldnames is not None
        header = tuple(_normalise_header(value) for value in reader.fieldnames)
        return tuple(
            [f"header={','.join(header)}"]
            + [f"{name}={columns[name] or 'unavailable'}" for name in sorted(columns)]
        )


def _timestamp(value: str | None, *, row_number: int, config: TwelveDataImportConfig) -> datetime:
    if value is None or not value.strip():
        raise TwelveDataSchemaError(f"row {row_number}: datetime is missing")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise TwelveDataSchemaError(f"row {row_number}: datetime is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TwelveDataSchemaError(
            f"row {row_number}: datetime must be timezone-aware UTC ISO-8601"
        )
    if parsed.utcoffset() != timedelta(0):
        raise TwelveDataSchemaError(f"row {row_number}: datetime must use UTC offset")
    return parsed.astimezone(UTC)


def _decimal(value: str | None, *, field: str, row_number: int, required: bool) -> Decimal | None:
    if value is None or not value.strip():
        if required:
            raise TwelveDataSchemaError(f"row {row_number}: {field} is missing")
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation as exc:
        raise TwelveDataSchemaError(f"row {row_number}: {field} is not a decimal") from exc


def _required_decimal(value: str | None, *, field: str, row_number: int) -> Decimal:
    parsed = _decimal(value, field=field, row_number=row_number, required=True)
    assert parsed is not None
    return parsed


def iter_twelve_data_csv(
    path: Path,
    *,
    config: TwelveDataImportConfig,
    artifact_hash: str,
) -> Iterator[EmpiricalOHLCVRecord]:
    """Stream a caller-supplied CSV without network or unsafe deserialization."""

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = _columns(reader.fieldnames)
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise TwelveDataSchemaError(f"row {row_number}: unexpected extra CSV fields")
            yield EmpiricalOHLCVRecord(
                timestamp=_timestamp(
                    row.get(columns["timestamp"]), row_number=row_number, config=config
                ),
                open=_required_decimal(
                    row.get(columns["open"]), field="open", row_number=row_number
                ),
                high=_required_decimal(
                    row.get(columns["high"]), field="high", row_number=row_number
                ),
                low=_required_decimal(
                    row.get(columns["low"]), field="low", row_number=row_number
                ),
                close=_required_decimal(
                    row.get(columns["close"]), field="close", row_number=row_number
                ),
                volume=_decimal(
                    row.get(columns["volume"]) if columns["volume"] else None,
                    field="volume",
                    row_number=row_number,
                    required=False,
                ),
                artifact_hash=artifact_hash,
                row_number=row_number,
            )


def admit_twelve_data_csv(
    *,
    raw_paths: tuple[Path, ...] | list[Path],
    raw_metadata: tuple[RawArtifactMetadata, ...] | list[RawArtifactMetadata],
    config: TwelveDataImportConfig,
    calendar: MarketCalendar,
    policy: AdmissionPolicy,
    normalized_dir: Path,
    manifest_dir: Path,
    created_at: datetime,
    deterministic_test_fixture: bool = False,
) -> DatasetAdmission:
    """Run the provider-neutral admission gate for Twelve Data CSV artifacts."""

    return admit_ohlcv_csv(
        raw_paths=raw_paths,
        raw_metadata=raw_metadata,
        config=config.admission_config(),
        calendar=calendar,
        policy=policy,
        normalized_dir=normalized_dir,
        manifest_dir=manifest_dir,
        created_at=created_at,
        parser=lambda path, artifact_hash: iter_twelve_data_csv(
            path, config=config, artifact_hash=artifact_hash
        ),
        schema_reader=twelve_data_csv_schema,
        deterministic_test_fixture=deterministic_test_fixture,
    )


def instrument_eurusd() -> Instrument:
    """Return the governed provider-neutral EUR/USD instrument mapping."""

    return Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        provider_symbols={"twelve_data": "EUR/USD"},
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
    )


__all__ = [
    "TwelveDataImportConfig",
    "TwelveDataSchemaError",
    "admit_twelve_data_csv",
    "instrument_eurusd",
    "iter_twelve_data_csv",
    "twelve_data_csv_schema",
]
