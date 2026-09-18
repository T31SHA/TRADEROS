"""Immutable local historical-dataset admission workflow.

The workflow is intentionally offline and does not invoke the research engine.
It preserves raw bytes, streams source rows into a canonical local JSONL file,
audits their integrity, then writes a content-addressed manifest.  A caller may
subsequently use the existing Phase 9 qualification APIs with the exact
``dataset_id`` recorded here.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from pathlib import Path
from statistics import median
from typing import Protocol

from traderos.data.calendars import MarketCalendar
from traderos.data.dukascopy import (
    DukascopyImportConfig,
    DukascopyQuoteRecord,
    DukascopySchemaError,
    QuoteConvention,
    dukascopy_csv_schema,
    iter_dukascopy_csv,
    iter_normalized_quote_records,
)
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.temporal import TimestampFormat, TimestampSemantics
from traderos.data.ticks import (
    DukascopyTickImportConfig,
    TickAggregationPolicy,
    TickAggregator,
    TickM15Bar,
    TickSchemaError,
    TickValidationReport,
    iter_dukascopy_ticks,
)
from traderos.data.time import require_utc
from traderos.data.timeframes import Timeframe

CANONICAL_SCHEMA_VERSION = "empirical-forex-jsonl-v2"
INGESTION_VERSION = "dukascopy-local-import-v3"
OHLCV_CANONICAL_SCHEMA_VERSION = "empirical-forex-ohlcv-jsonl-v1"
OHLCV_INGESTION_VERSION = "provider-neutral-ohlcv-local-import-v1"


class GapClassification(StrEnum):
    EXPECTED_MARKET_CLOSURE = "expected_market_closure"
    DATA_GAP = "data_gap"
    UNKNOWN = "unknown"


class AdmissionState(StrEnum):
    DETERMINISTIC_TEST_FIXTURE = "deterministic_test_fixture"
    EMPIRICALLY_QUALIFIED_DATASET = "empirically_qualified_dataset"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class EmpiricalOHLCVRecord:
    """Provider-neutral bar record emitted by a source-specific parser."""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    artifact_hash: str
    row_number: int

    def canonical(self) -> dict[str, str | int | None]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume) if self.volume is not None else None,
            "raw_artifact_hash": self.artifact_hash,
            "raw_row_number": self.row_number,
        }


@dataclass(frozen=True)
class OHLCVAdmissionConfig:
    """Provider-neutral interpretation for a local OHLCV artifact."""

    instrument: Instrument
    timeframe: Timeframe
    source: str
    source_symbol: str
    source_timezone: str
    timestamp_format: TimestampFormat
    timestamp_semantics: TimestampSemantics
    volume_semantics: str
    source_version: str | None = None
    artifact_timeframe: str | None = None

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.source_symbol.strip():
            raise ValueError("source and source symbol must not be blank")
        if not self.source_timezone.strip() or not self.volume_semantics.strip():
            raise ValueError("source timezone and volume semantics must not be blank")


@dataclass(frozen=True)
class RawArtifactMetadata:
    """Required append-only source identity, including operational provenance."""

    source: str
    source_symbol: str
    requested_start: datetime
    requested_end: datetime
    requested_timeframe: str
    timezone: str
    original_filename: str
    download_timestamp: datetime
    sha256: str
    byte_size: int
    source_version: str | None = None

    def __post_init__(self) -> None:
        require_utc(self.requested_start)
        require_utc(self.requested_end)
        require_utc(self.download_timestamp)
        if self.requested_start >= self.requested_end:
            raise ValueError("artifact requested start must precede requested end")
        if not all(
            value.strip()
            for value in (
                self.source,
                self.source_symbol,
                self.requested_timeframe,
                self.timezone,
                self.original_filename,
                self.sha256,
            )
        ):
            raise ValueError("raw artifact identity fields must not be blank")
        valid_hex = all(character in "0123456789abcdef" for character in self.sha256)
        if len(self.sha256) != 64 or not valid_hex:
            raise ValueError("raw artifact SHA-256 must be lowercase hexadecimal")
        if self.byte_size < 0:
            raise ValueError("raw artifact byte size must not be negative")

    def canonical(self) -> dict[str, object]:
        values = asdict(self)
        for key in ("requested_start", "requested_end", "download_timestamp"):
            values[key] = values[key].isoformat()
        return values


@dataclass(frozen=True)
class TickRawArtifactMetadata:
    """Identity and provenance for one immutable raw tick artifact."""

    source: str
    source_symbol: str
    coverage_start: datetime
    coverage_end: datetime
    timezone: str
    original_filename: str
    sha256: str
    byte_size: int
    download_timestamp: datetime
    source_version: str | None = None

    def __post_init__(self) -> None:
        require_utc(self.coverage_start)
        require_utc(self.coverage_end)
        require_utc(self.download_timestamp)
        if self.coverage_start >= self.coverage_end:
            raise ValueError("tick artifact coverage_start must precede coverage_end")
        if not all(
            value.strip()
            for value in (
                self.source,
                self.source_symbol,
                self.timezone,
                self.original_filename,
                self.sha256,
            )
        ):
            raise ValueError("tick artifact identity fields must not be blank")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("tick artifact SHA-256 must be lowercase hexadecimal")
        if self.byte_size < 0:
            raise ValueError("tick artifact byte size must not be negative")

    def canonical(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_symbol": self.source_symbol,
            "coverage_start": self.coverage_start.isoformat(),
            "coverage_end": self.coverage_end.isoformat(),
            "timezone": self.timezone,
            "original_filename": self.original_filename,
            "sha256": self.sha256,
            "byte_size": self.byte_size,
            "download_timestamp": self.download_timestamp.isoformat(),
            "source_version": self.source_version,
        }


TickArtifactMetadata = TickRawArtifactMetadata


@dataclass(frozen=True)
class AdmissionPolicy:
    """Versioned explicit rejection tolerances; no universal threshold is implied."""

    policy_id: str = "forex-admission"
    policy_version: str = "v1"
    max_duplicate_timestamps: int = 0
    max_non_monotonic_timestamps: int = 0
    max_invalid_ohlc: int = 0
    max_crossed_quotes: int = 0
    max_nonpositive_prices: int = 0
    max_volume_anomalies: int = 0
    max_unexpected_gaps: int = 0
    max_schema_drift: int = 0
    max_suspicious_jumps: int | None = None
    stale_sequence_length: int = 8
    suspicious_jump_fraction: Decimal = Decimal("0.20")
    require_bid_ask: bool = False
    max_active_session_zero_volume: int = 0
    max_invalid_ticks: int = 0
    require_volume: bool = False

    def __post_init__(self) -> None:
        if not self.policy_id.strip() or not self.policy_version.strip():
            raise ValueError("admission policy identity must not be blank")
        if self.stale_sequence_length < 2 or self.suspicious_jump_fraction <= 0:
            raise ValueError("admission policy values must be positive")
        values = (
            self.max_duplicate_timestamps,
            self.max_non_monotonic_timestamps,
            self.max_invalid_ohlc,
            self.max_crossed_quotes,
            self.max_nonpositive_prices,
            self.max_volume_anomalies,
            self.max_unexpected_gaps,
            self.max_schema_drift,
            self.max_active_session_zero_volume,
            self.max_invalid_ticks,
        )
        if any(value < 0 for value in values):
            raise ValueError("admission tolerances must be non-negative")

    @property
    def identity(self) -> str:
        return _hash_json(
            {
                "policy_id": self.policy_id,
                "policy_version": self.policy_version,
                "max_duplicate_timestamps": self.max_duplicate_timestamps,
                "max_non_monotonic_timestamps": self.max_non_monotonic_timestamps,
                "max_invalid_ohlc": self.max_invalid_ohlc,
                "max_crossed_quotes": self.max_crossed_quotes,
                "max_nonpositive_prices": self.max_nonpositive_prices,
                "max_volume_anomalies": self.max_volume_anomalies,
                "max_unexpected_gaps": self.max_unexpected_gaps,
                "max_schema_drift": self.max_schema_drift,
                "max_suspicious_jumps": self.max_suspicious_jumps,
                "stale_sequence_length": self.stale_sequence_length,
                "suspicious_jump_fraction": str(self.suspicious_jump_fraction),
                "require_bid_ask": self.require_bid_ask,
                "max_active_session_zero_volume": self.max_active_session_zero_volume,
                "max_invalid_ticks": self.max_invalid_ticks,
                "require_volume": self.require_volume,
            }
        )


@dataclass(frozen=True)
class DatasetQualityReport:
    """Deterministic audit summary; observations are classified, never repaired."""

    row_count: int
    coverage_start: datetime | None
    coverage_end: datetime | None
    duplicate_count: int
    non_monotonic_count: int
    invalid_ohlc_count: int
    crossed_bid_ask_count: int
    nonpositive_price_count: int
    volume_anomaly_count: int
    missing_intervals: int
    expected_closures: int
    unexpected_gaps: int
    unknown_gaps: int
    stale_sequences: int
    suspicious_jumps: int
    spread_count: int
    min_spread: Decimal | None
    max_spread: Decimal | None
    median_spread: Decimal | None
    p95_spread: Decimal | None
    p99_spread: Decimal | None
    schema_drift_count: int
    conflicting_overlap_count: int
    quote_side_complete_count: int
    calendar_id: str
    invalid_ohlc_before_normalization_count: int = 0
    invalid_ohlc_after_normalization_count: int = 0
    quantization_adjustment_count: int = 0
    quantization_adjustment_rows: tuple[int, ...] = ()
    quantization_adjustment_max_ticks: int = 0
    nonfinite_price_count: int = 0
    zero_volume_bar_count: int = 0
    active_session_zero_volume_count: int = 0
    expected_closure_bar_count: int = 0
    unknown_zero_volume_count: int = 0
    flat_bar_count: int = 0
    flat_zero_volume_count: int = 0
    raw_row_count: int = 0
    normalized_row_count: int = 0
    volume_unavailable_count: int = 0

    def canonical(self) -> dict[str, object]:
        values = asdict(self)
        for key in ("coverage_start", "coverage_end"):
            value = values[key]
            values[key] = value.isoformat() if value is not None else None
        for key in ("min_spread", "max_spread", "median_spread", "p95_spread", "p99_spread"):
            value = values[key]
            values[key] = str(value) if value is not None else None
        values["quantization_adjustment_rows"] = list(values["quantization_adjustment_rows"])
        return values

    @property
    def hash(self) -> str:
        return _hash_json(self.canonical())


@dataclass(frozen=True)
class EmpiricalDatasetManifest:
    """Machine-readable immutable identity for a locally normalized historical dataset."""

    dataset_id: str
    dataset_hash: str
    source: str
    source_artifact_hashes: tuple[str, ...]
    instrument: str
    asset_class: str
    timeframe: str
    coverage_start: datetime | None
    coverage_end: datetime | None
    timestamp_format: TimestampFormat
    timestamp_semantics: TimestampSemantics
    timezone: str
    quote_convention: QuoteConvention | None
    adjustment_policy: AdjustmentPolicy
    calendar_id: str
    quality_report_hash: str
    ingestion_version: str
    canonical_schema_version: str
    created_at: datetime
    artifact_metadata: tuple[RawArtifactMetadata, ...]
    policy_identity: str
    normalized_content_hash: str
    price_tick_size: Decimal | None = None
    quantization_policy_id: str = "dukascopy-fixed-price-quantization"
    quantization_policy_version: str = "v1"
    source_mode: str | None = None
    price_sides: str | None = None
    volume_sides: str | None = None
    volume_semantics: str | None = None
    quote_configuration: str | None = None

    def canonical(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_hash": self.dataset_hash,
            "source": self.source,
            "source_artifact_hashes": list(self.source_artifact_hashes),
            "instrument": self.instrument,
            "asset_class": self.asset_class,
            "timeframe": self.timeframe,
            "coverage_start": self.coverage_start.isoformat() if self.coverage_start else None,
            "coverage_end": self.coverage_end.isoformat() if self.coverage_end else None,
            "timestamp_format": self.timestamp_format.value,
            "timestamp_semantics": self.timestamp_semantics.value,
            "timezone": self.timezone,
            "quote_convention": (
                self.quote_convention.value if self.quote_convention is not None else None
            ),
            "adjustment_policy": self.adjustment_policy.value,
            "calendar_id": self.calendar_id,
            "quality_report_hash": self.quality_report_hash,
            "ingestion_version": self.ingestion_version,
            "canonical_schema_version": self.canonical_schema_version,
            "created_at": self.created_at.isoformat(),
            "artifact_metadata": [item.canonical() for item in self.artifact_metadata],
            "policy_identity": self.policy_identity,
            "normalized_content_hash": self.normalized_content_hash,
            "price_tick_size": (
                str(self.price_tick_size) if self.price_tick_size is not None else None
            ),
            "quantization_policy_id": self.quantization_policy_id,
            "quantization_policy_version": self.quantization_policy_version,
            "source_mode": self.source_mode,
            "price_sides": self.price_sides,
            "volume_sides": self.volume_sides,
            "volume_semantics": self.volume_semantics,
            "quote_configuration": self.quote_configuration,
        }


@dataclass(frozen=True)
class DatasetAdmission:
    """The admission decision only; it intentionally contains no performance metrics."""

    manifest: EmpiricalDatasetManifest
    quality_report: DatasetQualityReport
    state: AdmissionState
    blockers: tuple[str, ...]
    normalized_path: Path
    manifest_path: Path
    quality_report_path: Path


def _hash_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: Path) -> tuple[str, int]:
    """Hash raw bytes in chunks, preserving a byte-for-byte source identity."""

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class RawArtifactStore:
    """Content-addressed raw artifact archive. Existing bytes are never overwritten."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def preserve(
        self,
        source_path: Path,
        *,
        source: str,
        source_symbol: str,
        requested_start: datetime,
        requested_end: datetime,
        requested_timeframe: str,
        timezone: str,
        download_timestamp: datetime,
        source_version: str | None = None,
    ) -> tuple[Path, RawArtifactMetadata]:
        require_utc(requested_start)
        require_utc(requested_end)
        require_utc(download_timestamp)
        digest, size = sha256_file(source_path)
        metadata = RawArtifactMetadata(
            source=source,
            source_symbol=source_symbol,
            requested_start=requested_start,
            requested_end=requested_end,
            requested_timeframe=requested_timeframe,
            timezone=timezone,
            original_filename=source_path.name,
            download_timestamp=download_timestamp,
            sha256=digest,
            byte_size=size,
            source_version=source_version,
        )
        target = self._root / source / digest / source_path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing_digest, existing_size = sha256_file(target)
            if existing_digest != digest or existing_size != size:
                raise RuntimeError("immutable raw artifact path has conflicting bytes")
        else:
            _copy_exclusive(source_path, target)
        metadata_path = target.with_suffix(target.suffix + ".metadata.json")
        _write_immutable_json(metadata_path, metadata.canonical())
        return target, metadata

    def preserve_tick(
        self,
        source_path: Path,
        *,
        source: str,
        source_symbol: str,
        coverage_start: datetime,
        coverage_end: datetime,
        timezone: str,
        download_timestamp: datetime,
        source_version: str | None = None,
    ) -> tuple[Path, TickRawArtifactMetadata]:
        """Preserve raw tick bytes without interpreting or overwriting them."""

        digest, size = sha256_file(source_path)
        metadata = TickRawArtifactMetadata(
            source=source,
            source_symbol=source_symbol,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            timezone=timezone,
            original_filename=source_path.name,
            sha256=digest,
            byte_size=size,
            download_timestamp=download_timestamp,
            source_version=source_version,
        )
        target = self._root / source / digest / source_path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing_digest, existing_size = sha256_file(target)
            if existing_digest != digest or existing_size != size:
                raise RuntimeError("immutable raw artifact path has conflicting bytes")
        else:
            _copy_exclusive(source_path, target)
        metadata_path = target.with_suffix(target.suffix + ".metadata.json")
        _write_immutable_json(metadata_path, metadata.canonical())
        return target, metadata


def _copy_exclusive(source: Path, destination: Path) -> None:
    try:
        with source.open("rb") as source_handle, destination.open("xb") as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
    except FileExistsError:
        return


def _write_immutable_json(path: Path, value: object) -> None:
    encoded = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
    except FileExistsError as exc:
        if path.read_bytes() != encoded:
            raise RuntimeError(
                f"immutable artifact already exists with different content: {path}"
            ) from exc


class _TextWriter(Protocol):
    def write(self, text: str) -> int: ...


def _ohlc_values(record: DukascopyQuoteRecord, prefix: str) -> tuple[Decimal | None, ...]:
    return tuple(getattr(record, f"{prefix}_{field}") for field in ("open", "high", "low", "close"))


def _all_prices_finite_positive(record: DukascopyQuoteRecord) -> bool:
    return all(
        value is None or (value.is_finite() and value > 0)
        for prefix in ("bid", "ask")
        for value in _ohlc_values(record, prefix)
    )


def _normalise_one_tick_side(
    record: DukascopyQuoteRecord,
    *,
    prefix: str,
    tick_size: Decimal,
) -> tuple[DukascopyQuoteRecord, tuple[str, ...], int] | None:
    values = _ohlc_values(record, prefix)
    if any(value is None for value in values):
        return record, (), 0
    opening, high, low, close = values
    assert opening is not None and high is not None and low is not None and close is not None
    if _ohlc_valid((opening, high, low, close)):
        return record, (), 0

    high_required = max(opening, close, low)
    low_required = min(opening, close, high)
    high_inversion = high < high_required
    low_inversion = low > low_required
    if high_inversion == low_inversion:
        return None

    if high_inversion:
        correction = high_required - high
        if correction <= 0 or correction > tick_size:
            return None
        corrected = (
            replace(record, bid_high=high_required)
            if prefix == "bid"
            else replace(record, ask_high=high_required)
        )
        changed_field = f"{prefix}_high"
    else:
        correction = low - low_required
        if correction <= 0 or correction > tick_size:
            return None
        corrected = (
            replace(record, bid_low=low_required)
            if prefix == "bid"
            else replace(record, ask_low=low_required)
        )
        changed_field = f"{prefix}_low"

    corrected_values = _ohlc_values(corrected, prefix)
    if any(value is None for value in corrected_values):
        return None
    corrected_tuple = tuple(corrected_values)
    assert all(value is not None for value in corrected_tuple)
    if not _ohlc_valid(corrected_tuple):  # type: ignore[arg-type]
        return None
    return corrected, (changed_field,), 1


def _quantization_normalise(
    record: DukascopyQuoteRecord,
    *,
    config: DukascopyImportConfig,
) -> DukascopyQuoteRecord:
    """Apply only a single, source-configured one-tick ordering correction.

    A source row is rejected by the quality gate when it has non-finite or
    non-positive prices, more than one independent side inversion, or an
    inversion larger than the configured source tick. The raw row remains
    available through its immutable artifact hash and row number.
    """

    if config.price_tick_size is None or not _all_prices_finite_positive(record):
        return record
    candidates: list[tuple[DukascopyQuoteRecord, tuple[str, ...], int]] = []
    for prefix in ("bid", "ask"):
        candidate = _normalise_one_tick_side(
            record,
            prefix=prefix,
            tick_size=config.price_tick_size,
        )
        if candidate is None:
            # An invalid side is intentionally left unchanged so the audit
            # records the post-normalization blocker.
            values = _ohlc_values(record, prefix)
            if all(value is not None for value in values) and not _ohlc_valid(values):  # type: ignore[arg-type]
                return record
            continue
        if candidate[1]:
            candidates.append(candidate)
    if len(candidates) != 1:
        return record

    corrected, fields, max_ticks = candidates[0]
    raw_values = tuple(
        (name, str(value))
        for name, value in zip(
            (
                "bid_open",
                "bid_high",
                "bid_low",
                "bid_close",
                "ask_open",
                "ask_high",
                "ask_low",
                "ask_close",
            ),
            (
                record.bid_open,
                record.bid_high,
                record.bid_low,
                record.bid_close,
                record.ask_open,
                record.ask_high,
                record.ask_low,
                record.ask_close,
            ),
            strict=True,
        )
        if value is not None
    )
    return replace(
        corrected,
        quantization_adjusted=True,
        quantization_adjustment_fields=fields,
        quantization_raw_values=raw_values,
        quantization_adjustment_max_ticks=max_ticks,
    )


def _write_record(handle: _TextWriter, record: DukascopyQuoteRecord) -> str:
    value = {
        "timestamp": record.timestamp.isoformat(),
        "bid_open": str(record.bid_open) if record.bid_open is not None else None,
        "bid_high": str(record.bid_high) if record.bid_high is not None else None,
        "bid_low": str(record.bid_low) if record.bid_low is not None else None,
        "bid_close": str(record.bid_close) if record.bid_close is not None else None,
        "ask_open": str(record.ask_open) if record.ask_open is not None else None,
        "ask_high": str(record.ask_high) if record.ask_high is not None else None,
        "ask_low": str(record.ask_low) if record.ask_low is not None else None,
        "ask_close": str(record.ask_close) if record.ask_close is not None else None,
        "bid_volume": str(record.bid_volume) if record.bid_volume is not None else None,
        "ask_volume": str(record.ask_volume) if record.ask_volume is not None else None,
        "raw_artifact_hash": record.artifact_hash,
        "raw_row_number": record.row_number,
        "quantization_adjusted": record.quantization_adjusted,
        "quantization_adjustment_fields": list(record.quantization_adjustment_fields),
        "quantization_raw_values": dict(record.quantization_raw_values),
        "quantization_adjustment_max_ticks": record.quantization_adjustment_max_ticks,
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    handle.write(encoded)
    return encoded


def _ohlc_valid(values: tuple[Decimal, Decimal, Decimal, Decimal]) -> bool:
    opening, high, low, close = values
    return high >= max(opening, close, low) and low <= min(opening, close, high)


def _percentile(values: Sequence[Decimal], fraction: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    index = int((len(ordered) - 1) * float(fraction))
    return ordered[index]


def _blockers(
    report: DatasetQualityReport,
    policy: AdmissionPolicy,
    *,
    config: DukascopyImportConfig,
    artifacts: Sequence[RawArtifactMetadata],
    calendar: MarketCalendar,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if not artifacts:
        blockers.append("artifact_not_supplied")
    if not report.row_count or report.coverage_start is None or report.coverage_end is None:
        blockers.append("coverage_missing")
    checks = (
        (report.duplicate_count, policy.max_duplicate_timestamps, "duplicate_timestamps"),
        (
            report.non_monotonic_count,
            policy.max_non_monotonic_timestamps,
            "non_monotonic_timestamps",
        ),
        (report.invalid_ohlc_count, policy.max_invalid_ohlc, "material_ohlc_corruption"),
        (report.crossed_bid_ask_count, policy.max_crossed_quotes, "crossed_market_corruption"),
        (report.nonpositive_price_count, policy.max_nonpositive_prices, "nonpositive_prices"),
        (report.nonfinite_price_count, 0, "nonfinite_prices"),
        (report.volume_anomaly_count, policy.max_volume_anomalies, "volume_anomalies"),
        (
            report.active_session_zero_volume_count,
            policy.max_active_session_zero_volume,
            "active_session_zero_volume",
        ),
        (report.unexpected_gaps, policy.max_unexpected_gaps, "unexpected_data_gaps"),
        (report.schema_drift_count, policy.max_schema_drift, "schema_drift"),
    )
    blockers.extend(name for actual, allowed, name in checks if actual > allowed)
    if (
        policy.max_suspicious_jumps is not None
        and report.suspicious_jumps > policy.max_suspicious_jumps
    ):
        blockers.append("suspicious_jumps")
    if policy.require_bid_ask and report.quote_side_complete_count != report.row_count:
        blockers.append("bid_ask_unavailable")
    if report.conflicting_overlap_count:
        blockers.append("conflicting_overlapping_records")
    if config.instrument.asset_class is not AssetClass.FOREX or calendar.calendar_id not in {
        "forex-weekday-utc-v1",
        "dukascopy-forex-utc-session-v1",
    }:
        blockers.append("unknown_or_incompatible_calendar")
    return tuple(sorted(set(blockers)))


def admit_dukascopy_csv(
    *,
    raw_paths: Iterable[Path],
    raw_metadata: Iterable[RawArtifactMetadata],
    config: DukascopyImportConfig,
    calendar: MarketCalendar,
    policy: AdmissionPolicy,
    normalized_dir: Path,
    manifest_dir: Path,
    created_at: datetime,
    deterministic_test_fixture: bool = False,
) -> DatasetAdmission:
    """Stream locally supplied artifacts through normalization, audit, and admission.

    ``raw_paths`` must already have been preserved by :class:`RawArtifactStore`.
    Keeping acquisition separate from import makes research independent of a
    vendor website and ensures the importer cannot overwrite original bytes.
    """

    require_utc(created_at)
    paths = tuple(raw_paths)
    artifacts = tuple(raw_metadata)
    if len(paths) != len(artifacts):
        raise ValueError("each import path requires exactly one raw metadata record")
    if not paths:
        raise ValueError("artifact not supplied")
    if any(item.source != "dukascopy" for item in artifacts):
        raise ValueError("Dukascopy import requires Dukascopy raw artifact metadata")
    if any(item.requested_timeframe != config.timeframe.value for item in artifacts):
        raise ValueError("raw artifact timeframe does not match import configuration")
    if any(item.source_symbol != config.source_symbol for item in artifacts):
        raise ValueError("raw artifact symbol does not match import configuration")
    if any(item.timezone != config.source_timezone for item in artifacts):
        raise ValueError("raw artifact timezone does not match import configuration")
    if config.source_version and any(
        item.source_version != config.source_version for item in artifacts
    ):
        raise ValueError("raw artifact source version does not match import configuration")

    normalized_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=normalized_dir
    ) as staged:
        staged_path = Path(staged.name)
        hasher = hashlib.sha256()
        try:
            for path, metadata in zip(paths, artifacts, strict=True):
                actual_hash, actual_size = sha256_file(path)
                if actual_hash != metadata.sha256 or actual_size != metadata.byte_size:
                    raise ValueError("raw artifact bytes no longer match preserved metadata")
                records = iter_dukascopy_csv(path, config=config, artifact_hash=metadata.sha256)
                for record in records:
                    normalized_record = _quantization_normalise(record, config=config)
                    text = _write_record(staged, normalized_record)
                    hasher.update(text.encode())
        except (DukascopySchemaError, OSError):
            staged.close()
            staged_path.unlink(missing_ok=True)
            raise

    schemas = {dukascopy_csv_schema(path, convention=config.quote_convention) for path in paths}
    report = _audit(
        iter_normalized_quote_records(staged_path),
        config=config,
        calendar=calendar,
        policy=policy,
        schema_drift_count=max(0, len(schemas) - 1),
    )
    quality_hash = report.hash
    material = {
        "source": "dukascopy",
        "source_artifacts": [item.canonical() for item in artifacts],
        "instrument": config.instrument.canonical_symbol,
        "asset_class": config.instrument.asset_class.value,
        "timeframe": config.timeframe.value,
        "timestamp_format": config.timestamp_format.value,
        "timestamp_semantics": config.timestamp_semantics.value,
        "timezone": config.source_timezone,
        "quote_convention": config.quote_convention.value,
        "adjustment_policy": AdjustmentPolicy.RAW.value,
        "calendar_id": calendar.calendar_id,
        "quality_report_hash": quality_hash,
        "policy_identity": policy.identity,
        "price_tick_size": (
            str(config.price_tick_size) if config.price_tick_size is not None else None
        ),
        "quantization_policy_id": config.quantization_policy_id,
        "quantization_policy_version": config.quantization_policy_version,
        "ingestion_version": INGESTION_VERSION,
        "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
        "normalized_content_hash": hasher.hexdigest(),
    }
    dataset_hash = _hash_json(material)
    manifest = EmpiricalDatasetManifest(
        dataset_id=dataset_hash,
        dataset_hash=dataset_hash,
        source="dukascopy",
        source_artifact_hashes=tuple(item.sha256 for item in artifacts),
        instrument=config.instrument.canonical_symbol,
        asset_class=config.instrument.asset_class.value,
        timeframe=config.timeframe.value,
        coverage_start=report.coverage_start,
        coverage_end=report.coverage_end,
        timestamp_format=config.timestamp_format,
        timestamp_semantics=config.timestamp_semantics,
        timezone=config.source_timezone,
        quote_convention=config.quote_convention,
        adjustment_policy=AdjustmentPolicy.RAW,
        calendar_id=calendar.calendar_id,
        quality_report_hash=quality_hash,
        ingestion_version=INGESTION_VERSION,
        canonical_schema_version=CANONICAL_SCHEMA_VERSION,
        created_at=created_at,
        artifact_metadata=artifacts,
        policy_identity=policy.identity,
        normalized_content_hash=hasher.hexdigest(),
        price_tick_size=config.price_tick_size,
        quantization_policy_id=config.quantization_policy_id,
        quantization_policy_version=config.quantization_policy_version,
    )
    normalized_path = normalized_dir / f"{dataset_hash}.jsonl"
    if normalized_path.exists():
        if sha256_file(normalized_path)[0] != hasher.hexdigest():
            raise RuntimeError("immutable normalized dataset path has conflicting content")
        staged_path.unlink(missing_ok=True)
    else:
        os.replace(staged_path, normalized_path)
    report_path = manifest_dir / f"{dataset_hash}.quality.json"
    manifest_path = manifest_dir / f"{dataset_hash}.json"
    _write_immutable_json(report_path, report.canonical())
    _write_immutable_json(manifest_path, manifest.canonical())
    blockers = _blockers(
        report,
        policy,
        config=config,
        artifacts=artifacts,
        calendar=calendar,
    )
    state = (
        AdmissionState.DETERMINISTIC_TEST_FIXTURE
        if deterministic_test_fixture
        else AdmissionState.BLOCKED
        if blockers
        else AdmissionState.EMPIRICALLY_QUALIFIED_DATASET
    )
    return DatasetAdmission(
        manifest,
        report,
        state,
        blockers,
        normalized_path,
        manifest_path,
        report_path,
    )


def _audit(
    rows: Iterable[DukascopyQuoteRecord],
    *,
    config: DukascopyImportConfig,
    calendar: MarketCalendar,
    policy: AdmissionPolicy,
    schema_drift_count: int,
) -> DatasetQualityReport:
    timestamps: set[datetime] = set()
    duplicates = 0
    non_monotonic = 0
    overlap_values: dict[datetime, tuple[Decimal | None, ...]] = {}
    conflicting_overlaps = 0
    invalid_before = 0
    invalid_after = 0
    crossed = 0
    nonpositive = 0
    nonfinite = 0
    volumes = 0
    zero_volume = 0
    active_zero_volume = 0
    closure_zero_volume = 0
    unknown_zero_volume = 0
    flat_bars = 0
    flat_zero_volume = 0
    complete_quotes = 0
    spreads: list[Decimal] = []
    suspicious = 0
    stale = 0
    stale_run = 1
    prior: DukascopyQuoteRecord | None = None
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    row_count = 0
    actual_timestamps: set[datetime] = set()
    adjustment_rows: set[int] = set()
    adjustment_max_ticks = 0

    def raw_side(row: DukascopyQuoteRecord, prefix: str) -> tuple[Decimal | None, ...]:
        raw = dict(row.quantization_raw_values)
        current = _ohlc_values(row, prefix)
        return tuple(
            Decimal(raw[f"{prefix}_{field}"]) if f"{prefix}_{field}" in raw else value
            for field, value in zip(("open", "high", "low", "close"), current, strict=True)
        )

    def safe_ohlc(values: tuple[Decimal | None, ...]) -> bool:
        return (
            len(values) == 4
            and all(value is not None and value.is_finite() and value > 0 for value in values)
            and _ohlc_valid(values)  # type: ignore[arg-type]
        )

    for row in rows:
        row_count += 1
        actual_timestamps.add(row.timestamp)
        coverage_start = (
            row.timestamp if coverage_start is None else min(coverage_start, row.timestamp)
        )
        coverage_end = row.timestamp if coverage_end is None else max(coverage_end, row.timestamp)
        if row.timestamp in timestamps:
            duplicates += 1
        timestamps.add(row.timestamp)
        if prior is not None and row.timestamp <= prior.timestamp:
            non_monotonic += 1
        fingerprint = (
            row.bid_open,
            row.bid_high,
            row.bid_low,
            row.bid_close,
            row.ask_open,
            row.ask_high,
            row.ask_low,
            row.ask_close,
            row.bid_volume,
            row.ask_volume,
        )
        prior_overlap = overlap_values.setdefault(row.timestamp, fingerprint)
        if prior_overlap != fingerprint:
            conflicting_overlaps += 1

        for prefix in ("bid", "ask"):
            raw_values = raw_side(row, prefix)
            current_values = _ohlc_values(row, prefix)
            present_raw = tuple(value for value in raw_values if value is not None)
            if present_raw and any(not value.is_finite() for value in present_raw):
                nonfinite += 1
            if present_raw and any(value.is_finite() and value <= 0 for value in present_raw):
                nonpositive += 1
            if len(present_raw) == 4 and not safe_ohlc(raw_values):
                invalid_before += 1
            current_present = tuple(value for value in current_values if value is not None)
            if len(current_present) == 4 and not safe_ohlc(current_values):
                invalid_after += 1

        if (
            row.bid_close is not None
            and row.ask_close is not None
            and row.bid_close.is_finite()
            and row.ask_close.is_finite()
        ):
            complete_quotes += 1
            spreads.append(row.ask_close - row.bid_close)
        crossed += sum(
            bid is not None
            and ask is not None
            and bid.is_finite()
            and ask.is_finite()
            and bid > ask
            for bid, ask in (
                (row.bid_open, row.ask_open),
                (row.bid_high, row.ask_high),
                (row.bid_low, row.ask_low),
                (row.bid_close, row.ask_close),
            )
        )
        for volume in (row.bid_volume, row.ask_volume):
            if volume is not None and ((not volume.is_finite()) or volume < 0):
                volumes += 1

        selected = row.selected_ohlc(config.quote_convention)
        selected_volume = (
            row.bid_volume if config.quote_convention is QuoteConvention.BID else row.ask_volume
        )
        if (
            selected is not None
            and all(value.is_finite() for value in selected)
            and selected[0] == selected[1] == selected[2] == selected[3]
        ):
            flat_bars += 1
            if selected_volume == 0:
                flat_zero_volume += 1
        if selected_volume == 0:
            zero_volume += 1
            try:
                market_open = calendar.is_open_at(row.timestamp)
            except (TypeError, ValueError, OverflowError):
                unknown_zero_volume += 1
            else:
                if market_open:
                    active_zero_volume += 1
                else:
                    closure_zero_volume += 1

        if row.quantization_adjusted:
            adjustment_rows.add(row.row_number)
            adjustment_max_ticks = max(
                adjustment_max_ticks, row.quantization_adjustment_max_ticks
            )
        if selected is not None and prior is not None:
            previous = prior.selected_ohlc(config.quote_convention)
            if (
                all(value.is_finite() and value > 0 for value in selected)
                and previous is not None
                and all(value.is_finite() and value > 0 for value in previous)
            ):
                jump = abs(selected[0] - previous[3]) / previous[3]
                if jump > policy.suspicious_jump_fraction:
                    suspicious += 1
                if selected[3] == previous[3]:
                    stale_run += 1
                else:
                    if stale_run >= policy.stale_sequence_length:
                        stale += 1
                    stale_run = 1
        prior = row
    if stale_run >= policy.stale_sequence_length:
        stale += 1
    coverage_end = coverage_end + config.timeframe.duration if coverage_end is not None else None
    missing, closures, gaps, unknown = _gaps(
        actual_timestamps,
        calendar=calendar,
        timeframe=config.timeframe.duration,
        start=coverage_start,
        end=coverage_end,
    )
    return DatasetQualityReport(
        row_count=row_count,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        duplicate_count=duplicates,
        non_monotonic_count=non_monotonic,
        invalid_ohlc_count=invalid_after,
        crossed_bid_ask_count=crossed,
        nonpositive_price_count=nonpositive,
        volume_anomaly_count=volumes,
        missing_intervals=missing,
        expected_closures=closures,
        unexpected_gaps=gaps,
        unknown_gaps=unknown,
        stale_sequences=stale,
        suspicious_jumps=suspicious,
        spread_count=len(spreads),
        min_spread=min(spreads) if spreads else None,
        max_spread=max(spreads) if spreads else None,
        median_spread=median(spreads) if spreads else None,
        p95_spread=_percentile(spreads, Decimal("0.95")),
        p99_spread=_percentile(spreads, Decimal("0.99")),
        schema_drift_count=schema_drift_count,
        conflicting_overlap_count=conflicting_overlaps,
        quote_side_complete_count=complete_quotes,
        calendar_id=calendar.calendar_id,
        invalid_ohlc_before_normalization_count=invalid_before,
        invalid_ohlc_after_normalization_count=invalid_after,
        quantization_adjustment_count=len(adjustment_rows),
        quantization_adjustment_rows=tuple(sorted(adjustment_rows)),
        quantization_adjustment_max_ticks=adjustment_max_ticks,
        nonfinite_price_count=nonfinite,
        zero_volume_bar_count=zero_volume,
        active_session_zero_volume_count=active_zero_volume,
        expected_closure_bar_count=closure_zero_volume,
        unknown_zero_volume_count=unknown_zero_volume,
        flat_bar_count=flat_bars,
        flat_zero_volume_count=flat_zero_volume,
        raw_row_count=row_count,
        normalized_row_count=row_count,
    )


def _gaps(
    actual: set[datetime],
    *,
    calendar: MarketCalendar,
    timeframe: timedelta,
    start: datetime | None,
    end: datetime | None,
) -> tuple[int, int, int, int]:
    if start is None or end is None:
        return 0, 0, 0, 0
    missing = 0
    closures = 0
    gaps = 0
    unknown = 0
    current = start
    while current < end:
        if current not in actual:
            missing += 1
            try:
                market_open = calendar.is_open_at(current)
            except (TypeError, ValueError, OverflowError):
                unknown += 1
            else:
                if market_open:
                    gaps += 1
                else:
                    closures += 1
        current += timeframe
    return missing, closures, gaps, unknown


def _timeframe_for_duration(duration: timedelta) -> Timeframe:
    for timeframe in Timeframe:
        if timeframe.duration == duration:
            return timeframe
    raise ValueError("unsupported timeframe duration")


# ---------------------------------------------------------------------------
# Provider-neutral OHLCV empirical admission


def _write_ohlcv_record(handle: _TextWriter, row: EmpiricalOHLCVRecord) -> str:
    encoded = json.dumps(row.canonical(), sort_keys=True, separators=(",", ":")) + "\n"
    handle.write(encoded)
    return encoded


def iter_normalized_ohlcv_records(path: Path) -> Iterable[EmpiricalOHLCVRecord]:
    """Read canonical provider-neutral OHLCV JSONL without source access."""

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                payload = json.loads(line)
                timestamp = datetime.fromisoformat(str(payload["timestamp"]))
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError("normalized timestamp is naive")
                if timestamp.utcoffset() != timedelta(0):
                    raise ValueError("normalized timestamp is not UTC")
                yield EmpiricalOHLCVRecord(
                    timestamp=timestamp,
                    open=Decimal(str(payload["open"])),
                    high=Decimal(str(payload["high"])),
                    low=Decimal(str(payload["low"])),
                    close=Decimal(str(payload["close"])),
                    volume=(
                        Decimal(str(payload["volume"]))
                        if payload["volume"] is not None
                        else None
                    ),
                    artifact_hash=str(payload["raw_artifact_hash"]),
                    row_number=int(payload["raw_row_number"]),
                )
            except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError) as exc:
                raise ValueError(f"normalized OHLCV row {line_number} is malformed") from exc


def _ohlcv_quality_report(
    rows: Iterable[EmpiricalOHLCVRecord],
    *,
    config: OHLCVAdmissionConfig,
    calendar: MarketCalendar,
    policy: AdmissionPolicy,
    schema_drift_count: int,
    expected_start: datetime,
    expected_end: datetime,
) -> DatasetQualityReport:
    timestamps: set[datetime] = set()
    overlap_values: dict[datetime, tuple[Decimal, Decimal, Decimal, Decimal, Decimal | None]] = {}
    duplicates = non_monotonic = conflicts = 0
    invalid = nonpositive = nonfinite = volume_anomalies = 0
    zero_volume = active_zero = closure_zero = unknown_zero = 0
    volume_unavailable = flat = flat_zero = suspicious = stale = 0
    stale_run = 1
    row_count = 0
    prior: EmpiricalOHLCVRecord | None = None
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    actual_timestamps: set[datetime] = set()

    for row in rows:
        row_count += 1
        actual_timestamps.add(row.timestamp)
        coverage_start = (
            row.timestamp if coverage_start is None else min(coverage_start, row.timestamp)
        )
        coverage_end = (
            row.timestamp if coverage_end is None else max(coverage_end, row.timestamp)
        )
        if row.timestamp in timestamps:
            duplicates += 1
        timestamps.add(row.timestamp)
        if prior is not None and row.timestamp <= prior.timestamp:
            non_monotonic += 1

        fingerprint = (row.open, row.high, row.low, row.close, row.volume)
        prior_overlap = overlap_values.setdefault(row.timestamp, fingerprint)
        if prior_overlap != fingerprint:
            conflicts += 1

        prices = (row.open, row.high, row.low, row.close)
        if any(not value.is_finite() for value in prices):
            nonfinite += 1
        if any(value.is_finite() and value <= 0 for value in prices):
            nonpositive += 1
        if not all(value.is_finite() and value > 0 for value in prices) or not _ohlc_valid(prices):
            invalid += 1

        if row.volume is None:
            volume_unavailable += 1
        elif not row.volume.is_finite() or row.volume < 0:
            volume_anomalies += 1
        elif row.volume == 0:
            zero_volume += 1
            try:
                open_at = calendar.is_open_at(row.timestamp)
            except (TypeError, ValueError, OverflowError):
                unknown_zero += 1
            else:
                if open_at:
                    active_zero += 1
                else:
                    closure_zero += 1

        if (
            all(value.is_finite() for value in prices)
            and prices[0] == prices[1] == prices[2] == prices[3]
        ):
            flat += 1
            if row.volume == 0:
                flat_zero += 1

        if prior is not None and row.close.is_finite() and row.open.is_finite():
            previous_valid = prior.close.is_finite() and prior.close > 0
            current_valid = row.open > 0
            if previous_valid and current_valid:
                jump = abs(row.open - prior.close) / prior.close
                if jump > policy.suspicious_jump_fraction:
                    suspicious += 1
                if row.close == prior.close:
                    stale_run += 1
                else:
                    if stale_run >= policy.stale_sequence_length:
                        stale += 1
                    stale_run = 1
        prior = row

    if stale_run >= policy.stale_sequence_length and row_count:
        stale += 1
    coverage_end = coverage_end + config.timeframe.duration if coverage_end is not None else None
    missing, closures, gaps, unknown = _gaps(
        actual_timestamps,
        calendar=calendar,
        timeframe=config.timeframe.duration,
        start=expected_start,
        end=expected_end,
    )
    return DatasetQualityReport(
        row_count=row_count,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        duplicate_count=duplicates,
        non_monotonic_count=non_monotonic,
        invalid_ohlc_count=invalid,
        crossed_bid_ask_count=0,
        nonpositive_price_count=nonpositive,
        volume_anomaly_count=volume_anomalies,
        missing_intervals=missing,
        expected_closures=closures,
        unexpected_gaps=gaps,
        unknown_gaps=unknown,
        stale_sequences=stale,
        suspicious_jumps=suspicious,
        spread_count=0,
        min_spread=None,
        max_spread=None,
        median_spread=None,
        p95_spread=None,
        p99_spread=None,
        schema_drift_count=schema_drift_count,
        conflicting_overlap_count=conflicts,
        quote_side_complete_count=0,
        calendar_id=calendar.calendar_id,
        invalid_ohlc_before_normalization_count=invalid,
        invalid_ohlc_after_normalization_count=invalid,
        nonfinite_price_count=nonfinite,
        zero_volume_bar_count=zero_volume,
        active_session_zero_volume_count=active_zero,
        expected_closure_bar_count=closure_zero,
        unknown_zero_volume_count=unknown_zero,
        flat_bar_count=flat,
        flat_zero_volume_count=flat_zero,
        raw_row_count=row_count,
        normalized_row_count=row_count,
        volume_unavailable_count=volume_unavailable,
    )


def _ohlcv_blockers(
    report: DatasetQualityReport,
    *,
    config: OHLCVAdmissionConfig,
    artifacts: Sequence[RawArtifactMetadata],
    policy: AdmissionPolicy,
    calendar: MarketCalendar,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if not artifacts:
        blockers.append("artifact_not_supplied")
    if not report.row_count or report.coverage_start is None or report.coverage_end is None:
        blockers.append("coverage_missing")
    checks = (
        (report.duplicate_count, policy.max_duplicate_timestamps, "duplicate_timestamps"),
        (
            report.non_monotonic_count,
            policy.max_non_monotonic_timestamps,
            "non_monotonic_timestamps",
        ),
        (report.invalid_ohlc_count, policy.max_invalid_ohlc, "invalid_ohlc"),
        (report.nonpositive_price_count, policy.max_nonpositive_prices, "nonpositive_prices"),
        (report.volume_anomaly_count, policy.max_volume_anomalies, "volume_anomalies"),
        (report.unexpected_gaps, policy.max_unexpected_gaps, "unexpected_gaps"),
        (report.schema_drift_count, policy.max_schema_drift, "schema_drift"),
        (
            report.active_session_zero_volume_count,
            policy.max_active_session_zero_volume,
            "active_session_zero_volume",
        ),
    )
    blockers.extend(name for actual, allowed, name in checks if actual > allowed)
    if report.nonfinite_price_count:
        blockers.append("nonfinite_prices")
    if report.unknown_gaps:
        blockers.append("unknown_gaps")
    if policy.require_bid_ask:
        blockers.append("bid_ask_unavailable")
    if policy.require_volume and report.volume_unavailable_count:
        blockers.append("volume_unavailable")
    if (
        policy.max_suspicious_jumps is not None
        and report.suspicious_jumps > policy.max_suspicious_jumps
    ):
        blockers.append("suspicious_jumps")
    if report.conflicting_overlap_count:
        blockers.append("conflicting_overlapping_records")
    if config.instrument.asset_class is not AssetClass.FOREX or not calendar.calendar_id.startswith(
        ("twelve-data-forex-", "dukascopy-forex-", "forex-")
    ):
        blockers.append("unknown_or_incompatible_calendar")
    return tuple(sorted(set(blockers)))


def admit_ohlcv_csv(
    *,
    raw_paths: Iterable[Path],
    raw_metadata: Iterable[RawArtifactMetadata],
    config: OHLCVAdmissionConfig,
    calendar: MarketCalendar,
    policy: AdmissionPolicy,
    normalized_dir: Path,
    manifest_dir: Path,
    created_at: datetime,
    parser: Callable[[Path, str], Iterable[EmpiricalOHLCVRecord]],
    schema_reader: Callable[[Path], tuple[str, ...]],
    deterministic_test_fixture: bool = False,
) -> DatasetAdmission:
    """Admit locally preserved OHLCV artifacts through the common gate.

    ``parser`` and ``schema_reader`` are source-specific callables supplied by
    the adapter; this function never performs acquisition or provider logic.
    """

    require_utc(created_at)
    paths = tuple(raw_paths)
    artifacts = tuple(raw_metadata)
    if not paths or len(paths) != len(artifacts):
        raise ValueError("each OHLCV import path requires exactly one raw metadata record")
    artifact_timeframe = config.artifact_timeframe or config.timeframe.value
    if any(item.source != config.source for item in artifacts):
        raise ValueError("raw artifact source does not match import configuration")
    if any(item.requested_timeframe != artifact_timeframe for item in artifacts):
        raise ValueError("raw artifact timeframe does not match import configuration")
    if any(item.source_symbol != config.source_symbol for item in artifacts):
        raise ValueError("raw artifact symbol does not match import configuration")
    if any(item.timezone != config.source_timezone for item in artifacts):
        raise ValueError("raw artifact timezone does not match import configuration")
    if config.source_version and any(
        item.source_version != config.source_version for item in artifacts
    ):
        raise ValueError("raw artifact source version does not match import configuration")

    normalized_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=normalized_dir
    ) as staged:
        staged_path = Path(staged.name)
        hasher = hashlib.sha256()
        try:
            for path, metadata in zip(paths, artifacts, strict=True):
                actual_hash, actual_size = sha256_file(path)
                if actual_hash != metadata.sha256 or actual_size != metadata.byte_size:
                    raise ValueError("raw artifact bytes no longer match preserved metadata")
                for row in parser(path, metadata.sha256):
                    text = _write_ohlcv_record(staged, row)
                    hasher.update(text.encode())
        except (OSError, ValueError):
            staged.close()
            staged_path.unlink(missing_ok=True)
            raise

    schemas = {schema_reader(path) for path in paths}
    report = _ohlcv_quality_report(
        iter_normalized_ohlcv_records(staged_path),
        config=config,
        calendar=calendar,
        policy=policy,
        schema_drift_count=max(0, len(schemas) - 1),
        expected_start=min(item.requested_start for item in artifacts),
        expected_end=max(item.requested_end for item in artifacts),
    )
    quality_hash = report.hash
    normalized_hash = hasher.hexdigest()
    material = {
        "source": config.source,
        "source_mode": "ohlcv_bar",
        "source_artifacts": [item.canonical() for item in artifacts],
        "instrument": config.instrument.canonical_symbol,
        "asset_class": config.instrument.asset_class.value,
        "timeframe": config.timeframe.value,
        "timestamp_format": config.timestamp_format.value,
        "timestamp_semantics": config.timestamp_semantics.value,
        "timezone": config.source_timezone,
        "calendar_id": calendar.calendar_id,
        "quality_report_hash": quality_hash,
        "policy_identity": policy.identity,
        "volume_semantics": config.volume_semantics,
        "normalized_content_hash": normalized_hash,
        "ingestion_version": OHLCV_INGESTION_VERSION,
        "canonical_schema_version": OHLCV_CANONICAL_SCHEMA_VERSION,
    }
    dataset_hash = _hash_json(material)
    manifest = EmpiricalDatasetManifest(
        dataset_id=dataset_hash,
        dataset_hash=dataset_hash,
        source=config.source,
        source_artifact_hashes=tuple(item.sha256 for item in artifacts),
        instrument=config.instrument.canonical_symbol,
        asset_class=config.instrument.asset_class.value,
        timeframe=config.timeframe.value,
        coverage_start=report.coverage_start,
        coverage_end=report.coverage_end,
        timestamp_format=config.timestamp_format,
        timestamp_semantics=config.timestamp_semantics,
        timezone=config.source_timezone,
        quote_convention=None,
        adjustment_policy=AdjustmentPolicy.RAW,
        calendar_id=calendar.calendar_id,
        quality_report_hash=quality_hash,
        ingestion_version=OHLCV_INGESTION_VERSION,
        canonical_schema_version=OHLCV_CANONICAL_SCHEMA_VERSION,
        created_at=created_at,
        artifact_metadata=artifacts,
        policy_identity=policy.identity,
        normalized_content_hash=normalized_hash,
        source_mode="ohlcv_bar",
        price_sides="ohlc",
        volume_sides="source",
        volume_semantics=config.volume_semantics,
        quote_configuration="bid_ask_unavailable",
    )
    normalized_path = normalized_dir / f"{dataset_hash}.jsonl"
    if normalized_path.exists():
        if sha256_file(normalized_path)[0] != normalized_hash:
            raise RuntimeError("immutable normalized dataset path has conflicting content")
        staged_path.unlink(missing_ok=True)
    else:
        os.replace(staged_path, normalized_path)
    report_path = manifest_dir / f"{dataset_hash}.quality.json"
    manifest_path = manifest_dir / f"{dataset_hash}.json"
    _write_immutable_json(report_path, report.canonical())
    _write_immutable_json(manifest_path, manifest.canonical())
    blockers = _ohlcv_blockers(
        report, config=config, artifacts=artifacts, policy=policy, calendar=calendar
    )
    state = (
        AdmissionState.DETERMINISTIC_TEST_FIXTURE
        if deterministic_test_fixture
        else AdmissionState.BLOCKED
        if blockers
        else AdmissionState.EMPIRICALLY_QUALIFIED_DATASET
    )
    return DatasetAdmission(
        manifest,
        report,
        state,
        blockers,
        normalized_path,
        manifest_path,
        report_path,
    )


# ---------------------------------------------------------------------------
# Tick-source empirical admission

TICK_CANONICAL_SCHEMA_VERSION = "empirical-forex-tick-m15-jsonl-v1"
TICK_INGESTION_VERSION = "dukascopy-tick-import-v1"


@dataclass(frozen=True)
class TickDatasetQualityReport:
    """Admission evidence for a tick-derived canonical M15 dataset."""

    raw_tick_rows: int
    m15_bar_count: int
    coverage_start: datetime | None
    coverage_end: datetime | None
    missing_m15_intervals: int
    expected_closures: int
    unexpected_gaps: int
    unknown_gaps: int
    invalid_ticks: int
    duplicate_timestamps: int
    non_monotonic_timestamps: int
    crossed_quotes: int
    nonfinite_prices: int
    nonpositive_prices: int
    volume_anomalies: int
    zero_volume_bars: int
    closed_session_zero_volume: int
    active_session_zero_volume: int
    unknown_zero_volume: int
    ohlc_violations: int
    stale_sequences: int
    suspicious_jumps: int
    spread_bar_count: int
    spread_observation_count: int
    mean_spread: Decimal | None
    minimum_spread: Decimal | None
    maximum_spread: Decimal | None
    calendar_id: str
    validation_violations: tuple[dict[str, object], ...] = ()

    @property
    def missing_intervals(self) -> int:
        return self.missing_m15_intervals

    @property
    def active_session_zero_volume_count(self) -> int:
        return self.active_session_zero_volume

    @property
    def hash(self) -> str:
        return _hash_json(self.canonical())

    def canonical(self) -> dict[str, object]:
        payload = asdict(self)
        for key in ("coverage_start", "coverage_end"):
            value = payload[key]
            payload[key] = value.isoformat() if value is not None else None
        for key in ("mean_spread", "minimum_spread", "maximum_spread"):
            value = payload[key]
            payload[key] = str(value) if value is not None else None
        payload["validation_violations"] = list(self.validation_violations)
        return payload


@dataclass(frozen=True)
class TickDatasetManifest:
    """Immutable identity for the tick → M15 transformation."""

    dataset_id: str
    source: str
    source_mode: str
    aggregation: str
    price_sides: str
    volume_sides: str
    timezone: str
    timestamp_semantics: str
    calendar_id: str
    aggregation_policy_id: str
    aggregation_policy_version: str
    raw_artifact_hashes: tuple[str, ...]
    normalized_content_hash: str
    quality_report_hash: str
    instrument: str
    timeframe: str
    coverage_start: datetime | None
    coverage_end: datetime | None
    quality_policy_identity: str
    quote_configuration: str
    ingestion_version: str
    canonical_schema_version: str
    created_at: datetime
    artifact_metadata: tuple[TickRawArtifactMetadata, ...]

    def canonical(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "source": self.source,
            "source_mode": self.source_mode,
            "aggregation": self.aggregation,
            "price_sides": self.price_sides,
            "volume_sides": self.volume_sides,
            "timezone": self.timezone,
            "timestamp_semantics": self.timestamp_semantics,
            "calendar_id": self.calendar_id,
            "aggregation_policy_id": self.aggregation_policy_id,
            "aggregation_policy_version": self.aggregation_policy_version,
            "raw_artifact_hashes": list(self.raw_artifact_hashes),
            "normalized_content_hash": self.normalized_content_hash,
            "quality_report_hash": self.quality_report_hash,
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "coverage_start": self.coverage_start.isoformat() if self.coverage_start else None,
            "coverage_end": self.coverage_end.isoformat() if self.coverage_end else None,
            "quality_policy_identity": self.quality_policy_identity,
            "quote_configuration": self.quote_configuration,
            "ingestion_version": self.ingestion_version,
            "canonical_schema_version": self.canonical_schema_version,
            "created_at": self.created_at.isoformat(),
            "artifact_metadata": [item.canonical() for item in self.artifact_metadata],
        }


@dataclass(frozen=True)
class TickDatasetAdmission:
    """Tick admission result; it contains no research or performance claim."""

    manifest: TickDatasetManifest
    quality_report: TickDatasetQualityReport
    state: AdmissionState
    blockers: tuple[str, ...]
    normalized_path: Path
    manifest_path: Path
    quality_report_path: Path


def _tick_floor_m15(timestamp: datetime) -> datetime:
    return timestamp.replace(minute=(timestamp.minute // 15) * 15, second=0, microsecond=0)


def _tick_ceil_m15(timestamp: datetime) -> datetime:
    floor = _tick_floor_m15(timestamp)
    return floor if timestamp == floor else floor + timedelta(minutes=15)


def _tick_gap_counts(
    *,
    actual: set[datetime],
    calendar: MarketCalendar,
    start: datetime | None,
    end: datetime | None,
) -> tuple[int, int, int, int]:
    if start is None or end is None or start >= end:
        return 0, 0, 0, 0
    current = _tick_floor_m15(start)
    stop = _tick_ceil_m15(end)
    missing = closures = gaps = unknown = 0
    while current < stop:
        if current not in actual:
            missing += 1
            try:
                open_at = calendar.is_open_at(current)
            except (TypeError, ValueError, OverflowError):
                unknown += 1
            else:
                if open_at:
                    gaps += 1
                else:
                    closures += 1
        current += timedelta(minutes=15)
    return missing, closures, gaps, unknown


def _tick_quality_report(
    *,
    bars: Sequence[TickM15Bar],
    validation: TickValidationReport,
    calendar: MarketCalendar,
    coverage_start: datetime | None,
    coverage_end: datetime | None,
    policy: AdmissionPolicy,
) -> TickDatasetQualityReport:
    actual = {bar.timestamp for bar in bars}
    missing, closures, gaps, unknown = _tick_gap_counts(
        actual=actual,
        calendar=calendar,
        start=coverage_start,
        end=coverage_end,
    )
    zero_volume = closed_zero = active_zero = unknown_zero = 0
    ohlc_violations = 0
    spreads: list[Decimal] = []
    stale_sequences = 0
    suspicious_jumps = 0
    stale_run = 1
    previous: TickM15Bar | None = None
    for bar in bars:
        if bar.bid.volume == 0 or bar.ask.volume == 0:
            zero_volume += 1
            try:
                is_open = calendar.is_open_at(bar.timestamp)
            except (TypeError, ValueError, OverflowError):
                unknown_zero += 1
            else:
                if is_open:
                    active_zero += 1
                else:
                    closed_zero += 1
        for side in (bar.bid, bar.ask):
            if not (
                side.high >= side.open
                and side.high >= side.close
                and side.low <= side.open
                and side.low <= side.close
                and side.high >= side.low
            ):
                ohlc_violations += 1
        spreads.append(bar.mean_spread)
        if previous is not None and previous.bid.close != 0:
            jump = abs(bar.bid.open - previous.bid.close) / previous.bid.close
            if jump > policy.suspicious_jump_fraction:
                suspicious_jumps += 1
            if bar.bid.close == previous.bid.close:
                stale_run += 1
            else:
                if stale_run >= policy.stale_sequence_length:
                    stale_sequences += 1
                stale_run = 1
        previous = bar
    if stale_run >= policy.stale_sequence_length:
        stale_sequences += 1

    spread_observations = sum(bar.tick_count for bar in bars)
    mean_spread: Decimal | None = None
    if spread_observations:
        with localcontext() as context:
            context.prec = 50
            mean_spread = sum(
                (bar.mean_spread * bar.tick_count for bar in bars),
                Decimal("0"),
            ) / Decimal(spread_observations)
    return TickDatasetQualityReport(
        raw_tick_rows=validation.row_count,
        m15_bar_count=len(bars),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        missing_m15_intervals=missing,
        expected_closures=closures,
        unexpected_gaps=gaps,
        unknown_gaps=unknown,
        invalid_ticks=validation.invalid_tick_count,
        duplicate_timestamps=validation.duplicate_timestamps,
        non_monotonic_timestamps=validation.non_monotonic_timestamps,
        crossed_quotes=validation.crossed_quote_count,
        nonfinite_prices=validation.nonfinite_price_count,
        nonpositive_prices=validation.nonpositive_price_count,
        volume_anomalies=validation.volume_anomaly_count,
        zero_volume_bars=zero_volume,
        closed_session_zero_volume=closed_zero,
        active_session_zero_volume=active_zero,
        unknown_zero_volume=unknown_zero,
        ohlc_violations=ohlc_violations,
        stale_sequences=stale_sequences,
        suspicious_jumps=suspicious_jumps,
        spread_bar_count=len(spreads),
        spread_observation_count=spread_observations,
        mean_spread=mean_spread,
        minimum_spread=min((bar.minimum_spread for bar in bars), default=None),
        maximum_spread=max((bar.maximum_spread for bar in bars), default=None),
        calendar_id=calendar.calendar_id,
        validation_violations=tuple(item.canonical() for item in validation.violations),
    )


def _tick_blockers(
    report: TickDatasetQualityReport,
    *,
    policy: AdmissionPolicy,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if report.invalid_ticks > policy.max_invalid_ticks:
        blockers.append("invalid_ticks")
    if report.duplicate_timestamps > policy.max_duplicate_timestamps:
        blockers.append("duplicate_timestamps")
    if report.non_monotonic_timestamps > policy.max_non_monotonic_timestamps:
        blockers.append("non_monotonic_timestamps")
    if report.crossed_quotes > policy.max_crossed_quotes:
        blockers.append("crossed_quotes")
    if report.nonfinite_prices:
        blockers.append("nonfinite_prices")
    if report.nonpositive_prices > policy.max_nonpositive_prices:
        blockers.append("nonpositive_prices")
    if report.volume_anomalies > policy.max_volume_anomalies:
        blockers.append("volume_anomalies")
    if report.ohlc_violations > policy.max_invalid_ohlc:
        blockers.append("ohlc_violations")
    if report.unexpected_gaps > policy.max_unexpected_gaps:
        blockers.append("unexpected_gaps")
    if report.unknown_gaps:
        blockers.append("unknown_gaps")
    if report.active_session_zero_volume > policy.max_active_session_zero_volume:
        blockers.append("active_session_zero_volume")
    if (
        policy.max_suspicious_jumps is not None
        and report.suspicious_jumps > policy.max_suspicious_jumps
    ):
        blockers.append("suspicious_jumps")
    return tuple(sorted(set(blockers)))


def admit_dukascopy_ticks(
    *,
    raw_paths: Iterable[Path],
    raw_metadata: Iterable[TickRawArtifactMetadata],
    config: DukascopyTickImportConfig,
    calendar: MarketCalendar,
    policy: AdmissionPolicy,
    normalized_dir: Path,
    manifest_dir: Path,
    created_at: datetime,
    aggregation_policy: TickAggregationPolicy | None = None,
    deterministic_test_fixture: bool = False,
) -> TickDatasetAdmission:
    """Preserve no bytes and perform no acquisition; admit local tick artifacts."""

    require_utc(created_at)
    paths = tuple(raw_paths)
    artifacts = tuple(raw_metadata)
    if not paths or len(paths) != len(artifacts):
        raise ValueError("each tick import path requires exactly one raw metadata record")
    if any(item.source != "dukascopy" for item in artifacts):
        raise ValueError("tick import requires Dukascopy raw artifact metadata")
    if any(item.source_symbol != config.source_symbol for item in artifacts):
        raise ValueError("tick artifact symbol does not match import configuration")
    if any(item.timezone != config.source_timezone for item in artifacts):
        raise ValueError("tick artifact timezone does not match import configuration")
    if config.source_version and any(
        item.source_version != config.source_version for item in artifacts
    ):
        raise ValueError("tick artifact source version does not match import configuration")

    aggregation = aggregation_policy or TickAggregationPolicy()
    aggregator = TickAggregator(policy=aggregation)
    for path, metadata in zip(paths, artifacts, strict=True):
        actual_hash, actual_size = sha256_file(path)
        if actual_hash != metadata.sha256 or actual_size != metadata.byte_size:
            raise ValueError("raw tick bytes no longer match preserved metadata")
        try:
            ticks = iter_dukascopy_ticks(
                path,
                artifact_hash=metadata.sha256,
                source_timezone=config.source_timezone,
            )
            for tick in ticks:
                aggregator.add(tick)
        except (OSError, TickSchemaError):
            raise
    result = aggregator.finish()
    coverage_start = min(item.coverage_start for item in artifacts)
    coverage_end = max(item.coverage_end for item in artifacts)
    report = _tick_quality_report(
        bars=result.bars,
        validation=result.validation,
        calendar=calendar,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        policy=policy,
    )

    normalized_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=normalized_dir
    ) as staged:
        staged_path = Path(staged.name)
        hasher = hashlib.sha256()
        for bar in result.bars:
            text = json.dumps(bar.canonical(), sort_keys=True, separators=(",", ":")) + "\n"
            staged.write(text)
            hasher.update(text.encode())
    normalized_hash = hasher.hexdigest()
    quality_hash = report.hash
    material = {
        "source": "dukascopy",
        "source_mode": "tick",
        "aggregation": "15m",
        "price_sides": aggregation.price_sides,
        "volume_sides": aggregation.volume_sides,
        "timezone": config.source_timezone,
        "timestamp_semantics": aggregation.timestamp_semantics,
        "calendar_id": calendar.calendar_id,
        "aggregation_policy": aggregation.canonical(),
        "aggregation_policy_id": aggregation.policy_id,
        "aggregation_policy_version": aggregation.policy_version,
        "raw_artifact_hashes": [item.sha256 for item in artifacts],
        "raw_artifact_metadata": [item.canonical() for item in artifacts],
        "normalized_content_hash": normalized_hash,
        "quality_report_hash": quality_hash,
        "instrument": config.instrument.canonical_symbol,
        "timeframe": aggregation.timeframe.value,
        "quality_policy_identity": policy.identity,
        "quote_configuration": aggregation.quote_configuration,
        "ingestion_version": TICK_INGESTION_VERSION,
        "canonical_schema_version": TICK_CANONICAL_SCHEMA_VERSION,
    }
    dataset_id = _hash_json(material)
    normalized_path = normalized_dir / f"{dataset_id}.jsonl"
    if normalized_path.exists():
        if sha256_file(normalized_path)[0] != normalized_hash:
            raise RuntimeError("immutable normalized tick dataset has conflicting content")
        staged_path.unlink(missing_ok=True)
    else:
        os.replace(staged_path, normalized_path)

    manifest = TickDatasetManifest(
        dataset_id=dataset_id,
        source="dukascopy",
        source_mode="tick",
        aggregation="15m",
        price_sides=aggregation.price_sides,
        volume_sides=aggregation.volume_sides,
        timezone=config.source_timezone,
        timestamp_semantics=aggregation.timestamp_semantics,
        calendar_id=calendar.calendar_id,
        aggregation_policy_id=aggregation.policy_id,
        aggregation_policy_version=aggregation.policy_version,
        raw_artifact_hashes=tuple(item.sha256 for item in artifacts),
        normalized_content_hash=normalized_hash,
        quality_report_hash=quality_hash,
        instrument=config.instrument.canonical_symbol,
        timeframe=aggregation.timeframe.value,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        quality_policy_identity=policy.identity,
        quote_configuration=aggregation.quote_configuration,
        ingestion_version=TICK_INGESTION_VERSION,
        canonical_schema_version=TICK_CANONICAL_SCHEMA_VERSION,
        created_at=created_at,
        artifact_metadata=artifacts,
    )
    manifest_path = manifest_dir / f"{dataset_id}.json"
    quality_report_path = manifest_dir / f"{dataset_id}.quality.json"
    _write_immutable_json(manifest_path, manifest.canonical())
    _write_immutable_json(quality_report_path, report.canonical())
    blockers = _tick_blockers(report, policy=policy)
    state = (
        AdmissionState.DETERMINISTIC_TEST_FIXTURE
        if deterministic_test_fixture
        else AdmissionState.BLOCKED
        if blockers
        else AdmissionState.EMPIRICALLY_QUALIFIED_DATASET
    )
    return TickDatasetAdmission(
        manifest=manifest,
        quality_report=report,
        state=state,
        blockers=blockers,
        normalized_path=normalized_path,
        manifest_path=manifest_path,
        quality_report_path=quality_report_path,
    )


__all__ = [
    "AdmissionPolicy",
    "AdmissionState",
    "CANONICAL_SCHEMA_VERSION",
    "DatasetAdmission",
    "DatasetQualityReport",
    "EmpiricalOHLCVRecord",
    "EmpiricalDatasetManifest",
    "GapClassification",
    "INGESTION_VERSION",
    "OHLCVAdmissionConfig",
    "OHLCV_CANONICAL_SCHEMA_VERSION",
    "OHLCV_INGESTION_VERSION",
    "RawArtifactMetadata",
    "RawArtifactStore",
    "TICK_CANONICAL_SCHEMA_VERSION",
    "TICK_INGESTION_VERSION",
    "TickDatasetAdmission",
    "TickDatasetManifest",
    "TickDatasetQualityReport",
    "TickArtifactMetadata",
    "TickRawArtifactMetadata",
    "admit_dukascopy_ticks",
    "admit_dukascopy_csv",
    "admit_ohlcv_csv",
    "iter_normalized_ohlcv_records",
    "sha256_file",
]
