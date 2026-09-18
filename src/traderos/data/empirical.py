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
from dataclasses import asdict, dataclass
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

CANONICAL_SCHEMA_VERSION = "empirical-forex-jsonl-v1"
INGESTION_VERSION = "dukascopy-local-import-v2"


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

    def canonical(self) -> dict[str, object]:
        values = asdict(self)
        for key in ("coverage_start", "coverage_end"):
            value = values[key]
            values[key] = value.isoformat() if value is not None else None
        for key in ("min_spread", "max_spread", "median_spread", "p95_spread", "p99_spread"):
            value = values[key]
            values[key] = str(value) if value is not None else None
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
        (report.volume_anomaly_count, policy.max_volume_anomalies, "volume_anomalies"),
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
                    text = _write_record(staged, record)
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
    invalid_ohlc = 0
    crossed = 0
    nonpositive = 0
    volumes = 0
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
        sides = (
            (row.bid_open, row.bid_high, row.bid_low, row.bid_close),
            (row.ask_open, row.ask_high, row.ask_low, row.ask_close),
        )
        for side in sides:
            present = tuple(value for value in side if value is not None)
            if present and any(value <= 0 for value in present):
                nonpositive += 1
            if len(present) == 4 and not _ohlc_valid(side):  # type: ignore[arg-type]
                invalid_ohlc += 1
        if row.bid_close is not None and row.ask_close is not None:
            complete_quotes += 1
            spread = row.ask_close - row.bid_close
            spreads.append(spread)
        crossed += sum(
            bid is not None and ask is not None and bid > ask
            for bid, ask in (
                (row.bid_open, row.ask_open),
                (row.bid_high, row.ask_high),
                (row.bid_low, row.ask_low),
                (row.bid_close, row.ask_close),
            )
        )
        if any(value is not None and value <= 0 for value in (row.bid_volume, row.ask_volume)):
            volumes += 1
        selected = row.selected_ohlc(config.quote_convention)
        if selected is not None and prior is not None:
            previous = prior.selected_ohlc(config.quote_convention)
            if previous is not None and previous[3] > 0:
                jump = abs(selected[0] - previous[3]) / previous[3]
                if jump > policy.suspicious_jump_fraction:
                    suspicious += 1
            if previous is not None:
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
        invalid_ohlc_count=invalid_ohlc,
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
    expected = set(calendar.expected_bar_timestamps(start, end, _timeframe_for_duration(timeframe)))
    missing = expected - actual
    closures = 0
    current = start
    while current < end:
        if not calendar.is_open_at(current) and current not in actual:
            closures += 1
        current += timeframe
    # Calendar knows expected sessions. A bounded importer never labels an
    # in-session absent bar as closure; unknown is reserved for timestamps that
    # cannot be evaluated (none under this required calendar contract).
    return len(missing), closures, len(missing), 0


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
