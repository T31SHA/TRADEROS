"""Admit one caller-supplied, bounded Dukascopy-node tick artifact.

Acquisition is intentionally external.  For example, a caller may acquire
only a six-hour interval with ``npx dukascopy-node`` and pass the resulting
CSV here.  This script never downloads, runs strategies, or expands a range.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from traderos.data.calendars import DukascopyForexCalendar
from traderos.data.empirical import AdmissionPolicy, RawArtifactStore, admit_dukascopy_ticks
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.ticks import DukascopyTickImportConfig

ROOT = Path(__file__).resolve().parents[1]


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise argparse.ArgumentTypeError("timestamps must be timezone-aware UTC ISO-8601 values")
    return parsed.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="local Dukascopy-node tick CSV")
    parser.add_argument("--coverage-start", required=True, type=_utc)
    parser.add_argument("--coverage-end", required=True, type=_utc)
    parser.add_argument("--download-timestamp", required=True, type=_utc)
    parser.add_argument("--source-version", default=None)
    parser.add_argument(
        "--license-reference",
        default=None,
        help="operator-supplied provider terms/license URI or configured evidence reference",
    )
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    if not args.source.is_file():
        raise FileNotFoundError(args.source)

    store = RawArtifactStore(args.data_root / "raw")
    preserved, metadata = store.preserve_tick(
        args.source,
        source="dukascopy",
        source_symbol="EURUSD",
        coverage_start=args.coverage_start,
        coverage_end=args.coverage_end,
        timezone="UTC",
        download_timestamp=args.download_timestamp,
        source_version=args.source_version,
    )
    config = DukascopyTickImportConfig(
        instrument=Instrument(
            canonical_symbol="EUR/USD",
            asset_class=AssetClass.FOREX,
            provider_symbols={"dukascopy": "EURUSD"},
            base_currency="EUR",
            quote_currency="USD",
            trading_currency="USD",
        ),
        source_symbol="EURUSD",
        source_timezone="UTC",
        source_version=args.source_version,
    )
    admission = admit_dukascopy_ticks(
        raw_paths=(preserved,),
        raw_metadata=(metadata,),
        config=config,
        calendar=DukascopyForexCalendar(),
        policy=AdmissionPolicy(),
        normalized_dir=args.data_root / "normalized",
        manifest_dir=args.data_root / "manifests",
        created_at=args.download_timestamp,
        license_reference=args.license_reference,
    )
    report = admission.quality_report
    print(f"raw_artifact_sha256={metadata.sha256}")
    print(f"raw_tick_rows={report.raw_tick_rows}")
    print(f"m15_bar_count={report.m15_bar_count}")
    print(f"coverage_start={report.coverage_start}")
    print(f"coverage_end={report.coverage_end}")
    print(f"missing_m15_intervals={report.missing_m15_intervals}")
    print(f"expected_closures={report.expected_closures}")
    print(f"unexpected_gaps={report.unexpected_gaps}")
    print(f"unknown_gaps={report.unknown_gaps}")
    print(f"invalid_ticks={report.invalid_ticks}")
    print(f"crossed_quotes={report.crossed_quotes}")
    print(f"nonpositive_prices={report.nonpositive_prices}")
    print(f"volume_anomalies={report.volume_anomalies}")
    print(f"zero_volume_bars={report.zero_volume_bars}")
    print(f"active_session_zero_volume={report.active_session_zero_volume}")
    print(f"mean_spread={report.mean_spread}")
    print(f"minimum_spread={report.minimum_spread}")
    print(f"maximum_spread={report.maximum_spread}")
    print(f"dataset_id={admission.manifest.dataset_id}")
    print(f"quality_report_hash={report.hash}")
    print(f"normalized_path={admission.normalized_path}")
    print(f"manifest_path={admission.manifest_path}")
    print(f"qualification_state={admission.state.value}")
    print(f"blockers={','.join(admission.blockers) if admission.blockers else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
