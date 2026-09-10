"""Storage contracts and a deterministic in-memory implementation."""

from collections.abc import Sequence
from datetime import datetime
from threading import RLock
from typing import Protocol

from traderos.data.bars import MarketBar
from traderos.data.lineage import AdjustmentPolicy, DatasetMetadata, IngestionRun
from traderos.data.quality import DataQualityEvent
from traderos.data.timeframes import Timeframe


class StoreWriteResult:
    """Result of an idempotent batch write."""

    def __init__(
        self,
        accepted: int,
        duplicate_keys: tuple[tuple[str, str, datetime, str, str], ...],
    ) -> None:
        self.accepted = accepted
        self.duplicate_keys = duplicate_keys

    @property
    def duplicates(self) -> int:
        return len(self.duplicate_keys)


class MarketDataStore(Protocol):
    """Authoritative storage contract used by ingestion and future consumers."""

    def upsert_bars(self, bars: Sequence[MarketBar]) -> StoreWriteResult:
        """Persist a batch while enforcing source-aware bar uniqueness."""

    def query_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        source: str | None = None,
        adjustment_policy: AdjustmentPolicy | None = None,
    ) -> tuple[MarketBar, ...]:
        """Return a bounded, timestamp-ordered range."""

    def latest_bar(
        self,
        symbol: str,
        timeframe: Timeframe,
        source: str | None = None,
        adjustment_policy: AdjustmentPolicy | None = None,
    ) -> MarketBar | None:
        """Return the newest stored bar for a logical series."""

    def has_bar(self, bar: MarketBar) -> bool:
        """Return whether the source-aware logical bar exists."""

    def record_quality_events(self, events: Sequence[DataQualityEvent]) -> None:
        """Persist quality findings without deleting data."""

    def quality_events(self) -> tuple[DataQualityEvent, ...]:
        """Return persisted quality findings."""

    def record_dataset(self, metadata: DatasetMetadata) -> None:
        """Persist dataset lineage metadata."""

    def datasets(self) -> tuple[DatasetMetadata, ...]:
        """Return persisted dataset lineage metadata."""

    def record_ingestion_run(self, run: IngestionRun) -> None:
        """Persist or replace an ingestion-run summary."""

    def ingestion_runs(self) -> tuple[IngestionRun, ...]:
        """Return persisted ingestion-run summaries."""


class InMemoryMarketDataStore:
    """Thread-safe test/offline store with the same logical contract as SQL storage."""

    def __init__(self) -> None:
        self._bars: dict[tuple[str, str, datetime, str, str], MarketBar] = {}
        self._quality_events: list[DataQualityEvent] = []
        self._datasets: dict[str, DatasetMetadata] = {}
        self._runs: dict[str, IngestionRun] = {}
        self._lock = RLock()

    def upsert_bars(self, bars: Sequence[MarketBar]) -> StoreWriteResult:
        duplicate_keys: list[tuple[str, str, datetime, str, str]] = []
        accepted = 0
        with self._lock:
            for bar in bars:
                key = (
                    bar.symbol,
                    bar.timeframe.value,
                    bar.timestamp,
                    bar.source,
                    bar.adjustment_policy.value,
                )
                if key in self._bars:
                    duplicate_keys.append(key)
                else:
                    self._bars[key] = bar
                    accepted += 1
        return StoreWriteResult(accepted, tuple(duplicate_keys))

    def query_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        source: str | None = None,
        adjustment_policy: AdjustmentPolicy | None = None,
    ) -> tuple[MarketBar, ...]:
        with self._lock:
            bars = [
                bar
                for bar in self._bars.values()
                if bar.symbol == symbol
                and bar.timeframe is timeframe
                and start <= bar.timestamp < end
                and (source is None or bar.source == source)
                and (adjustment_policy is None or bar.adjustment_policy is adjustment_policy)
            ]
        return tuple(sorted(bars, key=lambda bar: bar.timestamp))

    def latest_bar(
        self,
        symbol: str,
        timeframe: Timeframe,
        source: str | None = None,
        adjustment_policy: AdjustmentPolicy | None = None,
    ) -> MarketBar | None:
        bars = [
            bar
            for bar in self._bars.values()
            if bar.symbol == symbol
            and bar.timeframe is timeframe
            and (source is None or bar.source == source)
            and (adjustment_policy is None or bar.adjustment_policy is adjustment_policy)
        ]
        return max(bars, key=lambda bar: bar.timestamp) if bars else None

    def has_bar(self, bar: MarketBar) -> bool:
        key = (
            bar.symbol,
            bar.timeframe.value,
            bar.timestamp,
            bar.source,
            bar.adjustment_policy.value,
        )
        with self._lock:
            return key in self._bars

    def record_quality_events(self, events: Sequence[DataQualityEvent]) -> None:
        with self._lock:
            self._quality_events.extend(events)

    def quality_events(self) -> tuple[DataQualityEvent, ...]:
        with self._lock:
            return tuple(self._quality_events)

    def record_dataset(self, metadata: DatasetMetadata) -> None:
        with self._lock:
            self._datasets[metadata.dataset_version] = metadata

    def datasets(self) -> tuple[DatasetMetadata, ...]:
        with self._lock:
            return tuple(self._datasets.values())

    def record_ingestion_run(self, run: IngestionRun) -> None:
        with self._lock:
            self._runs[str(run.run_id)] = run

    def ingestion_runs(self) -> tuple[IngestionRun, ...]:
        with self._lock:
            return tuple(self._runs.values())


__all__ = ["InMemoryMarketDataStore", "MarketDataStore", "StoreWriteResult"]
