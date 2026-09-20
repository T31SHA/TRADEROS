"""Adversarial tests for the Dukascopy tick → canonical M15 pipeline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from traderos.data.calendars import DukascopyForexCalendar
from traderos.data.empirical import (
    AdmissionPolicy,
    AdmissionState,
    RawArtifactStore,
    TickRawArtifactMetadata,
    admit_dukascopy_ticks,
    sha256_file,
)
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.ticks import (
    DukascopyTickImportConfig,
    TickAggregationPolicy,
    TickValidationError,
    aggregate_ticks,
    iter_dukascopy_ticks,
    iter_normalized_tick_bars,
    normalize_tick_timestamp,
    validate_ticks,
)

BASE = datetime(2024, 1, 2, tzinfo=UTC)


def _instrument() -> Instrument:
    return Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        provider_symbols={"dukascopy": "EURUSD"},
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
    )


def _config() -> DukascopyTickImportConfig:
    return DukascopyTickImportConfig(
        instrument=_instrument(), source_symbol="EURUSD", source_timezone="UTC"
    )


def _epoch(timestamp: datetime) -> str:
    return str(int(timestamp.timestamp() * 1000))


def _csv(rows: list[tuple[datetime, str, str, str, str]]) -> str:
    lines = ["timestamp,askPrice,bidPrice,askVolume,bidVolume"]
    lines.extend(
        f"{_epoch(timestamp)},{ask},{bid},{ask_volume},{bid_volume}"
        for timestamp, ask, bid, ask_volume, bid_volume in rows
    )
    return "\n".join(lines) + "\n"


def _fixture_rows(
    *,
    start: datetime = datetime(2024, 1, 2, 12, 0, 1, tzinfo=UTC),
    volume: str = "1",
) -> list[tuple[datetime, str, str, str, str]]:
    return [
        (start, "1.10002", "1.10000", volume, volume),
        (start + timedelta(minutes=2, seconds=59), "1.10007", "1.10005", "2", "2"),
        (start + timedelta(minutes=9, seconds=59), "1.09997", "1.09995", "3", "3"),
        (start + timedelta(minutes=14, seconds=58), "1.10003", "1.10001", "4", "4"),
    ]


def _preserve(
    tmp_path: Path,
    rows: list[tuple[datetime, str, str, str, str]],
    *,
    coverage_start: datetime | None = None,
    coverage_end: datetime | None = None,
) -> tuple[Path, TickRawArtifactMetadata]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "eurusd-ticks.csv"
    source.write_text(_csv(rows), encoding="utf-8")
    start = coverage_start or rows[0][0]
    end = coverage_end or rows[-1][0] + timedelta(milliseconds=1)
    return RawArtifactStore(tmp_path / "raw").preserve_tick(
        source,
        source="dukascopy",
        source_symbol="EURUSD",
        coverage_start=start,
        coverage_end=end,
        timezone="UTC",
        download_timestamp=datetime(2024, 1, 3, tzinfo=UTC),
    )


def _admit(
    tmp_path: Path,
    rows: list[tuple[datetime, str, str, str, str]],
    *,
    coverage_start: datetime | None = None,
    coverage_end: datetime | None = None,
    policy: AdmissionPolicy | None = None,
    aggregation_policy: TickAggregationPolicy | None = None,
    license_reference: str | None = None,
):
    preserved, metadata = _preserve(
        tmp_path,
        rows,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )
    return admit_dukascopy_ticks(
        raw_paths=(preserved,),
        raw_metadata=(metadata,),
        config=_config(),
        calendar=DukascopyForexCalendar(),
        policy=policy or AdmissionPolicy(),
        normalized_dir=tmp_path / "normalized",
        manifest_dir=tmp_path / "manifests",
        created_at=datetime(2024, 1, 3, tzinfo=UTC),
        aggregation_policy=aggregation_policy,
        license_reference=license_reference,
    )


def test_tick_manifest_persists_license_reference_in_dataset_identity(tmp_path: Path) -> None:
    without_reference = _admit(tmp_path / "without", _fixture_rows())
    admission = _admit(
        tmp_path / "with",
        _fixture_rows(),
        license_reference="https://provider.example/terms/research",
    )

    assert (
        admission.manifest.canonical()["license_reference"]
        == "https://provider.example/terms/research"
    )
    assert admission.manifest.dataset_id != without_reference.manifest.dataset_id


def test_tick_admission_rejects_blank_license_reference(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="license reference"):
        _admit(tmp_path, _fixture_rows(), license_reference=" ")


def test_tick_timestamp_normalization_is_exact_utc() -> None:
    assert normalize_tick_timestamp("1704196801000") == datetime(
        2024, 1, 2, 12, 0, 1, tzinfo=UTC
    )


def test_tick_fixture_aggregates_exact_bid_ask_ohlcv_and_spread(tmp_path: Path) -> None:
    source = tmp_path / "ticks.csv"
    source.write_text(_csv(_fixture_rows()), encoding="utf-8")
    ticks = tuple(iter_dukascopy_ticks(source, artifact_hash="a" * 64))

    bars = aggregate_ticks(ticks)

    assert len(bars) == 1
    bar = bars[0]
    assert bar.timestamp == datetime(2024, 1, 2, 12, tzinfo=UTC)
    assert bar.bid.open == Decimal("1.10000")
    assert bar.bid.high == Decimal("1.10005")
    assert bar.bid.low == Decimal("1.09995")
    assert bar.bid.close == Decimal("1.10001")
    assert bar.bid.volume == Decimal("10")
    assert bar.ask.open == Decimal("1.10002")
    assert bar.ask.high == Decimal("1.10007")
    assert bar.ask.low == Decimal("1.09997")
    assert bar.ask.close == Decimal("1.10003")
    assert bar.ask.volume == Decimal("10")
    assert bar.closing_spread == Decimal("0.00002")
    assert bar.minimum_spread == Decimal("0.00002")
    assert bar.maximum_spread == Decimal("0.00002")
    assert bar.mean_spread == Decimal("0.00002")


def test_tick_mapping_uses_explicit_canonical_bid_side(tmp_path: Path) -> None:
    source = tmp_path / "ticks.csv"
    source.write_text(_csv(_fixture_rows()), encoding="utf-8")
    bar = aggregate_ticks(tuple(iter_dukascopy_ticks(source, artifact_hash="a" * 64)))[0]

    market_bar = bar.to_market_bar(
        instrument=_instrument(),
        quote_side="bid",
        ingestion_timestamp=datetime(2024, 1, 3, tzinfo=UTC),
    )

    assert (market_bar.open, market_bar.high, market_bar.low, market_bar.close) == (
        Decimal("1.10000"),
        Decimal("1.10005"),
        Decimal("1.09995"),
        Decimal("1.10001"),
    )
    assert (market_bar.bid, market_bar.ask, market_bar.spread) == (
        Decimal("1.10001"),
        Decimal("1.10003"),
        Decimal("0.00002"),
    )


def test_reordered_ticks_are_reported_and_blocked(tmp_path: Path) -> None:
    rows = _fixture_rows()
    source = tmp_path / "ticks.csv"
    source.write_text(_csv([rows[1], rows[0], *rows[2:]]), encoding="utf-8")
    ticks = tuple(iter_dukascopy_ticks(source, artifact_hash="a" * 64))

    report = validate_ticks(ticks)
    assert report.non_monotonic_timestamps == 1
    with pytest.raises(TickValidationError) as error:
        aggregate_ticks(ticks)
    assert error.value.report.non_monotonic_timestamps == 1


@pytest.mark.parametrize(
    ("ask", "bid", "expected"),
    [("1.09999", "1.10000", "crossed_quotes"), ("1.10002", "-1", "nonpositive_prices")],
)
def test_invalid_quotes_are_blockers(
    tmp_path: Path, ask: str, bid: str, expected: str
) -> None:
    rows = [(BASE, ask, bid, "1", "1")]
    admission = _admit(tmp_path, rows)
    assert admission.state is AdmissionState.BLOCKED
    assert expected in admission.blockers
    assert admission.quality_report.invalid_ticks == 1


def test_negative_volume_is_reported_and_blocked(tmp_path: Path) -> None:
    admission = _admit(tmp_path, [(BASE, "1.10002", "1.10000", "-1", "1")])

    assert admission.state is AdmissionState.BLOCKED
    assert admission.quality_report.volume_anomalies == 1
    assert "volume_anomalies" in admission.blockers


def test_missing_tick_interval_is_not_synthesized_and_active_gap_blocks(tmp_path: Path) -> None:
    rows = [
        (datetime(2024, 1, 2, 0, 0, 1, tzinfo=UTC), "1.1", "1.0", "1", "1"),
        (datetime(2024, 1, 2, 0, 30, 1, tzinfo=UTC), "1.1", "1.0", "1", "1"),
    ]
    admission = _admit(
        tmp_path,
        rows,
        coverage_start=datetime(2024, 1, 2, tzinfo=UTC),
        coverage_end=datetime(2024, 1, 2, 0, 45, tzinfo=UTC),
    )

    assert admission.quality_report.m15_bar_count == 2
    assert admission.quality_report.missing_m15_intervals == 1
    assert admission.quality_report.unexpected_gaps == 1
    assert admission.state is AdmissionState.BLOCKED
    assert "unexpected_gaps" in admission.blockers


def test_weekend_missing_ticks_are_expected_closures(tmp_path: Path) -> None:
    rows = [
        (datetime(2024, 1, 5, 21, 45, 1, tzinfo=UTC), "1.1", "1.0", "1", "1"),
        (datetime(2024, 1, 7, 22, 0, 1, tzinfo=UTC), "1.1", "1.0", "1", "1"),
    ]
    admission = _admit(
        tmp_path,
        rows,
        coverage_start=datetime(2024, 1, 5, 21, 45, tzinfo=UTC),
        coverage_end=datetime(2024, 1, 7, 22, 15, tzinfo=UTC),
    )

    assert admission.quality_report.expected_closures > 0
    assert admission.quality_report.unexpected_gaps == 0
    assert admission.state is AdmissionState.EMPIRICALLY_QUALIFIED_DATASET


def test_active_session_zero_volume_is_a_blocker(tmp_path: Path) -> None:
    admission = _admit(tmp_path, [(BASE, "1.10002", "1.10000", "0", "0")])

    assert admission.quality_report.zero_volume_bars == 1
    assert admission.quality_report.active_session_zero_volume == 1
    assert "active_session_zero_volume" in admission.blockers


def test_raw_bytes_are_immutable_and_dataset_identity_binds_policy(tmp_path: Path) -> None:
    rows = _fixture_rows()
    first = _admit(tmp_path / "first", rows)
    changed = _admit(
        tmp_path / "changed",
        rows,
        aggregation_policy=TickAggregationPolicy(policy_version="v2"),
    )
    preserved, metadata = _preserve(tmp_path / "raw", rows)
    before = preserved.read_bytes()
    digest, size = sha256_file(preserved)

    assert preserved.read_bytes() == before
    assert metadata.sha256 == digest
    assert metadata.byte_size == size
    assert first.manifest.dataset_id != changed.manifest.dataset_id
    assert first.manifest.source_mode == "tick"
    assert len(first.manifest.raw_artifact_hashes) == 1
    assert first.manifest.price_sides == "bid+ask"
    assert first.manifest.volume_sides == "bid+ask"


def test_repeated_aggregation_is_byte_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "ticks.csv"
    source.write_text(_csv(_fixture_rows()), encoding="utf-8")
    ticks = tuple(iter_dukascopy_ticks(source, artifact_hash="a" * 64))

    assert aggregate_ticks(ticks) == aggregate_ticks(ticks)


def test_admitted_normalized_tick_dataset_preserves_both_sides(tmp_path: Path) -> None:
    admission = _admit(tmp_path, _fixture_rows())

    bars = tuple(iter_normalized_tick_bars(admission.normalized_path))

    assert len(bars) == 1
    assert bars[0].bid.close == Decimal("1.10001")
    assert bars[0].ask.close == Decimal("1.10003")
    assert bars[0].mean_spread == Decimal("0.00002")
