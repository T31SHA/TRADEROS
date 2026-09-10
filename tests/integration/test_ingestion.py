"""Ingestion, lineage, idempotency, and provider failure tests."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from traderos.data.bars import BarCandidate
from traderos.data.calendars import UsEquityCalendar
from traderos.data.errors import ProviderNetworkError, ProviderTimeoutError
from traderos.data.ingestion import IngestionConfig, IngestionRequest, IngestionService
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import IngestionStatus
from traderos.data.providers.base import HistoricalBarsRequest, ProviderHealth
from traderos.data.providers.local import DeterministicLocalProvider
from traderos.data.quality import QualityCode
from traderos.data.storage import InMemoryMarketDataStore
from traderos.data.timeframes import Timeframe


def instrument() -> Instrument:
    return Instrument(
        canonical_symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        provider_symbols={"local": "AAPL"},
        exchange="NASDAQ",
        trading_currency="USD",
        timezone="America/New_York",
    )


def candidate(timestamp: datetime, close: str = "100") -> BarCandidate:
    price = Decimal(close)
    return BarCandidate(
        instrument=instrument(),
        timeframe=Timeframe.H1,
        timestamp=timestamp,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=Decimal("1000"),
    )


def request() -> IngestionRequest:
    return IngestionRequest(
        instrument=instrument(),
        timeframe=Timeframe.H1,
        start=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
        end=datetime(2026, 1, 5, 17, 30, tzinfo=UTC),
    )


def service(
    candidates: list[BarCandidate],
    store: InMemoryMarketDataStore | None = None,
) -> tuple[IngestionService, InMemoryMarketDataStore]:
    data_store = store or InMemoryMarketDataStore()
    return (
        IngestionService(
            DeterministicLocalProvider(candidates, [instrument()]),
            data_store,
            UsEquityCalendar(),
            config=IngestionConfig(max_retries=2),
            clock=lambda: datetime(2026, 1, 5, 18, 0, tzinfo=UTC),
        ),
        data_store,
    )


def test_ingestion_is_idempotent_and_dataset_version_is_stable() -> None:
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    candidates = [candidate(start), candidate(start + timedelta(hours=1))]
    ingestion, store = service(candidates)

    first = ingestion.ingest(request())
    second = ingestion.ingest(request())

    assert first.status is IngestionStatus.PARTIAL  # one expected bar is absent
    assert second.status is IngestionStatus.PARTIAL
    assert first.dataset_version == second.dataset_version
    assert len(store.query_bars("AAPL", Timeframe.H1, request().start, request().end)) == 2
    assert second.duplicates == 2
    assert any(event.code is QualityCode.MISSING_BAR for event in store.quality_events())


def test_invalid_ohlc_is_rejected_and_not_persisted() -> None:
    invalid = BarCandidate.model_construct(
        instrument=instrument(),
        timeframe=Timeframe.H1,
        timestamp=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("99"),
        low=Decimal("98"),
        close=Decimal("99"),
    )
    ingestion, store = service([invalid])

    run = ingestion.ingest(request())

    assert run.records_rejected == 1
    assert run.records_accepted == 0
    assert store.query_bars("AAPL", Timeframe.H1, request().start, request().end) == ()
    assert any(event.code is QualityCode.INVALID_OHLC for event in store.quality_events())


def test_naive_timestamp_without_source_timezone_is_rejected() -> None:
    naive = candidate(datetime(2026, 1, 5, 14, 30))
    ingestion, store = service([naive])

    run = ingestion.ingest(request())

    assert run.records_rejected == 1
    assert any(event.code is QualityCode.TIMEZONE_INCONSISTENCY for event in store.quality_events())


def test_provider_bar_outside_requested_range_is_rejected() -> None:
    outside = candidate(datetime(2026, 1, 5, 18, 30, tzinfo=UTC))

    class OutOfRangeProvider(DeterministicLocalProvider):
        def get_historical_bars(self, request: HistoricalBarsRequest) -> tuple[BarCandidate, ...]:
            del request
            return (outside,)

    store = InMemoryMarketDataStore()
    ingestion = IngestionService(
        OutOfRangeProvider([outside], [instrument()]),
        store,
        UsEquityCalendar(),
        config=IngestionConfig(max_retries=0),
        clock=lambda: datetime(2026, 1, 5, 18, 0, tzinfo=UTC),
    )

    run = ingestion.ingest(request())

    assert run.records_rejected == 1
    assert store.query_bars("AAPL", Timeframe.H1, request().start, request().end) == ()
    assert any(event.code is QualityCode.PROVIDER_INCONSISTENCY for event in store.quality_events())


def test_empty_provider_response_is_recorded_as_partial_quality_failure() -> None:
    ingestion, store = service([])

    run = ingestion.ingest(request())

    assert run.status is IngestionStatus.PARTIAL
    assert run.errors >= 1
    assert any(event.code is QualityCode.PROVIDER_INCONSISTENCY for event in store.quality_events())


def test_duplicate_provider_records_are_reported_without_duplicate_storage() -> None:
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    ingestion, store = service([candidate(start), candidate(start)])

    run = ingestion.ingest(request())

    assert run.duplicates == 1
    assert len(store.query_bars("AAPL", Timeframe.H1, request().start, request().end)) == 1
    assert any(event.code is QualityCode.DUPLICATE_BAR for event in store.quality_events())


def test_negative_volume_is_rejected_as_quality_error() -> None:
    invalid = BarCandidate.model_construct(
        instrument=instrument(),
        timeframe=Timeframe.H1,
        timestamp=datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("-1"),
    )
    ingestion, store = service([invalid])

    run = ingestion.ingest(request())

    assert run.records_rejected == 1
    assert any(event.code is QualityCode.NEGATIVE_VOLUME for event in store.quality_events())


class FlakyProvider:
    provider_id = "flaky"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def get_instruments(self) -> tuple[Instrument, ...]:
        return (instrument(),)

    def get_historical_bars(self, request: HistoricalBarsRequest) -> tuple[BarCandidate, ...]:
        del request
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderTimeoutError("temporary timeout")
        return (candidate(datetime(2026, 1, 5, 14, 30, tzinfo=UTC)),)

    def get_latest_bar(self, instrument: Instrument, timeframe: Timeframe) -> BarCandidate | None:
        del instrument, timeframe
        return None

    def health_check(self) -> ProviderHealth:
        return ProviderHealth(provider=self.provider_id, healthy=True, message="test")


def test_transient_provider_failures_are_retried_with_a_bound() -> None:
    provider = FlakyProvider(failures=2)
    store = InMemoryMarketDataStore()
    ingestion = IngestionService(
        provider,
        store,
        UsEquityCalendar(),
        config=IngestionConfig(max_retries=2),
        clock=lambda: datetime(2026, 1, 5, 18, 0, tzinfo=UTC),
    )

    run = ingestion.ingest(request())

    assert provider.calls == 3
    assert run.records_received == 1


def test_transient_provider_failure_is_not_retried_indefinitely() -> None:
    provider = FlakyProvider(failures=10)
    store = InMemoryMarketDataStore()
    ingestion = IngestionService(
        provider,
        store,
        UsEquityCalendar(),
        config=IngestionConfig(max_retries=2),
        clock=lambda: datetime(2026, 1, 5, 18, 0, tzinfo=UTC),
    )

    with pytest.raises(ProviderTimeoutError):
        ingestion.ingest(request())

    assert provider.calls == 3
    assert store.ingestion_runs()[0].status is IngestionStatus.FAILED


def test_network_failure_type_is_preserved() -> None:
    class NetworkProvider(FlakyProvider):
        def get_historical_bars(self, request: HistoricalBarsRequest) -> tuple[BarCandidate, ...]:
            del request
            raise ProviderNetworkError("network down")

    ingestion = IngestionService(
        NetworkProvider(0),
        InMemoryMarketDataStore(),
        UsEquityCalendar(),
        config=IngestionConfig(max_retries=0),
        clock=lambda: datetime(2026, 1, 5, 18, 0, tzinfo=UTC),
    )

    with pytest.raises(ProviderNetworkError):
        ingestion.ingest(request())
