"""SQLite integration tests for the PostgreSQL-shaped storage contract."""

from datetime import UTC, datetime
from decimal import Decimal

from traderos.data.bars import MarketBar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import (
    AdjustmentPolicy,
    DatasetMetadata,
    IngestionRun,
    IngestionStatus,
)
from traderos.data.quality import DataQualityEvent, QualityCode, QualitySeverity
from traderos.data.timeframes import Timeframe
from traderos.database.connection import create_database_engine
from traderos.database.store import SqlAlchemyMarketDataStore


def test_sqlalchemy_store_enforces_source_aware_uniqueness_and_range_queries() -> None:
    engine = create_database_engine("sqlite:///:memory:")
    store = SqlAlchemyMarketDataStore(engine)
    store.create_schema()
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        provider_symbols={"local": "EURUSD"},
        trading_currency="USD",
        timezone="UTC",
    )
    timestamp = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
    bar = MarketBar(
        instrument=instrument,
        timeframe=Timeframe.H1,
        timestamp=timestamp,
        open=Decimal("1.1000"),
        high=Decimal("1.1010"),
        low=Decimal("1.0990"),
        close=Decimal("1.1005"),
        source="local",
        currency="USD",
        ingestion_timestamp=timestamp,
    )

    first = store.upsert_bars([bar])
    second = store.upsert_bars([bar])

    assert first.accepted == 1
    assert first.duplicates == 0
    assert second.accepted == 0
    assert second.duplicates == 1
    queried = store.query_bars(
        "EUR/USD",
        Timeframe.H1,
        datetime(2026, 1, 5, 13, 0, tzinfo=UTC),
        datetime(2026, 1, 5, 15, 0, tzinfo=UTC),
    )
    assert len(queried) == 1
    assert queried[0].close == bar.close
    assert queried[0].instrument.provider_symbol("local") == "EURUSD"
    assert store.latest_bar("EUR/USD", Timeframe.H1) == queried[0]
    assert store.has_bar(bar)
    engine.dispose()


def test_raw_and_adjusted_bars_have_distinct_storage_identity() -> None:
    engine = create_database_engine("sqlite:///:memory:")
    store = SqlAlchemyMarketDataStore(engine)
    store.create_schema()
    instrument = Instrument(
        canonical_symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        provider_symbols={"local": "AAPL"},
        trading_currency="USD",
        timezone="America/New_York",
    )
    timestamp = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    common = {
        "instrument": instrument,
        "timeframe": Timeframe.H1,
        "timestamp": timestamp,
        "open": Decimal("100"),
        "high": Decimal("101"),
        "low": Decimal("99"),
        "close": Decimal("100"),
        "source": "local",
        "currency": "USD",
        "ingestion_timestamp": timestamp,
    }
    raw = MarketBar(**common)
    adjusted = MarketBar(
        **common, adjustment_policy=AdjustmentPolicy.ADJUSTED, adjusted_close=Decimal("98")
    )

    result = store.upsert_bars([raw, adjusted])

    assert result.accepted == 2
    assert len(store.query_bars("AAPL", Timeframe.H1, timestamp, timestamp.replace(hour=15))) == 2
    assert (
        len(
            store.query_bars(
                "AAPL",
                Timeframe.H1,
                timestamp,
                timestamp.replace(hour=15),
                adjustment_policy=AdjustmentPolicy.ADJUSTED,
            )
        )
        == 1
    )
    engine.dispose()


def test_lineage_and_quality_records_are_queryable() -> None:
    engine = create_database_engine("sqlite:///:memory:")
    store = SqlAlchemyMarketDataStore(engine)
    store.create_schema()
    timestamp = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    instrument = Instrument(
        canonical_symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        trading_currency="USD",
        timezone="America/New_York",
    )
    store.upsert_bars(
        [
            MarketBar(
                instrument=instrument,
                timeframe=Timeframe.H1,
                timestamp=timestamp,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                source="local",
                currency="USD",
                ingestion_timestamp=timestamp,
            )
        ]
    )
    metadata = DatasetMetadata(
        dataset_version="dataset-test",
        provider="local",
        symbol="AAPL",
        timeframe="1h",
        start=timestamp,
        end=timestamp.replace(hour=16),
        adjustment_policy=AdjustmentPolicy.RAW,
        configuration_version="test",
        quality_status="clean",
        created_at=timestamp,
    )
    event = DataQualityEvent(
        occurred_at=timestamp,
        severity=QualitySeverity.INFO,
        code=QualityCode.DUPLICATE_BAR,
        message="test lineage event",
        source="local",
        symbol="AAPL",
        timeframe="1h",
        bar_timestamp=timestamp,
    )
    run = IngestionRun(
        dataset_version="dataset-test",
        provider="local",
        symbol="AAPL",
        timeframe="1h",
        requested_start=timestamp,
        requested_end=timestamp.replace(hour=16),
        adjustment_policy=AdjustmentPolicy.RAW,
        configuration_version="test",
        started_at=timestamp,
        finished_at=timestamp,
        records_received=1,
        records_accepted=1,
        status=IngestionStatus.SUCCESS,
    )

    store.record_dataset(metadata)
    store.record_quality_events([event])
    store.record_ingestion_run(run)

    assert store.datasets()[0].dataset_version == "dataset-test"
    assert store.quality_events()[0].code is QualityCode.DUPLICATE_BAR
    assert store.ingestion_runs()[0].status is IngestionStatus.SUCCESS
    engine.dispose()
