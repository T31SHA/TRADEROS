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
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
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
    TimestampFormat,
    TimestampSemantics,
    dukascopy_csv_schema,
    iter_dukascopy_csv,
    iter_normalized_quote_records,
)
from traderos.data.instruments import AssetClass
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.time import require_utc
from traderos.data.timeframes import Timeframe

CANONICAL_SCHEMA_VERSION = "empirical-forex-jsonl-v2"
INGESTION_VERSION = "dukascopy-local-import-v3"


class GapClassification(StrEnum):
    EXPECTED_MARKET_CLOSURE = "expected_market_closure"
    DATA_GAP = "data_gap"
    UNKNOWN = "unknown"


class AdmissionState(StrEnum):
    DETERMINISTIC_TEST_FIXTURE = "deterministic_test_fixture"
    EMPIRICALLY_QUALIFIED_DATASET = "empirically_qualified_dataset"
    BLOCKED = "blocked"


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
    quote_convention: QuoteConvention
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
            "quote_convention": self.quote_convention.value,
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
    if (
        config.instrument.asset_class is not AssetClass.FOREX
        or calendar.calendar_id != "forex-weekday-utc-v1"
    ):
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


__all__ = [
    "AdmissionPolicy",
    "AdmissionState",
    "CANONICAL_SCHEMA_VERSION",
    "DatasetAdmission",
    "DatasetQualityReport",
    "EmpiricalDatasetManifest",
    "GapClassification",
    "INGESTION_VERSION",
    "RawArtifactMetadata",
    "RawArtifactStore",
    "admit_dukascopy_csv",
    "sha256_file",
]
