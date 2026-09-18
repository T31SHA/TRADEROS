"""Adversarial tests for the offline Twelve Data OHLCV admission boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from traderos.data.calendars import TwelveDataForexCalendar
from traderos.data.comparison import compare_close_series
from traderos.data.empirical import (
    AdmissionPolicy,
    AdmissionState,
    RawArtifactStore,
    iter_normalized_ohlcv_records,
    sha256_file,
)
from traderos.data.temporal import TimestampFormat, TimestampSemantics
from traderos.data.twelvedata import (
    TwelveDataImportConfig,
    TwelveDataSchemaError,
    admit_twelve_data_csv,
    instrument_eurusd,
    iter_twelve_data_csv,
    twelve_data_csv_schema,
)

BASE = datetime(2024, 1, 2, tzinfo=UTC)


def _config(**changes: object) -> TwelveDataImportConfig:
    values: dict[str, object] = {"instrument": instrument_eurusd()}
    values.update(changes)
    return TwelveDataImportConfig(**values)  # type: ignore[arg-type]


def _csv(rows: list[str], *, header: str = "datetime,open,high,low,close,volume") -> str:
    return "\n".join([header, *rows]) + "\n"


def _rows(*timestamps: datetime, volume: str = "10") -> list[str]:
    return [
        f"{timestamp.isoformat().replace('+00:00', 'Z')},1.1000,1.1010,1.0990,1.1005,{volume}"
        for timestamp in timestamps
    ]


def _preserve(
    tmp_path: Path,
    content: str,
    *,
    name: str = "eurusd-m15.csv",
    requested_start: datetime = BASE,
    requested_end: datetime = BASE + timedelta(minutes=15),
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / name
    source.write_text(content, encoding="utf-8")
    return RawArtifactStore(tmp_path / "raw").preserve(
        source,
        source="twelve_data",
        source_symbol="EUR/USD",
        requested_start=requested_start,
        requested_end=requested_end,
        requested_timeframe="15min",
        timezone="UTC",
        download_timestamp=BASE + timedelta(days=2),
    )


def _admit(
    tmp_path: Path,
    content: str,
    *,
    policy: AdmissionPolicy | None = None,
    config: TwelveDataImportConfig | None = None,
    requested_start: datetime = BASE,
    requested_end: datetime = BASE + timedelta(minutes=15),
):
    preserved, metadata = _preserve(
        tmp_path,
        content,
        requested_start=requested_start,
        requested_end=requested_end,
    )
    return admit_twelve_data_csv(
        raw_paths=[preserved],
        raw_metadata=[metadata],
        config=config or _config(),
        calendar=TwelveDataForexCalendar(),
        policy=policy or AdmissionPolicy(),
        normalized_dir=tmp_path / "normalized",
        manifest_dir=tmp_path / "manifests",
        created_at=BASE + timedelta(days=3),
    )


def test_twelve_data_csv_parsing_preserves_ohlcv_and_no_quotes(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text(_csv(_rows(BASE, BASE + timedelta(minutes=15))), encoding="utf-8")

    records = tuple(iter_twelve_data_csv(source, config=_config(), artifact_hash="a" * 64))

    assert records[0].timestamp == BASE
    assert records[0].open == Decimal("1.1000")
    assert records[0].volume == Decimal("10")
    assert not hasattr(records[0], "bid")
    assert twelve_data_csv_schema(source) == (
        "header=datetime,open,high,low,close,volume",
        "close=close",
        "high=high",
        "low=low",
        "open=open",
        "timestamp=datetime",
        "volume=volume",
    )


@pytest.mark.parametrize("value", ["2024-01-02 00:00:00", "2024-01-02T00:00:00+03:00"])
def test_timestamp_parsing_rejects_naive_and_non_utc_values(tmp_path: Path, value: str) -> None:
    source = tmp_path / "timestamps.csv"
    source.write_text(_csv([f"{value},1,2,1,1.5,1"]), encoding="utf-8")

    with pytest.raises(TwelveDataSchemaError, match="UTC"):
        next(iter_twelve_data_csv(source, config=_config(), artifact_hash="a" * 64))


def test_timestamp_contract_is_explicit_and_config_rejects_unsafe_variants() -> None:
    assert _config().timestamp_format is TimestampFormat.ISO_8601
    assert _config().timestamp_semantics is TimestampSemantics.BAR_START
    with pytest.raises(ValueError, match="UTC"):
        _config(source_timezone="Africa/Nairobi")
    with pytest.raises(ValueError, match="BAR_START"):
        _config(timestamp_semantics=TimestampSemantics.BAR_END)


def test_invalid_ohlc_nonpositive_and_nonfinite_prices_block(tmp_path: Path) -> None:
    admission = _admit(
        tmp_path,
        _csv(
            [
                "2024-01-02T00:00:00Z,1.1,1.2,0,1.05,1",
                "2024-01-02T00:15:00Z,1.1,NaN,0.9,1.05,1",
            ]
        ),
    )

    assert admission.state is AdmissionState.BLOCKED
    assert admission.quality_report.invalid_ohlc_count == 2
    assert admission.quality_report.nonpositive_price_count == 1
    assert admission.quality_report.nonfinite_price_count == 1
    assert {"invalid_ohlc", "nonpositive_prices", "nonfinite_prices"}.issubset(
        admission.blockers
    )


def test_duplicate_and_non_monotonic_timestamps_are_blockers(tmp_path: Path) -> None:
    admission = _admit(
        tmp_path,
        _csv(_rows(BASE, BASE, BASE - timedelta(minutes=15))),
    )

    assert admission.state is AdmissionState.BLOCKED
    assert admission.quality_report.duplicate_count == 1
    assert admission.quality_report.non_monotonic_count == 2
    assert {"duplicate_timestamps", "non_monotonic_timestamps"}.issubset(
        admission.blockers
    )


def test_weekend_gap_is_expected_but_active_gap_blocks(tmp_path: Path) -> None:
    weekend = _admit(
        tmp_path / "weekend",
        _csv(
            _rows(
                datetime(2024, 1, 5, 21, 45, tzinfo=UTC),
                datetime(2024, 1, 7, 22, tzinfo=UTC),
            )
        ),
        requested_start=datetime(2024, 1, 5, 21, 45, tzinfo=UTC),
        requested_end=datetime(2024, 1, 7, 22, 15, tzinfo=UTC),
    )
    active = _admit(
        tmp_path / "active",
        _csv(_rows(BASE, BASE + timedelta(minutes=30))),
        requested_end=BASE + timedelta(minutes=45),
    )

    assert weekend.quality_report.expected_closures > 0
    assert weekend.quality_report.unexpected_gaps == 0
    assert weekend.state is AdmissionState.EMPIRICALLY_QUALIFIED_DATASET
    assert active.quality_report.unexpected_gaps == 1
    assert "unexpected_gaps" in active.blockers


def test_volume_is_preserved_or_explicitly_unavailable(tmp_path: Path) -> None:
    present = _admit(tmp_path / "present", _csv(_rows(BASE)))
    absent = _admit(
        tmp_path / "absent",
        _csv(
            ["2024-01-02T00:00:00Z,1.1,1.2,1.0,1.15"],
            header="datetime,open,high,low,close",
        ),
    )

    assert present.quality_report.volume_unavailable_count == 0
    assert present.manifest.volume_semantics == "source-provided Forex volume"
    assert absent.quality_report.volume_unavailable_count == 1
    assert absent.state is AdmissionState.EMPIRICALLY_QUALIFIED_DATASET
    assert "volume_unavailable" not in absent.blockers
    required = _admit(
        tmp_path / "required",
        _csv(["2024-01-02T00:00:00Z,1.1,1.2,1.0,1.15"], header="datetime,open,high,low,close"),
        policy=AdmissionPolicy(require_volume=True),
    )
    assert required.state is AdmissionState.BLOCKED
    assert "volume_unavailable" in required.blockers


def test_negative_volume_is_anomaly_and_zero_active_volume_is_blocked(tmp_path: Path) -> None:
    negative = _admit(tmp_path / "negative", _csv(_rows(BASE, volume="-1")))
    zero = _admit(tmp_path / "zero", _csv(_rows(BASE, volume="0")))

    assert "volume_anomalies" in negative.blockers
    assert negative.quality_report.volume_anomaly_count == 1
    assert "active_session_zero_volume" in zero.blockers


def test_malformed_csv_and_schema_drift_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "malformed.csv"
    source.write_text("datetime,open,high,low,close\n2024-01-02T00:00:00Z,1,2\n", encoding="utf-8")
    with pytest.raises(TwelveDataSchemaError, match="low|close"):
        next(iter_twelve_data_csv(source, config=_config(), artifact_hash="a" * 64))

    first_path, first_meta = _preserve(tmp_path / "first", _csv(_rows(BASE)))
    second_path, second_meta = _preserve(
        tmp_path / "second",
        _csv(_rows(BASE + timedelta(minutes=15)), header="datetime,open,high,low,close,vol"),
    )
    admission = admit_twelve_data_csv(
        raw_paths=[first_path, second_path],
        raw_metadata=[first_meta, second_meta],
        config=_config(),
        calendar=TwelveDataForexCalendar(),
        policy=AdmissionPolicy(),
        normalized_dir=tmp_path / "normalized",
        manifest_dir=tmp_path / "manifests",
        created_at=BASE + timedelta(days=3),
    )
    assert admission.quality_report.schema_drift_count == 1
    assert "schema_drift" in admission.blockers


def test_immutable_artifacts_and_deterministic_manifest_identity(tmp_path: Path) -> None:
    content = _csv(_rows(BASE, BASE + timedelta(minutes=15)))
    first = _admit(tmp_path / "first", content)
    second = _admit(tmp_path / "second", content)
    assert first.manifest.dataset_id == second.manifest.dataset_id
    assert first.manifest.canonical() == second.manifest.canonical()

    source = tmp_path / "mutation.csv"
    source.write_text(content, encoding="utf-8")
    store = RawArtifactStore(tmp_path / "mutation-raw")
    preserved, metadata = store.preserve(
        source,
        source="twelve_data",
        source_symbol="EUR/USD",
        requested_start=BASE,
        requested_end=BASE + timedelta(days=1),
        requested_timeframe="15min",
        timezone="UTC",
        download_timestamp=BASE,
    )
    original_bytes = preserved.read_bytes()
    original_hash, original_size = sha256_file(preserved)
    source.write_text(
        _csv(_rows(BASE, BASE + timedelta(minutes=15), volume="11")), encoding="utf-8"
    )
    changed, changed_metadata = store.preserve(
        source,
        source="twelve_data",
        source_symbol="EUR/USD",
        requested_start=BASE,
        requested_end=BASE + timedelta(days=1),
        requested_timeframe="15min",
        timezone="UTC",
        download_timestamp=BASE,
    )
    assert preserved.read_bytes() == original_bytes
    assert metadata.sha256 == original_hash and metadata.byte_size == original_size
    assert changed_metadata.sha256 != metadata.sha256
    assert changed != preserved


def test_future_append_changes_dataset_identity_and_normalized_output_is_readable(
    tmp_path: Path,
) -> None:
    first = _admit(tmp_path / "first", _csv(_rows(BASE)))
    appended = _admit(
        tmp_path / "appended",
        _csv(_rows(BASE, BASE + timedelta(minutes=15))),
    )

    assert first.manifest.dataset_id != appended.manifest.dataset_id
    assert len(tuple(iter_normalized_ohlcv_records(appended.normalized_path))) == 2
    assert first.normalized_path.exists()


def test_cross_source_close_comparison_is_not_a_merge_or_strategy_metric() -> None:
    report = compare_close_series(
        {BASE: Decimal("1.1000"), BASE + timedelta(minutes=15): Decimal("1.1010")},
        {BASE: Decimal("1.1002"), BASE + timedelta(minutes=15): Decimal("1.1000")},
        source_a="twelve_data",
        source_b="dukascopy",
    )

    assert report.overlap_count == 2
    assert report.mean_difference == Decimal("0.0006")
    assert report.max_difference == Decimal("0.0010")
    assert report.signed_mean_difference == Decimal("0.0004")
