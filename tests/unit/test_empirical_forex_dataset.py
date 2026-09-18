"""Offline adversarial coverage for the empirical Forex admission gate."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from traderos.data.calendars import DukascopyForexCalendar, ForexCalendar, MarketCalendar
from traderos.data.dukascopy import (
    DukascopyImportConfig,
    DukascopySchemaError,
    QuoteConvention,
    TimestampFormat,
    TimestampSemantics,
    iter_dukascopy_csv,
    iter_normalized_market_bars,
)
from traderos.data.empirical import (
    AdmissionPolicy,
    AdmissionState,
    RawArtifactMetadata,
    RawArtifactStore,
    admit_dukascopy_csv,
    sha256_file,
)
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.timeframes import Timeframe

BASE = datetime(2025, 1, 6, tzinfo=UTC)


def _instrument() -> Instrument:
    return Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        provider_symbols={"dukascopy": "EURUSD"},
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
    )


def _config(
    timezone: str = "UTC",
    semantics: TimestampSemantics = TimestampSemantics.BAR_START,
    *,
    timestamp_format: TimestampFormat = TimestampFormat.ISO_8601,
    price_tick_size: Decimal | None = None,
    quantization_policy_id: str = "dukascopy-fixed-price-quantization",
    quantization_policy_version: str = "v1",
) -> DukascopyImportConfig:
    return DukascopyImportConfig(
        instrument=_instrument(),
        timeframe=Timeframe.M15,
        source_symbol="EURUSD",
        source_timezone=timezone,
        timestamp_semantics=semantics,
        quote_convention=QuoteConvention.BID,
        timestamp_format=timestamp_format,
        price_tick_size=price_tick_size,
        quantization_policy_id=quantization_policy_id,
        quantization_policy_version=quantization_policy_version,
    )


def _csv(rows: list[str]) -> str:
    return "\n".join(
        [
            "timestamp,bid_open,bid_high,bid_low,bid_close,ask_open,ask_high,ask_low,ask_close,bid_volume,ask_volume",
            *rows,
            "",
        ]
    )


def _bid_only_csv(rows: list[str]) -> str:
    return "\n".join(["timestamp,open,high,low,close", *rows, ""])


def _node_bid_csv(rows: list[str]) -> str:
    return "\n".join(["timestamp,open,high,low,close,volume", *rows, ""])


def _metadata(path: Path, *, timezone: str = "UTC") -> RawArtifactMetadata:
    digest, size = sha256_file(path)
    return RawArtifactMetadata(
        source="dukascopy",
        source_symbol="EURUSD",
        requested_start=BASE,
        requested_end=BASE + timedelta(hours=1),
        requested_timeframe="15m",
        timezone=timezone,
        original_filename=path.name,
        download_timestamp=BASE + timedelta(days=1),
        sha256=digest,
        byte_size=size,
        source_version="test-source-v1",
    )


def _admit(
    tmp_path: Path,
    rows: list[str],
    *,
    policy: AdmissionPolicy | None = None,
    config: DukascopyImportConfig | None = None,
    content: str | None = None,
    calendar: MarketCalendar | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "eurusd.csv"
    source.write_text(content if content is not None else _csv(rows), encoding="utf-8")
    return admit_dukascopy_csv(
        raw_paths=(source,),
        raw_metadata=(_metadata(source),),
        config=config or _config(),
        calendar=calendar or ForexCalendar(),
        policy=policy or AdmissionPolicy(require_bid_ask=True),
        normalized_dir=tmp_path / "normalized",
        manifest_dir=tmp_path / "manifests",
        created_at=BASE + timedelta(days=2),
        deterministic_test_fixture=True,
    )


def test_fixture_admission_preserves_quotes_audits_spread_and_is_immutable(tmp_path: Path) -> None:
    admission = _admit(
        tmp_path,
        [
            "2025-01-06T00:00:00,1.1000,1.1010,1.0990,1.1005,1.1002,1.1012,1.0992,1.1007,10,11",
            "2025-01-06T00:15:00,1.1005,1.1015,1.1000,1.1010,1.1007,1.1017,1.1002,1.1012,12,13",
        ],
    )

    assert admission.state is AdmissionState.DETERMINISTIC_TEST_FIXTURE
    assert admission.quality_report.min_spread == admission.quality_report.max_spread
    assert admission.quality_report.unexpected_gaps == 0
    assert admission.manifest.timestamp_semantics is TimestampSemantics.BAR_START
    assert admission.manifest_path.exists() and admission.normalized_path.exists()
    assert "bid_open" in admission.normalized_path.read_text(encoding="utf-8")
    loaded = tuple(
        iter_normalized_market_bars(
            admission.normalized_path,
            config=_config(),
            ingestion_timestamp=BASE,
        )
    )
    assert len(loaded) == 2 and loaded[0].timestamp == BASE


def test_raw_artifact_hash_is_content_addressed_and_mutation_changes_dataset_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.csv"
    source.write_text(_csv(["2025-01-06T00:00:00,1,2,1,1.5,1.1,2.1,1.1,1.6,,"]), encoding="utf-8")
    store = RawArtifactStore(tmp_path / "raw")
    preserved, metadata = store.preserve(
        source,
        source="dukascopy",
        source_symbol="EURUSD",
        requested_start=BASE,
        requested_end=BASE + timedelta(minutes=15),
        requested_timeframe="15m",
        timezone="UTC",
        download_timestamp=BASE,
    )
    first = admit_dukascopy_csv(
        raw_paths=(preserved,),
        raw_metadata=(metadata,),
        config=_config(),
        calendar=ForexCalendar(),
        policy=AdmissionPolicy(require_bid_ask=True),
        normalized_dir=tmp_path / "normalized",
        manifest_dir=tmp_path / "manifests",
        created_at=BASE,
        deterministic_test_fixture=True,
    )
    source.write_text(_csv(["2025-01-06T00:00:00,1,2,1,1.6,1.1,2.1,1.1,1.7,,"]), encoding="utf-8")
    changed, changed_metadata = store.preserve(
        source,
        source="dukascopy",
        source_symbol="EURUSD",
        requested_start=BASE,
        requested_end=BASE + timedelta(minutes=15),
        requested_timeframe="15m",
        timezone="UTC",
        download_timestamp=BASE + timedelta(days=1),
    )
    second = admit_dukascopy_csv(
        raw_paths=(changed,),
        raw_metadata=(changed_metadata,),
        config=_config(),
        calendar=ForexCalendar(),
        policy=AdmissionPolicy(require_bid_ask=True),
        normalized_dir=tmp_path / "normalized",
        manifest_dir=tmp_path / "manifests",
        created_at=BASE,
        deterministic_test_fixture=True,
    )
    assert first.manifest.dataset_id != second.manifest.dataset_id
    assert first.normalized_path.exists()


def test_future_append_and_timestamp_mutation_create_new_ids_without_altering_original(
    tmp_path: Path,
) -> None:
    first = _admit(
        tmp_path / "first",
        ["2025-01-06T00:00:00,1,2,1,1.5,1.1,2.1,1.1,1.6,,"],
    )
    appended = _admit(
        tmp_path / "append",
        [
            "2025-01-06T00:00:00,1,2,1,1.5,1.1,2.1,1.1,1.6,,",
            "2025-01-06T00:15:00,1,2,1,1.5,1.1,2.1,1.1,1.6,,",
        ],
    )
    timestamp_changed = _admit(
        tmp_path / "timestamp",
        ["2025-01-06T00:15:00,1,2,1,1.5,1.1,2.1,1.1,1.6,,"],
    )
    assert (
        len(
            {
                first.manifest.dataset_id,
                appended.manifest.dataset_id,
                timestamp_changed.manifest.dataset_id,
            }
        )
        == 3
    )
    assert first.normalized_path.exists()


def test_source_adapter_maps_the_declared_side_to_existing_market_bar(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text(
        _csv(["2025-01-06T00:00:00,1,2,1,1.5,1.1,2.1,1.1,1.6,10,11"]),
        encoding="utf-8",
    )
    record = next(iter_dukascopy_csv(source, config=_config(), artifact_hash="a" * 64))
    bar = record.to_market_bar(config=_config(), ingestion_timestamp=BASE)
    assert (bar.open, bar.close, bar.bid, bar.ask) == (
        Decimal("1"),
        Decimal("1.5"),
        Decimal("1.5"),
        Decimal("1.6"),
    )


def test_bar_end_source_timestamp_is_normalized_to_phase_one_bar_start(tmp_path: Path) -> None:
    source = tmp_path / "bar-end.csv"
    source.write_text(_csv(["2025-01-06T00:15:00,1,2,1,1.5,1.1,2.1,1.1,1.6,,"]), encoding="utf-8")
    record = next(
        iter_dukascopy_csv(
            source,
            config=_config(semantics=TimestampSemantics.BAR_END),
            artifact_hash="a" * 64,
        )
    )
    assert record.timestamp == BASE


def test_epoch_milliseconds_are_converted_to_exact_utc_bar_start(tmp_path: Path) -> None:
    source = tmp_path / "epoch.csv"
    source.write_text(
        _bid_only_csv(
            [
                "1704067200000,1.10429,1.10429,1.10429,1.10429",
                "1704068100000,1.10429,1.10429,1.10429,1.10429",
            ]
        ),
        encoding="utf-8",
    )
    config = DukascopyImportConfig(
        instrument=_instrument(),
        timeframe=Timeframe.M15,
        source_symbol="EURUSD",
        source_timezone="UTC",
        timestamp_semantics=TimestampSemantics.BAR_START,
        quote_convention=QuoteConvention.BID,
        timestamp_format=TimestampFormat.EPOCH_MILLISECONDS,
    )
    records = tuple(iter_dukascopy_csv(source, config=config, artifact_hash="a" * 64))
    assert [record.timestamp for record in records] == [
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 15, tzinfo=UTC),
    ]
    assert all(record.timestamp.tzinfo == UTC for record in records)
    assert records == tuple(iter_dukascopy_csv(source, config=config, artifact_hash="a" * 64))


def test_iso_8601_remains_the_default_timestamp_format(tmp_path: Path) -> None:
    source = tmp_path / "iso.csv"
    source.write_text(_bid_only_csv(["2024-01-01T00:00:00Z,1,1,1,1"]), encoding="utf-8")
    config = _config()
    record = next(iter_dukascopy_csv(source, config=config, artifact_hash="a" * 64))
    assert config.timestamp_format is TimestampFormat.ISO_8601
    assert record.timestamp == datetime(2024, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("value", ["", "1704067200000.0", "not-a-timestamp", "9" * 100])
def test_invalid_epoch_milliseconds_fail_closed(tmp_path: Path, value: str) -> None:
    source = tmp_path / "invalid-epoch.csv"
    source.write_text(_bid_only_csv([f"{value},1,1,1,1"]), encoding="utf-8")
    config = DukascopyImportConfig(
        instrument=_instrument(),
        timeframe=Timeframe.M15,
        source_symbol="EURUSD",
        source_timezone="UTC",
        timestamp_semantics=TimestampSemantics.BAR_START,
        quote_convention=QuoteConvention.BID,
        timestamp_format=TimestampFormat.EPOCH_MILLISECONDS,
    )
    with pytest.raises(DukascopySchemaError, match="epoch-millisecond|missing"):
        next(iter_dukascopy_csv(source, config=config, artifact_hash="a" * 64))


def test_real_source_schema_preserves_bid_only_and_unavailable_fields(tmp_path: Path) -> None:
    source = tmp_path / "eurusd-m15-bid.csv"
    source.write_text(
        _bid_only_csv(
            [
                "1704067200000,1.10429,1.10429,1.10429,1.10429",
                "1704068100000,1.10429,1.10429,1.10429,1.10429",
            ]
        ),
        encoding="utf-8",
    )
    original_bytes = source.read_bytes()
    store = RawArtifactStore(tmp_path / "raw")
    preserved, metadata = store.preserve(
        source,
        source="dukascopy",
        source_symbol="EURUSD",
        requested_start=datetime(2024, 1, 1, tzinfo=UTC),
        requested_end=datetime(2025, 1, 1, tzinfo=UTC),
        requested_timeframe="15m",
        timezone="UTC",
        download_timestamp=BASE,
    )
    config = DukascopyImportConfig(
        instrument=_instrument(),
        timeframe=Timeframe.M15,
        source_symbol="EURUSD",
        source_timezone="UTC",
        timestamp_semantics=TimestampSemantics.BAR_START,
        quote_convention=QuoteConvention.BID,
        timestamp_format=TimestampFormat.EPOCH_MILLISECONDS,
    )
    admission = admit_dukascopy_csv(
        raw_paths=(preserved,),
        raw_metadata=(metadata,),
        config=config,
        calendar=ForexCalendar(),
        policy=AdmissionPolicy(),
        normalized_dir=tmp_path / "normalized",
        manifest_dir=tmp_path / "manifests",
        created_at=BASE,
        deterministic_test_fixture=True,
    )
    record = next(
        iter_normalized_market_bars(
            admission.normalized_path,
            config=config,
            ingestion_timestamp=BASE,
        )
    )
    assert record.timestamp == datetime(2024, 1, 1, tzinfo=UTC)
    assert record.ask is None and record.bid == Decimal("1.10429")
    assert record.volume is None
    normalized_text = admission.normalized_path.read_text(encoding="utf-8")
    assert '"ask_close":null' in normalized_text
    assert '"bid_volume":null' in normalized_text
    assert '"ask_volume":null' in normalized_text
    assert source.read_bytes() == original_bytes
    assert sha256_file(source) == sha256_file(preserved) == (metadata.sha256, metadata.byte_size)
    assert admission.manifest.timestamp_format is TimestampFormat.EPOCH_MILLISECONDS


@pytest.mark.parametrize(
    ("rows", "blocker"),
    [
        (
            [
                "2025-01-06T00:00:00,1,2,1,1.5,1.1,2.1,1.1,1.6,,",
                "2025-01-06T00:00:00,1,2,1,1.5,1.1,2.1,1.1,1.6,,",
            ],
            "duplicate_timestamps",
        ),
        (["2025-01-06T00:00:00,1.2,1.1,1,1.05,1.3,1.4,1.1,1.2,,"], "material_ohlc_corruption"),
        (["2025-01-06T00:00:00,1,2,1,1.7,1.1,2.1,1.1,1.6,,"], "crossed_market_corruption"),
        (
            [
                "2025-01-06T00:00:00,1,2,1,1.5,1.1,2.1,1.1,1.6,,",
                "2025-01-06T00:30:00,1,2,1,1.5,1.1,2.1,1.1,1.6,,",
            ],
            "unexpected_data_gaps",
        ),
    ],
)
def test_quality_failures_are_explicit_blockers(
    tmp_path: Path, rows: list[str], blocker: str
) -> None:
    admission = _admit(tmp_path, rows)
    assert blocker in admission.blockers


def test_weekend_closure_is_not_a_data_gap(tmp_path: Path) -> None:
    friday = "2025-01-03T21:45:00,1,2,1,1.5,1.1,2.1,1.1,1.6,,"
    sunday = "2025-01-05T22:00:00,1,2,1,1.5,1.1,2.1,1.1,1.6,,"
    admission = _admit(tmp_path, [friday, sunday])
    assert admission.quality_report.unexpected_gaps == 0
    assert admission.quality_report.expected_closures > 0


def test_schema_drift_and_overlapping_artifacts_are_blocked(tmp_path: Path) -> None:
    first = tmp_path / "one.csv"
    second = tmp_path / "two.csv"
    first.write_text(_csv(["2025-01-06T00:00:00,1,2,1,1.5,1.1,2.1,1.1,1.6,,"]), encoding="utf-8")
    second.write_text(
        "timestamp,open,high,low,close\n2025-01-06T00:00:00,1,2,1,1.5\n", encoding="utf-8"
    )
    admission = admit_dukascopy_csv(
        raw_paths=(first, second),
        raw_metadata=(_metadata(first), _metadata(second)),
        config=_config(),
        calendar=ForexCalendar(),
        policy=AdmissionPolicy(),
        normalized_dir=tmp_path / "normalized",
        manifest_dir=tmp_path / "manifests",
        created_at=BASE,
        deterministic_test_fixture=True,
    )
    assert {
        "duplicate_timestamps",
        "schema_drift",
        "conflicting_overlapping_records",
    } <= set(admission.blockers)


def test_clean_nonfixture_can_be_empirically_qualified_only_by_the_gate(tmp_path: Path) -> None:
    source = tmp_path / "eurusd.csv"
    source.write_text(_csv(["2025-01-06T00:00:00,1,2,1,1.5,1.1,2.1,1.1,1.6,,"]), encoding="utf-8")
    admission = admit_dukascopy_csv(
        raw_paths=(source,),
        raw_metadata=(_metadata(source),),
        config=_config(),
        calendar=ForexCalendar(),
        policy=AdmissionPolicy(require_bid_ask=True),
        normalized_dir=tmp_path / "normalized",
        manifest_dir=tmp_path / "manifests",
        created_at=BASE,
        deterministic_test_fixture=False,
    )
    assert admission.state is AdmissionState.EMPIRICALLY_QUALIFIED_DATASET


def test_unknown_timezone_and_malformed_schema_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "bad.csv"
    source.write_text(
        "timestamp,open,high,low,close\n2025-01-06T00:00:00,1,2,1,1.5\n", encoding="utf-8"
    )
    with pytest.raises(DukascopySchemaError, match="Unknown source timezone"):
        admit_dukascopy_csv(
            raw_paths=(source,),
            raw_metadata=(_metadata(source, timezone="Mars/Unknown"),),
            config=_config("Mars/Unknown"),
            calendar=ForexCalendar(),
            policy=AdmissionPolicy(),
            normalized_dir=tmp_path / "normalized",
            manifest_dir=tmp_path / "manifests",
            created_at=BASE,
            deterministic_test_fixture=True,
        )


def test_dukascopy_node_bare_volume_maps_only_to_selected_bid_side(tmp_path: Path) -> None:
    source = tmp_path / "node.csv"
    source.write_text(
        _node_bid_csv(["1704067200000,1.10429,1.10429,1.10429,1.10429,12.3400"]),
        encoding="utf-8",
    )
    config = _config(timestamp_format=TimestampFormat.EPOCH_MILLISECONDS)
    record = next(iter_dukascopy_csv(source, config=config, artifact_hash="a" * 64))
    bar = record.to_market_bar(config=config, ingestion_timestamp=BASE)

    assert record.bid_volume == Decimal("12.3400")
    assert record.ask_volume is None
    assert bar.volume == Decimal("12.3400")
    assert bar.ask is None


def test_v2_schema_preserves_flat_zero_volume_closure_bar(tmp_path: Path) -> None:
    admission = _admit(
        tmp_path,
        [],
        content=_node_bid_csv(["1735948800000,1,1,1,1,0"]),  # 2025-01-04 00:00 UTC
        config=_config(timestamp_format=TimestampFormat.EPOCH_MILLISECONDS),
        policy=AdmissionPolicy(),
    )

    assert admission.quality_report.row_count == 1
    assert admission.quality_report.raw_row_count == 1
    assert admission.quality_report.normalized_row_count == 1
    assert admission.quality_report.zero_volume_bar_count == 1
    assert admission.quality_report.flat_bar_count == 1
    assert admission.quality_report.flat_zero_volume_count == 1
    assert admission.quality_report.expected_closure_bar_count == 1
    assert admission.quality_report.active_session_zero_volume_count == 0
    assert admission.blockers == ()


def test_active_session_zero_volume_is_a_blocker(tmp_path: Path) -> None:
    admission = _admit(
        tmp_path,
        [],
        content=_node_bid_csv(["1736121600000,1,1,1,1,0"]),  # 2025-01-06 00:00 UTC
        config=_config(timestamp_format=TimestampFormat.EPOCH_MILLISECONDS),
        policy=AdmissionPolicy(),
    )

    assert admission.quality_report.active_session_zero_volume_count == 1
    assert "active_session_zero_volume" in admission.blockers


def test_one_tick_ohlc_inversion_is_normalized_with_provenance(tmp_path: Path) -> None:
    admission = _admit(
        tmp_path,
        ["2025-01-06T00:00:00,1.00000,0.99999,0.99990,0.99995,1.0001,1.0002,1.0000,1.0001,,"],
        config=_config(price_tick_size=Decimal("0.00001")),
    )
    payload = json.loads(admission.normalized_path.read_text(encoding="utf-8"))

    assert admission.state is AdmissionState.DETERMINISTIC_TEST_FIXTURE
    assert admission.quality_report.invalid_ohlc_before_normalization_count == 1
    assert admission.quality_report.invalid_ohlc_after_normalization_count == 0
    assert admission.quality_report.invalid_ohlc_count == 0
    assert admission.quality_report.quantization_adjustment_count == 1
    assert admission.quality_report.quantization_adjustment_rows == (2,)
    assert admission.quality_report.quantization_adjustment_max_ticks == 1
    assert payload["bid_high"] == "1.00000"
    assert payload["quantization_adjusted"] is True
    assert payload["quantization_adjustment_fields"] == ["bid_high"]
    assert payload["raw_artifact_hash"] == admission.manifest.source_artifact_hashes[0]
    assert payload["raw_row_number"] == 2
    assert payload["quantization_raw_values"]["bid_high"] == "0.99999"


@pytest.mark.parametrize(
    ("row", "expected_blocker"),
    [
        (
            "2025-01-06T00:00:00,1.00002,1.00000,0.99990,1.00000,1.0001,1.0002,1.0000,1.0001,,",
            "material_ohlc_corruption",
        ),
        (
            "2025-01-06T00:00:00,1.00000,0.99999,1.00001,1.00000,1.0001,1.0002,1.0000,1.0001,,",
            "material_ohlc_corruption",
        ),
        (
            "2025-01-06T00:00:00,1.00000,NaN,0.99990,0.99995,1.0001,1.0002,1.0000,1.0001,,",
            "nonfinite_prices",
        ),
        (
            "2025-01-06T00:00:00,1.00000,-0.99999,0.99990,0.99995,1.0001,1.0002,1.0000,1.0001,,",
            "nonpositive_prices",
        ),
    ],
)
def test_quantization_adversarial_rows_remain_blocked(
    tmp_path: Path, row: str, expected_blocker: str
) -> None:
    admission = _admit(
        tmp_path,
        [row],
        config=_config(price_tick_size=Decimal("0.00001")),
    )

    assert admission.quality_report.quantization_adjustment_count == 0
    assert expected_blocker in admission.blockers


def test_price_tick_and_quantization_policy_participate_in_dataset_identity(tmp_path: Path) -> None:
    row = "2025-01-06T00:00:00,1.00000,0.99999,0.99990,0.99995,1.0001,1.0002,1.0000,1.0001,,"
    first = _admit(
        tmp_path / "first",
        [row],
        config=_config(price_tick_size=Decimal("0.00001")),
    )
    changed_tick = _admit(
        tmp_path / "tick",
        [row],
        config=_config(price_tick_size=Decimal("0.0001")),
    )
    changed_policy = _admit(
        tmp_path / "policy",
        [row],
        config=_config(
            price_tick_size=Decimal("0.00001"),
            quantization_policy_version="v2",
        ),
    )

    assert first.manifest.dataset_id != changed_tick.manifest.dataset_id
    assert first.manifest.dataset_id != changed_policy.manifest.dataset_id
    assert first.manifest.price_tick_size == Decimal("0.00001")
    assert first.manifest.quantization_policy_version == "v1"


def test_calendar_identity_participates_in_dataset_identity(tmp_path: Path) -> None:
    row = "2025-01-06T00:00:00,1,2,1,1.5,1.0001,2.0001,1.0001,1.5001,,"
    fixed = _admit(tmp_path / "fixed", [row], policy=AdmissionPolicy(require_bid_ask=True))
    dukascopy = _admit(
        tmp_path / "dukascopy",
        [row],
        policy=AdmissionPolicy(require_bid_ask=True),
        calendar=DukascopyForexCalendar(),
    )

    assert fixed.manifest.calendar_id == "forex-weekday-utc-v1"
    assert dukascopy.manifest.calendar_id == "dukascopy-forex-utc-session-v1"
    assert fixed.quality_report.calendar_id == "forex-weekday-utc-v1"
    assert dukascopy.quality_report.calendar_id == "dukascopy-forex-utc-session-v1"
    assert fixed.manifest.dataset_id != dukascopy.manifest.dataset_id


def test_quantization_is_not_applied_when_another_source_price_is_nonfinite(
    tmp_path: Path,
) -> None:
    admission = _admit(
        tmp_path,
        [
            "2025-01-06T00:00:00,1.00000,0.99999,0.99990,0.99995,NaN,1.0002,1.0000,1.0001,,"
        ],
        config=_config(price_tick_size=Decimal("0.00001")),
    )

    assert admission.quality_report.quantization_adjustment_count == 0
    assert "nonfinite_prices" in admission.blockers
