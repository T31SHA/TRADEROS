"""Admit caller-supplied Twelve Data EUR/USD M15 CSV artifacts offline.

Acquisition is deliberately external.  This command preserves each local raw
file, validates its exact bytes, and runs the common empirical admission gate;
it never contacts Twelve Data and never runs a strategy.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from traderos.data.calendars import TwelveDataForexCalendar
from traderos.data.empirical import AdmissionPolicy, RawArtifactStore
from traderos.data.temporal import TimestampFormat, TimestampSemantics
from traderos.data.timeframes import Timeframe
from traderos.data.twelvedata import (
    TwelveDataImportConfig,
    admit_twelve_data_csv,
    instrument_eurusd,
)

ROOT = Path(__file__).resolve().parents[1]


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamps must be ISO-8601 UTC values") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise argparse.ArgumentTypeError("timestamps must be timezone-aware UTC values")
    return parsed.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="+", type=Path, help="local Twelve Data CSV artifact(s)")
    parser.add_argument("--requested-start", required=True, type=_utc)
    parser.add_argument("--requested-end", required=True, type=_utc)
    parser.add_argument("--download-timestamp", required=True, type=_utc)
    parser.add_argument("--source-version", default=None)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    if any(not path.is_file() for path in args.source):
        missing = next(path for path in args.source if not path.is_file())
        raise FileNotFoundError(missing)

    raw_store = RawArtifactStore(args.data_root / "raw")
    preserved: list[Path] = []
    metadata = []
    for source in args.source:
        item, item_metadata = raw_store.preserve(
            source,
            source="twelve_data",
            source_symbol="EUR/USD",
            requested_start=args.requested_start,
            requested_end=args.requested_end,
            requested_timeframe="15min",
            timezone="UTC",
            download_timestamp=args.download_timestamp,
            source_version=args.source_version,
        )
        preserved.append(item)
        metadata.append(item_metadata)

    config = TwelveDataImportConfig(
        instrument=instrument_eurusd(),
        timeframe=Timeframe.M15,
        source_symbol="EUR/USD",
        source_timezone="UTC",
        interval="15min",
        timestamp_format=TimestampFormat.ISO_8601,
        timestamp_semantics=TimestampSemantics.BAR_START,
        source_version=args.source_version,
    )
    admission = admit_twelve_data_csv(
        raw_paths=preserved,
        raw_metadata=metadata,
        config=config,
        calendar=TwelveDataForexCalendar(),
        policy=AdmissionPolicy(),
        normalized_dir=args.data_root / "normalized",
        manifest_dir=args.data_root / "manifests",
        created_at=args.download_timestamp,
    )
    report = admission.quality_report
    print(f"source_artifact_sha256={','.join(item.sha256 for item in metadata)}")
    print(f"row_count={report.row_count}")
    print(f"coverage_start={report.coverage_start}")
    print(f"coverage_end={report.coverage_end}")
    for name in (
        "duplicate_count",
        "non_monotonic_count",
        "invalid_ohlc_count",
        "nonpositive_price_count",
        "nonfinite_price_count",
        "missing_intervals",
        "expected_closures",
        "unexpected_gaps",
        "unknown_gaps",
        "stale_sequences",
        "suspicious_jumps",
        "volume_anomaly_count",
        "volume_unavailable_count",
    ):
        print(f"{name}={getattr(report, name)}")
    print(f"dataset_id={admission.manifest.dataset_id}")
    print(f"quality_report_hash={report.hash}")
    print(f"normalized_path={admission.normalized_path}")
    print(f"manifest_path={admission.manifest_path}")
    print(f"qualification_state={admission.state.value}")
    print(f"blockers={','.join(admission.blockers) if admission.blockers else 'none'}")
    print("bid_ask_limitation=unavailable; no bid/ask or spread values were fabricated")
    print("volume_semantics=source-provided Forex volume; not centralized exchange volume")
    print("cost_boundary=historical spread unavailable; use an explicit transaction cost scenario")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
