"""Run the offline admission workflow for the local EUR/USD Dukascopy artifact."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from traderos.data.calendars import ForexCalendar
from traderos.data.dukascopy import (
    DukascopyImportConfig,
    QuoteConvention,
    TimestampFormat,
    TimestampSemantics,
)
from traderos.data.empirical import AdmissionPolicy, RawArtifactStore, admit_dukascopy_csv
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.timeframes import Timeframe

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/raw/dukascopy/EURUSD/v2/eurusd-m15-bid-2024-01-01-2025-01-01.csv"


def main() -> int:
    if not SOURCE.is_file():
        raise FileNotFoundError(f"local raw artifact not found: {SOURCE}")

    # The repository does not have the source's original download timestamp.
    # The existing artifact mtime is a deterministic local capture timestamp;
    # it is provenance metadata, not a claimed Dukascopy source version.
    capture_timestamp = datetime.fromtimestamp(SOURCE.stat().st_mtime, tz=UTC)
    raw_store = RawArtifactStore(ROOT / "data/raw")
    preserved, metadata = raw_store.preserve(
        SOURCE,
        source="dukascopy",
        source_symbol="EURUSD",
        requested_start=datetime(2024, 1, 1, tzinfo=UTC),
        requested_end=datetime(2025, 1, 1, tzinfo=UTC),
        requested_timeframe=Timeframe.M15.value,
        timezone="UTC",
        download_timestamp=capture_timestamp,
        source_version="dukascopy-node",
    )
    config = DukascopyImportConfig(
        instrument=Instrument(
            canonical_symbol="EUR/USD",
            asset_class=AssetClass.FOREX,
            provider_symbols={"dukascopy": "EURUSD"},
            base_currency="EUR",
            quote_currency="USD",
            trading_currency="USD",
        ),
        timeframe=Timeframe.M15,
        source_symbol="EURUSD",
        source_timezone="UTC",
        timestamp_semantics=TimestampSemantics.BAR_START,
        quote_convention=QuoteConvention.BID,
        timestamp_format=TimestampFormat.EPOCH_MILLISECONDS,
        source_version="dukascopy-node",
        price_tick_size=Decimal("0.00001"),
    )
    admission = admit_dukascopy_csv(
        raw_paths=(preserved,),
        raw_metadata=(metadata,),
        config=config,
        calendar=ForexCalendar(),
        policy=AdmissionPolicy(),
        normalized_dir=ROOT / "data/normalized",
        manifest_dir=ROOT / "data/manifests",
        created_at=capture_timestamp,
    )
    report = admission.quality_report
    print(f"file={SOURCE}")
    print(f"bytes={metadata.byte_size}")
    print(f"source_artifact_sha256={metadata.sha256}")
    print(f"rows={report.row_count}")
    print(f"coverage_start={report.coverage_start.isoformat() if report.coverage_start else None}")
    print(f"coverage_end={report.coverage_end.isoformat() if report.coverage_end else None}")
    for name in (
        "duplicate_count",
        "non_monotonic_count",
        "invalid_ohlc_count",
        "invalid_ohlc_before_normalization_count",
        "invalid_ohlc_after_normalization_count",
        "quantization_adjustment_count",
        "quantization_adjustment_max_ticks",
        "crossed_bid_ask_count",
        "nonpositive_price_count",
        "nonfinite_price_count",
        "volume_anomaly_count",
        "zero_volume_bar_count",
        "active_session_zero_volume_count",
        "expected_closure_bar_count",
        "unknown_zero_volume_count",
        "flat_bar_count",
        "flat_zero_volume_count",
        "missing_intervals",
        "unexpected_gaps",
        "expected_closures",
        "unknown_gaps",
        "stale_sequences",
        "suspicious_jumps",
        "spread_count",
        "schema_drift_count",
        "conflicting_overlap_count",
        "quote_side_complete_count",
    ):
        print(f"{name}={getattr(report, name)}")
    print(f"normalized_path={admission.normalized_path}")
    print(f"quality_report_path={admission.quality_report_path}")
    print(f"manifest_path={admission.manifest_path}")
    print(f"normalized_content_hash={admission.manifest.normalized_content_hash}")
    print(f"dataset_id={admission.manifest.dataset_id}")
    print(f"quality_report_hash={report.hash}")
    print(f"quantization_adjustment_rows={list(report.quantization_adjustment_rows)}")
    print(f"price_tick_size={config.price_tick_size}")
    print(f"quantization_policy_id={config.quantization_policy_id}")
    print(f"quantization_policy_version={config.quantization_policy_version}")
    print(f"qualification_state={admission.state.value}")
    print(f"blockers={','.join(admission.blockers) if admission.blockers else 'none'}")
    print("quote_limitation=bid-only; ask OHLC and spread metrics unavailable")
    print(
        "volume_limitation=source-provided Dukascopy volume mapped to bid_volume only; "
        "not market-wide traded volume"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
