"""SQLAlchemy schema for PostgreSQL market-data storage.

The tables are also compatible with SQLite for deterministic local integration
tests. PostgreSQL remains the production database target.
"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

instruments = Table(
    "instruments",
    metadata,
    Column("canonical_symbol", String(64), primary_key=True),
    Column("asset_class", String(16), nullable=False),
    Column("exchange", String(64)),
    Column("base_currency", String(16)),
    Column("quote_currency", String(16)),
    Column("trading_currency", String(16)),
    Column("provider_symbols", JSON, nullable=False),
    Column("timezone", String(64), nullable=False),
    Column("tick_size", Numeric(28, 12)),
    Column("lot_size", Numeric(28, 12)),
    Column("is_active", Boolean, nullable=False, default=True),
)

data_sources = Table(
    "data_sources",
    metadata,
    Column("source_id", String(64), primary_key=True),
    Column("provider", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("provider", name="uq_data_sources_provider"),
)

dataset_versions = Table(
    "dataset_versions",
    metadata,
    Column("dataset_version", String(64), primary_key=True),
    Column("provider", String(64), nullable=False),
    Column("symbol", String(64), ForeignKey("instruments.canonical_symbol"), nullable=False),
    Column("timeframe", String(8), nullable=False),
    Column("start", DateTime(timezone=True), nullable=False),
    Column("end", DateTime(timezone=True), nullable=False),
    Column("adjustment_policy", String(16), nullable=False),
    Column("configuration_version", String(64), nullable=False),
    Column("quality_status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

market_data = Table(
    "market_data",
    metadata,
    Column(
        "id",
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    Column("symbol", String(64), ForeignKey("instruments.canonical_symbol"), nullable=False),
    Column("asset_class", String(16), nullable=False),
    Column("timeframe", String(8), nullable=False),
    Column("adjustment_policy", String(16), nullable=False),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("open", Numeric(28, 12), nullable=False),
    Column("high", Numeric(28, 12), nullable=False),
    Column("low", Numeric(28, 12), nullable=False),
    Column("close", Numeric(28, 12), nullable=False),
    Column("volume", Numeric(28, 8)),
    Column("source_id", String(64), ForeignKey("data_sources.source_id"), nullable=False),
    Column("currency", String(16)),
    Column("ingestion_timestamp", DateTime(timezone=True), nullable=False),
    Column("bid", Numeric(28, 12)),
    Column("ask", Numeric(28, 12)),
    Column("spread", Numeric(28, 12)),
    Column("adjusted_close", Numeric(28, 12)),
    Column("trade_count", Integer),
    Column("vwap", Numeric(28, 12)),
    UniqueConstraint(
        "symbol",
        "timeframe",
        "timestamp",
        "source_id",
        "adjustment_policy",
        name="uq_market_data_logical_bar",
    ),
    CheckConstraint(
        "open > 0 AND high > 0 AND low > 0 AND close > 0", name="ck_market_data_positive_prices"
    ),
    CheckConstraint(
        "high >= open AND high >= close AND high >= low", name="ck_market_data_high_bound"
    ),
    CheckConstraint(
        "low <= open AND low <= close AND low <= high", name="ck_market_data_low_bound"
    ),
    CheckConstraint("volume IS NULL OR volume >= 0", name="ck_market_data_nonnegative_volume"),
    CheckConstraint("bid IS NULL OR bid > 0", name="ck_market_data_positive_bid"),
    CheckConstraint("ask IS NULL OR ask > 0", name="ck_market_data_positive_ask"),
    CheckConstraint(
        "trade_count IS NULL OR trade_count >= 0", name="ck_market_data_nonnegative_trade_count"
    ),
    CheckConstraint("spread IS NULL OR spread >= 0", name="ck_market_data_nonnegative_spread"),
    CheckConstraint(
        "adjusted_close IS NULL OR adjusted_close > 0",
        name="ck_market_data_positive_adjusted_close",
    ),
    CheckConstraint("vwap IS NULL OR vwap > 0", name="ck_market_data_positive_vwap"),
)
Index(
    "ix_market_data_series_time",
    market_data.c.symbol,
    market_data.c.timeframe,
    market_data.c.timestamp,
)

data_ingestion_runs = Table(
    "data_ingestion_runs",
    metadata,
    Column("run_id", String(36), primary_key=True),
    Column("dataset_version", String(64), nullable=False),
    Column("provider", String(64), nullable=False),
    Column("symbol", String(64), nullable=False),
    Column("timeframe", String(8), nullable=False),
    Column("requested_start", DateTime(timezone=True), nullable=False),
    Column("requested_end", DateTime(timezone=True), nullable=False),
    Column("adjustment_policy", String(16), nullable=False),
    Column("configuration_version", String(64), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("records_received", Integer, nullable=False),
    Column("records_accepted", Integer, nullable=False),
    Column("records_rejected", Integer, nullable=False),
    Column("duplicates", Integer, nullable=False),
    Column("warnings", Integer, nullable=False),
    Column("errors", Integer, nullable=False),
    Column("status", String(16), nullable=False),
    Column("error_message", Text),
)

data_quality_events = Table(
    "data_quality_events",
    metadata,
    Column("event_id", String(36), primary_key=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("code", String(64), nullable=False),
    Column("message", Text, nullable=False),
    Column("source_id", String(64)),
    Column("symbol", String(64)),
    Column("timeframe", String(8)),
    Column("bar_timestamp", DateTime(timezone=True)),
    Column("metadata", JSON, nullable=False),
)
Index(
    "ix_data_quality_symbol_time",
    data_quality_events.c.symbol,
    data_quality_events.c.timeframe,
    data_quality_events.c.bar_timestamp,
)

__all__ = [
    "data_ingestion_runs",
    "data_quality_events",
    "data_sources",
    "dataset_versions",
    "instruments",
    "market_data",
    "metadata",
]
