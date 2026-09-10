"""Bounded, idempotent historical-data ingestion orchestration."""

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from traderos.core.errors import TraderosError
from traderos.data.bars import BarCandidate, MarketBar
from traderos.data.calendars import MarketCalendar
from traderos.data.data_policy import DEFAULT_CONFIGURATION_VERSION
from traderos.data.errors import IngestionError, TransientProviderError
from traderos.data.instruments import Instrument
from traderos.data.lineage import (
    AdjustmentPolicy,
    DatasetMetadata,
    IngestionRun,
    IngestionStatus,
    dataset_version,
)
from traderos.data.providers.base import HistoricalBarsRequest, MarketDataProvider
from traderos.data.quality import DataQualityEngine, DataQualityEvent, QualityCode, QualitySeverity
from traderos.data.storage import MarketDataStore
from traderos.data.time import normalize_timestamp, utc_now
from traderos.data.timeframes import Timeframe
from traderos.data.validation import normalize_and_validate_candidate


class IngestionRequest(BaseModel):
    """Reproducible, bounded ingestion request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument: Instrument
    timeframe: Timeframe
    start: datetime
    end: datetime
    adjustment_policy: AdjustmentPolicy = AdjustmentPolicy.RAW
    configuration_version: str = Field(default=DEFAULT_CONFIGURATION_VERSION, min_length=1)

    @model_validator(mode="after")
    def validate_request(self) -> "IngestionRequest":
        from traderos.data.time import require_utc
        from traderos.data.timeframes import require_supported_timeframe

        require_supported_timeframe(self.instrument.asset_class, self.timeframe)
        require_utc(self.start)
        require_utc(self.end)
        if self.start >= self.end:
            raise ValueError("request start must be before request end")
        return self

    @property
    def typed_instrument(self) -> Instrument:
        return self.instrument


class IngestionConfig(BaseModel):
    """Bounded retry and normalization behavior."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_retries: int = Field(default=3, ge=0, le=10)
    retry_backoff_seconds: float = Field(default=0.0, ge=0.0, le=300.0)


def _bar_fingerprint(bars: Sequence[MarketBar]) -> str:
    payload = [
        {
            "symbol": bar.symbol,
            "timeframe": bar.timeframe.value,
            "timestamp": bar.timestamp.isoformat(),
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": str(bar.volume) if bar.volume is not None else None,
            "source": bar.source,
            "adjustment_policy": bar.adjustment_policy.value,
            "adjusted_close": str(bar.adjusted_close) if bar.adjusted_close is not None else None,
        }
        for bar in sorted(bars, key=lambda item: item.logical_key)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class IngestionService:
    """Fetch, normalize, validate, quality-check, and persist one bounded request."""

    def __init__(
        self,
        provider: MarketDataProvider,
        store: MarketDataStore,
        calendar: MarketCalendar,
        *,
        config: IngestionConfig | None = None,
        quality_engine: DataQualityEngine | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._provider = provider
        self._store = store
        self._calendar = calendar
        self._config = config or IngestionConfig()
        self._quality_engine = quality_engine or DataQualityEngine()
        self._clock = clock
        self._sleeper = sleeper

    def _fetch(self, request: HistoricalBarsRequest) -> tuple[BarCandidate, ...]:
        attempts = self._config.max_retries + 1
        for attempt in range(attempts):
            try:
                return self._provider.get_historical_bars(request)
            except TransientProviderError:
                if attempt == attempts - 1:
                    raise
                delay = self._config.retry_backoff_seconds * (2**attempt)
                if delay:
                    self._sleeper(delay)
        raise AssertionError("bounded retry loop must return or raise")

    def ingest(self, request: IngestionRequest) -> IngestionRun:
        """Run ingestion and raise ``IngestionError`` only after recording failure."""

        started_at = normalize_timestamp(self._clock())
        typed_instrument = request.typed_instrument
        initial_version = dataset_version(
            provider=self._provider.provider_id,
            symbol=typed_instrument.canonical_symbol,
            timeframe=request.timeframe.value,
            start=request.start,
            end=request.end,
            adjustment_policy=request.adjustment_policy,
            configuration_version=request.configuration_version,
            bar_fingerprint="pending",
        )
        run = IngestionRun(
            dataset_version=initial_version,
            provider=self._provider.provider_id,
            symbol=typed_instrument.canonical_symbol,
            timeframe=request.timeframe.value,
            requested_start=request.start,
            requested_end=request.end,
            adjustment_policy=request.adjustment_policy,
            configuration_version=request.configuration_version,
            started_at=started_at,
        )
        self._store.record_ingestion_run(run)

        try:
            candidates = self._fetch(
                HistoricalBarsRequest(
                    instrument=typed_instrument,
                    timeframe=request.timeframe,
                    start=request.start,
                    end=request.end,
                )
            )
            events: list[DataQualityEvent] = []
            valid_bars: list[MarketBar] = []
            seen_keys: set[tuple[str, str, datetime, str, str]] = set()
            rejected = 0
            if not candidates:
                events.append(
                    DataQualityEvent(
                        occurred_at=started_at,
                        severity=QualitySeverity.ERROR,
                        code=QualityCode.PROVIDER_INCONSISTENCY,
                        message="Provider returned no bars for a non-empty request",
                        source=self._provider.provider_id,
                        symbol=typed_instrument.canonical_symbol,
                        timeframe=request.timeframe.value,
                    )
                )
            for candidate in candidates:
                result = normalize_and_validate_candidate(
                    candidate,
                    expected_symbol=typed_instrument.canonical_symbol,
                    expected_timeframe=request.timeframe,
                    adjustment_policy=request.adjustment_policy,
                    source=self._provider.provider_id,
                    ingestion_timestamp=started_at,
                    occurred_at=started_at,
                )
                events.extend(result.events)
                if result.bar is None:
                    rejected += 1
                    continue
                if not request.start <= result.bar.timestamp < request.end:
                    events.append(
                        DataQualityEvent(
                            occurred_at=started_at,
                            severity=QualitySeverity.ERROR,
                            code=QualityCode.PROVIDER_INCONSISTENCY,
                            message="Provider returned a bar outside the requested range",
                            source=result.bar.source,
                            symbol=result.bar.symbol,
                            timeframe=result.bar.timeframe.value,
                            bar_timestamp=result.bar.timestamp,
                        )
                    )
                    rejected += 1
                    continue
                key = (
                    result.bar.symbol,
                    result.bar.timeframe.value,
                    result.bar.timestamp,
                    result.bar.source,
                    result.bar.adjustment_policy.value,
                )
                if key in seen_keys:
                    events.append(
                        DataQualityEvent(
                            occurred_at=started_at,
                            severity=QualitySeverity.WARNING,
                            code=QualityCode.DUPLICATE_BAR,
                            message="Duplicate bar returned by provider in one response",
                            source=result.bar.source,
                            symbol=result.bar.symbol,
                            timeframe=result.bar.timeframe.value,
                            bar_timestamp=result.bar.timestamp,
                        )
                    )
                    continue
                seen_keys.add(key)
                valid_bars.append(result.bar)

            write_result = self._store.upsert_bars(valid_bars)
            for duplicate_key in write_result.duplicate_keys:
                events.append(
                    DataQualityEvent(
                        occurred_at=started_at,
                        severity=QualitySeverity.INFO,
                        code=QualityCode.DUPLICATE_BAR,
                        message="Bar already existed; idempotent write skipped it",
                        source=duplicate_key[3],
                        symbol=duplicate_key[0],
                        timeframe=duplicate_key[1],
                        bar_timestamp=duplicate_key[2],
                    )
                )

            persisted_bars = self._store.query_bars(
                typed_instrument.canonical_symbol,
                request.timeframe,
                request.start,
                request.end,
                self._provider.provider_id,
                request.adjustment_policy,
            )
            quality_report = self._quality_engine.inspect(
                persisted_bars,
                calendar=self._calendar,
                start=request.start,
                end=request.end,
                occurred_at=started_at,
            )
            events.extend(quality_report.events)
            self._store.record_quality_events(events)

            final_version = dataset_version(
                provider=self._provider.provider_id,
                symbol=typed_instrument.canonical_symbol,
                timeframe=request.timeframe.value,
                start=request.start,
                end=request.end,
                adjustment_policy=request.adjustment_policy,
                configuration_version=request.configuration_version,
                bar_fingerprint=_bar_fingerprint(persisted_bars),
            )
            quality_status = (
                "error"
                if any(event.severity is QualitySeverity.ERROR for event in events)
                else "warning"
                if events
                else "clean"
            )
            finished_at = normalize_timestamp(self._clock())
            status = (
                IngestionStatus.PARTIAL
                if rejected or write_result.duplicates or quality_status != "clean"
                else IngestionStatus.SUCCESS
            )
            completed_data = run.model_dump()
            completed_data.update(
                dataset_version=final_version,
                finished_at=finished_at,
                records_received=len(candidates),
                records_accepted=write_result.accepted,
                records_rejected=rejected,
                duplicates=write_result.duplicates
                + sum(
                    event.code is QualityCode.DUPLICATE_BAR
                    and event.message.startswith("Duplicate bar returned")
                    for event in events
                ),
                warnings=sum(event.severity is QualitySeverity.WARNING for event in events),
                errors=sum(event.severity is QualitySeverity.ERROR for event in events),
                status=status,
            )
            completed = IngestionRun(**completed_data)
            self._store.record_dataset(
                DatasetMetadata(
                    dataset_version=final_version,
                    provider=self._provider.provider_id,
                    symbol=typed_instrument.canonical_symbol,
                    timeframe=request.timeframe.value,
                    start=request.start,
                    end=request.end,
                    adjustment_policy=request.adjustment_policy,
                    configuration_version=request.configuration_version,
                    quality_status=quality_status,
                    created_at=finished_at,
                )
            )
            self._store.record_ingestion_run(completed)
            return completed
        except Exception as exc:
            failed_data = run.model_dump()
            failed_data.update(
                finished_at=normalize_timestamp(self._clock()),
                status=IngestionStatus.FAILED,
                errors=1,
                error_message=str(exc),
            )
            failed = IngestionRun(**failed_data)
            self._store.record_ingestion_run(failed)
            if isinstance(exc, (IngestionError, TraderosError)):
                raise
            raise IngestionError(str(exc)) from exc


__all__ = ["IngestionConfig", "IngestionRequest", "IngestionService"]
