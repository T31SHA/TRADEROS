"""SQLAlchemy market-data storage adapter."""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, select, tuple_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from traderos.data.bars import MarketBar
from traderos.data.errors import StorageError
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy, DatasetMetadata, IngestionRun
from traderos.data.quality import DataQualityEvent, QualityCode, QualitySeverity
from traderos.data.storage import StoreWriteResult
from traderos.data.time import normalize_timestamp
from traderos.data.timeframes import Timeframe
from traderos.database.schema import (
    data_ingestion_runs,
    data_quality_events,
    data_sources,
    dataset_versions,
    instruments,
    market_data,
    metadata,
)


def _instrument_row(instrument: Instrument) -> dict[str, object]:
    return {
        "canonical_symbol": instrument.canonical_symbol,
        "asset_class": instrument.asset_class.value,
        "exchange": instrument.exchange,
        "base_currency": instrument.base_currency,
        "quote_currency": instrument.quote_currency,
        "trading_currency": instrument.trading_currency,
        "provider_symbols": instrument.provider_symbols,
        "timezone": instrument.timezone,
        "tick_size": instrument.tick_size,
        "lot_size": instrument.lot_size,
        "is_active": instrument.is_active,
    }


def _bar_row(bar: MarketBar) -> dict[str, object]:
    return {
        "symbol": bar.symbol,
        "asset_class": bar.asset_class.value,
        "timeframe": bar.timeframe.value,
        "adjustment_policy": bar.adjustment_policy.value,
        "timestamp": bar.timestamp,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "source_id": bar.source,
        "currency": bar.currency,
        "ingestion_timestamp": bar.ingestion_timestamp,
        "bid": bar.bid,
        "ask": bar.ask,
        "quote_timestamp": bar.quote_timestamp,
        "spread": bar.spread,
        "adjusted_close": bar.adjusted_close,
        "trade_count": bar.trade_count,
        "vwap": bar.vwap,
    }


class SqlAlchemyMarketDataStore:
    """PostgreSQL-targeted store with SQLite-compatible test behavior."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_schema(self) -> None:
        try:
            metadata.create_all(self._engine)
        except SQLAlchemyError as exc:
            raise StorageError(str(exc)) from exc

    def upsert_bars(self, bars: Sequence[MarketBar]) -> StoreWriteResult:
        if not bars:
            return StoreWriteResult(0, ())
        rows = [_bar_row(bar) for bar in bars]
        unique_keys = tuple(
            (
                bar.symbol,
                bar.timeframe.value,
                bar.timestamp,
                bar.source,
                bar.adjustment_policy.value,
            )
            for bar in bars
        )
        try:
            with self._engine.begin() as connection:
                source_rows = [
                    {
                        "source_id": source,
                        "provider": source,
                        "created_at": datetime.now(UTC),
                    }
                    for source in sorted({bar.source for bar in bars})
                ]
                instrument_rows = [
                    _instrument_row(instrument)
                    for instrument in {
                        bar.instrument.canonical_symbol: bar.instrument for bar in bars
                    }.values()
                ]
                self._insert_ignore(connection, data_sources, source_rows, ("source_id",))
                self._insert_ignore(
                    connection,
                    instruments,
                    instrument_rows,
                    ("canonical_symbol",),
                )
                existing_statement = select(
                    market_data.c.symbol,
                    market_data.c.timeframe,
                    market_data.c.timestamp,
                    market_data.c.source_id,
                    market_data.c.adjustment_policy,
                ).where(
                    tuple_(
                        market_data.c.symbol,
                        market_data.c.timeframe,
                        market_data.c.timestamp,
                        market_data.c.source_id,
                        market_data.c.adjustment_policy,
                    ).in_(list(dict.fromkeys(unique_keys)))
                )
                existing_keys = {
                    (
                        row[0],
                        row[1],
                        row[2].replace(tzinfo=UTC) if row[2].tzinfo is None else row[2],
                        row[3],
                        row[4],
                    )
                    for row in connection.execute(existing_statement).all()
                }
                seen_keys: set[tuple[str, str, datetime, str, str]] = set()
                duplicate_keys: list[tuple[str, str, datetime, str, str]] = []
                rows_to_insert: list[dict[str, object]] = []
                for row, key in zip(rows, unique_keys, strict=True):
                    if key in seen_keys or key in existing_keys:
                        duplicate_keys.append(key)
                    else:
                        seen_keys.add(key)
                        rows_to_insert.append(row)
                if rows_to_insert:
                    statement = self._insert_ignore_statement(
                        market_data,
                        rows_to_insert,
                        ("symbol", "timeframe", "timestamp", "source_id", "adjustment_policy"),
                    )
                    result = connection.execute(statement)
                    accepted = max(result.rowcount, 0)
                else:
                    accepted = 0
        except SQLAlchemyError as exc:
            raise StorageError(str(exc)) from exc
        return StoreWriteResult(accepted, tuple(duplicate_keys))

    def _insert_ignore_statement(
        self, table: Any, rows: list[dict[str, object]], index_elements: tuple[str, ...]
    ) -> Any:
        dialect = self._engine.dialect.name
        if dialect == "sqlite":
            return (
                sqlite_insert(table)
                .values(rows)
                .on_conflict_do_nothing(index_elements=list(index_elements))
            )
        if dialect == "postgresql":
            return (
                postgresql_insert(table)
                .values(rows)
                .on_conflict_do_nothing(index_elements=list(index_elements))
            )
        raise StorageError(f"Unsupported SQLAlchemy dialect for idempotent writes: {dialect}")

    def _insert_ignore(
        self,
        connection: Any,
        table: Any,
        rows: list[dict[str, object]],
        index_elements: tuple[str, ...],
    ) -> None:
        if rows:
            connection.execute(self._insert_ignore_statement(table, rows, index_elements))

    def query_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        source: str | None = None,
        adjustment_policy: AdjustmentPolicy | None = None,
    ) -> tuple[MarketBar, ...]:
        statement = self._bar_select().where(
            market_data.c.symbol == symbol,
            market_data.c.timeframe == timeframe.value,
            market_data.c.timestamp >= normalize_timestamp(start),
            market_data.c.timestamp < normalize_timestamp(end),
        )
        if source is not None:
            statement = statement.where(market_data.c.source_id == source)
        if adjustment_policy is not None:
            statement = statement.where(market_data.c.adjustment_policy == adjustment_policy.value)
        statement = statement.order_by(market_data.c.timestamp)
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as exc:
            raise StorageError(str(exc)) from exc
        return tuple(self._row_to_bar(row) for row in rows)

    def latest_bar(
        self,
        symbol: str,
        timeframe: Timeframe,
        source: str | None = None,
        adjustment_policy: AdjustmentPolicy | None = None,
    ) -> MarketBar | None:
        statement = self._bar_select().where(
            market_data.c.symbol == symbol,
            market_data.c.timeframe == timeframe.value,
        )
        if source is not None:
            statement = statement.where(market_data.c.source_id == source)
        if adjustment_policy is not None:
            statement = statement.where(market_data.c.adjustment_policy == adjustment_policy.value)
        statement = statement.order_by(market_data.c.timestamp.desc()).limit(1)
        try:
            with self._engine.connect() as connection:
                row = connection.execute(statement).mappings().first()
        except SQLAlchemyError as exc:
            raise StorageError(str(exc)) from exc
        return self._row_to_bar(row) if row else None

    def has_bar(self, bar: MarketBar) -> bool:
        statement = (
            select(market_data.c.id)
            .where(
                market_data.c.symbol == bar.symbol,
                market_data.c.timeframe == bar.timeframe.value,
                market_data.c.timestamp == bar.timestamp,
                market_data.c.source_id == bar.source,
                market_data.c.adjustment_policy == bar.adjustment_policy.value,
            )
            .limit(1)
        )
        try:
            with self._engine.connect() as connection:
                return connection.execute(statement).first() is not None
        except SQLAlchemyError as exc:
            raise StorageError(str(exc)) from exc

    def record_quality_events(self, events: Sequence[DataQualityEvent]) -> None:
        rows = [
            {
                "event_id": str(event.event_id),
                "occurred_at": event.occurred_at,
                "severity": event.severity.value,
                "code": event.code.value,
                "message": event.message,
                "source_id": event.source,
                "symbol": event.symbol,
                "timeframe": event.timeframe,
                "bar_timestamp": event.bar_timestamp,
                "metadata": event.metadata,
            }
            for event in events
        ]
        try:
            with self._engine.begin() as connection:
                if rows:
                    connection.execute(data_quality_events.insert(), rows)
        except SQLAlchemyError as exc:
            raise StorageError(str(exc)) from exc

    def quality_events(self) -> tuple[DataQualityEvent, ...]:
        try:
            with self._engine.connect() as connection:
                rows = (
                    connection.execute(
                        select(data_quality_events).order_by(data_quality_events.c.occurred_at)
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError as exc:
            raise StorageError(str(exc)) from exc
        return tuple(
            DataQualityEvent(
                event_id=row["event_id"],
                occurred_at=row["occurred_at"].replace(tzinfo=UTC)
                if row["occurred_at"].tzinfo is None
                else row["occurred_at"],
                severity=QualitySeverity(row["severity"]),
                code=QualityCode(row["code"]),
                message=row["message"],
                source=row["source_id"],
                symbol=row["symbol"],
                timeframe=row["timeframe"],
                bar_timestamp=(
                    row["bar_timestamp"].replace(tzinfo=UTC)
                    if row["bar_timestamp"] and row["bar_timestamp"].tzinfo is None
                    else row["bar_timestamp"]
                ),
                metadata=row["metadata"],
            )
            for row in rows
        )

    def record_dataset(self, metadata_record: DatasetMetadata) -> None:
        row: dict[str, object] = {
            "dataset_version": metadata_record.dataset_version,
            "dataset_hash": metadata_record.dataset_hash,
            "provider": metadata_record.provider,
            "symbol": metadata_record.symbol,
            "timeframe": metadata_record.timeframe,
            "start": metadata_record.start,
            "end": metadata_record.end,
            "adjustment_policy": metadata_record.adjustment_policy.value,
            "configuration_version": metadata_record.configuration_version,
            "quality_status": metadata_record.quality_status,
            "created_at": metadata_record.created_at,
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    self._insert_ignore_statement(dataset_versions, [row], ("dataset_version",))
                )
        except SQLAlchemyError as exc:
            raise StorageError(str(exc)) from exc

    def datasets(self) -> tuple[DatasetMetadata, ...]:
        try:
            with self._engine.connect() as connection:
                rows = (
                    connection.execute(
                        select(dataset_versions).order_by(dataset_versions.c.created_at)
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError as exc:
            raise StorageError(str(exc)) from exc
        return tuple(
            DatasetMetadata(
                dataset_version=row["dataset_version"],
                dataset_hash=row["dataset_hash"],
                provider=row["provider"],
                symbol=row["symbol"],
                timeframe=row["timeframe"],
                start=row["start"].replace(tzinfo=UTC)
                if row["start"].tzinfo is None
                else row["start"],
                end=row["end"].replace(tzinfo=UTC) if row["end"].tzinfo is None else row["end"],
                adjustment_policy=AdjustmentPolicy(row["adjustment_policy"]),
                configuration_version=row["configuration_version"],
                quality_status=row["quality_status"],
                created_at=row["created_at"].replace(tzinfo=UTC)
                if row["created_at"].tzinfo is None
                else row["created_at"],
            )
            for row in rows
        )

    def record_ingestion_run(self, run: IngestionRun) -> None:
        row = {
            "run_id": str(run.run_id),
            "dataset_version": run.dataset_version,
            "provider": run.provider,
            "symbol": run.symbol,
            "timeframe": run.timeframe,
            "requested_start": run.requested_start,
            "requested_end": run.requested_end,
            "adjustment_policy": run.adjustment_policy.value,
            "configuration_version": run.configuration_version,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "records_received": run.records_received,
            "records_accepted": run.records_accepted,
            "records_rejected": run.records_rejected,
            "duplicates": run.duplicates,
            "warnings": run.warnings,
            "errors": run.errors,
            "status": run.status.value,
            "error_message": run.error_message,
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    data_ingestion_runs.delete().where(
                        data_ingestion_runs.c.run_id == str(run.run_id)
                    )
                )
                connection.execute(data_ingestion_runs.insert(), row)
        except SQLAlchemyError as exc:
            raise StorageError(str(exc)) from exc

    def ingestion_runs(self) -> tuple[IngestionRun, ...]:
        try:
            with self._engine.connect() as connection:
                rows = (
                    connection.execute(
                        select(data_ingestion_runs).order_by(data_ingestion_runs.c.started_at)
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError as exc:
            raise StorageError(str(exc)) from exc
        return tuple(
            IngestionRun(
                run_id=row["run_id"],
                dataset_version=row["dataset_version"],
                provider=row["provider"],
                symbol=row["symbol"],
                timeframe=row["timeframe"],
                requested_start=row["requested_start"].replace(tzinfo=UTC)
                if row["requested_start"].tzinfo is None
                else row["requested_start"],
                requested_end=row["requested_end"].replace(tzinfo=UTC)
                if row["requested_end"].tzinfo is None
                else row["requested_end"],
                adjustment_policy=AdjustmentPolicy(row["adjustment_policy"]),
                configuration_version=row["configuration_version"],
                started_at=row["started_at"].replace(tzinfo=UTC)
                if row["started_at"].tzinfo is None
                else row["started_at"],
                finished_at=(
                    row["finished_at"].replace(tzinfo=UTC)
                    if row["finished_at"] and row["finished_at"].tzinfo is None
                    else row["finished_at"]
                ),
                records_received=row["records_received"],
                records_accepted=row["records_accepted"],
                records_rejected=row["records_rejected"],
                duplicates=row["duplicates"],
                warnings=row["warnings"],
                errors=row["errors"],
                status=row["status"],
                error_message=row["error_message"],
            )
            for row in rows
        )

    @staticmethod
    def _bar_select() -> Any:
        return select(
            market_data,
            instruments.c.provider_symbols.label("instrument_provider_symbols"),
            instruments.c.exchange.label("instrument_exchange"),
            instruments.c.base_currency.label("instrument_base_currency"),
            instruments.c.quote_currency.label("instrument_quote_currency"),
            instruments.c.timezone.label("instrument_timezone"),
            instruments.c.tick_size.label("instrument_tick_size"),
            instruments.c.lot_size.label("instrument_lot_size"),
            instruments.c.is_active.label("instrument_is_active"),
        ).select_from(
            market_data.join(
                instruments,
                market_data.c.symbol == instruments.c.canonical_symbol,
            )
        )

    @staticmethod
    def _row_to_bar(row: Any) -> MarketBar:
        instrument = Instrument(
            canonical_symbol=row["symbol"],
            asset_class=AssetClass(row["asset_class"]),
            provider_symbols=row["instrument_provider_symbols"],
            exchange=row["instrument_exchange"],
            base_currency=row["instrument_base_currency"],
            quote_currency=row["instrument_quote_currency"],
            trading_currency=row["currency"],
            timezone=row["instrument_timezone"],
            tick_size=row["instrument_tick_size"],
            lot_size=row["instrument_lot_size"],
            is_active=row["instrument_is_active"],
        )
        return MarketBar(
            instrument=instrument,
            timeframe=Timeframe(row["timeframe"]),
            adjustment_policy=AdjustmentPolicy(row["adjustment_policy"]),
            timestamp=row["timestamp"].replace(tzinfo=UTC)
            if row["timestamp"].tzinfo is None
            else row["timestamp"],
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            volume=Decimal(row["volume"]) if row["volume"] is not None else None,
            source=row["source_id"],
            currency=row["currency"],
            ingestion_timestamp=row["ingestion_timestamp"].replace(tzinfo=UTC)
            if row["ingestion_timestamp"].tzinfo is None
            else row["ingestion_timestamp"],
            bid=Decimal(row["bid"]) if row["bid"] is not None else None,
            ask=Decimal(row["ask"]) if row["ask"] is not None else None,
            quote_timestamp=(
                None
                if row["quote_timestamp"] is None
                else row["quote_timestamp"].replace(tzinfo=UTC)
                if row["quote_timestamp"].tzinfo is None
                else row["quote_timestamp"]
            ),
            spread=Decimal(row["spread"]) if row["spread"] is not None else None,
            adjusted_close=Decimal(row["adjusted_close"])
            if row["adjusted_close"] is not None
            else None,
            trade_count=row["trade_count"],
            vwap=Decimal(row["vwap"]) if row["vwap"] is not None else None,
        )


__all__ = ["SqlAlchemyMarketDataStore"]
