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

# Phase 8: these tables are authoritative paper state.  Historical fills and
# audit records are append-only; account/position rows are current projections
# which are verified from the ledger during recovery/reconciliation.
paper_accounts = Table(
    "paper_accounts",
    metadata,
    Column("account_id", String(64), primary_key=True),
    Column("account_currency", String(16), nullable=False),
    Column("starting_cash", Numeric(28, 12), nullable=False),
    Column("cash", Numeric(28, 12), nullable=False),
    Column("reserved_risk", Numeric(28, 12), nullable=False, default=0),
    Column("risk_capacity", Numeric(28, 12), nullable=False),
    Column("daily_loss_limit", Numeric(28, 12), nullable=False),
    Column("max_drawdown", Numeric(28, 12), nullable=False),
    Column("risk_policy_id", String(64), nullable=False),
    Column("risk_policy_configuration_id", String(128), nullable=False),
    Column("risk_day", String(10), nullable=False),
    Column("risk_day_starting_equity", Numeric(28, 12), nullable=False),
    Column("high_water_mark", Numeric(28, 12), nullable=False),
    Column("realized_pnl", Numeric(28, 12), nullable=False, default=0),
    Column("fees", Numeric(28, 12), nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("starting_cash > 0 AND risk_capacity > 0", name="ck_paper_account_capital"),
    CheckConstraint("reserved_risk >= 0", name="ck_paper_account_reserved_risk"),
)

paper_positions = Table(
    "paper_positions",
    metadata,
    Column("account_id", String(64), ForeignKey("paper_accounts.account_id"), primary_key=True),
    Column("symbol", String(64), ForeignKey("instruments.canonical_symbol"), primary_key=True),
    Column("instrument", JSON, nullable=False),
    Column("quantity", Numeric(28, 12), nullable=False),
    Column("average_entry_price", Numeric(28, 12), nullable=False),
    Column("market_price", Numeric(28, 12), nullable=False),
    Column("realized_pnl", Numeric(28, 12), nullable=False, default=0),
    Column("unrealized_pnl", Numeric(28, 12), nullable=False, default=0),
    Column("fees", Numeric(28, 12), nullable=False, default=0),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "average_entry_price >= 0 AND market_price >= 0", name="ck_paper_position_prices"
    ),
)

paper_orders = Table(
    "paper_orders",
    metadata,
    Column("order_id", String(96), primary_key=True),
    Column("idempotency_key", String(128), nullable=False),
    Column("account_id", String(64), ForeignKey("paper_accounts.account_id"), nullable=False),
    Column("symbol", String(64), ForeignKey("instruments.canonical_symbol"), nullable=False),
    Column("instrument", JSON, nullable=False),
    Column("side", String(8), nullable=False),
    Column("order_type", String(16), nullable=False),
    Column("quantity", Numeric(28, 12), nullable=False),
    Column("filled_quantity", Numeric(28, 12), nullable=False, default=0),
    Column("average_fill_price", Numeric(28, 12)),
    Column("limit_price", Numeric(28, 12)),
    Column("stop_price", Numeric(28, 12)),
    Column("time_in_force", String(8), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("submitted_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True)),
    Column("last_market_event_at", DateTime(timezone=True)),
    Column("risk_decision_id", String(128), nullable=False),
    Column("risk_policy_id", String(64), nullable=False),
    Column("risk_policy_configuration_id", String(128), nullable=False),
    Column("sizing_configuration_id", String(128), nullable=False),
    Column("source_intent_id", String(128), nullable=False),
    Column("authorization_action", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("rejection_reason", Text),
    UniqueConstraint("account_id", "idempotency_key", name="uq_paper_order_idempotency"),
    UniqueConstraint("account_id", "risk_decision_id", name="uq_paper_order_risk_decision"),
    CheckConstraint(
        "quantity > 0 AND filled_quantity >= 0 AND filled_quantity <= quantity",
        name="ck_paper_order_quantity",
    ),
)
Index(
    "ix_paper_orders_open",
    paper_orders.c.account_id,
    paper_orders.c.status,
    paper_orders.c.submitted_at,
)

paper_reservations = Table(
    "paper_reservations",
    metadata,
    Column("reservation_id", String(128), primary_key=True),
    Column("account_id", String(64), ForeignKey("paper_accounts.account_id"), nullable=False),
    Column(
        "order_id", String(96), ForeignKey("paper_orders.order_id"), nullable=False, unique=True
    ),
    Column("intent_id", String(128), nullable=False),
    Column("risk_decision_id", String(128), nullable=False, unique=True),
    Column("reserved_risk", Numeric(28, 12), nullable=False),
    Column("reserved_cash", Numeric(28, 12), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("released_at", DateTime(timezone=True)),
    CheckConstraint(
        "reserved_risk >= 0 AND reserved_cash >= 0", name="ck_paper_reservation_nonnegative"
    ),
)
Index("ix_paper_reservations_active", paper_reservations.c.account_id, paper_reservations.c.active)

paper_fills = Table(
    "paper_fills",
    metadata,
    Column("fill_id", String(128), primary_key=True),
    Column("order_id", String(96), ForeignKey("paper_orders.order_id"), nullable=False),
    Column("account_id", String(64), ForeignKey("paper_accounts.account_id"), nullable=False),
    Column("symbol", String(64), ForeignKey("instruments.canonical_symbol"), nullable=False),
    Column("instrument", JSON, nullable=False),
    Column("side", String(8), nullable=False),
    Column("quantity", Numeric(28, 12), nullable=False),
    Column("price", Numeric(28, 12), nullable=False),
    Column("reference_price", Numeric(28, 12), nullable=False),
    Column("commission", Numeric(28, 12), nullable=False),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    UniqueConstraint("order_id", "fill_id", name="uq_paper_fill_order_identity"),
    CheckConstraint(
        "quantity > 0 AND price > 0 AND reference_price > 0 AND commission >= 0",
        name="ck_paper_fill_values",
    ),
)

paper_risk_locks = Table(
    "paper_risk_locks",
    metadata,
    Column("lock_id", String(128), primary_key=True),
    Column("account_id", String(64), ForeignKey("paper_accounts.account_id"), nullable=False),
    Column("lock_type", String(32), nullable=False),
    Column("reason", Text, nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("cleared_at", DateTime(timezone=True)),
    UniqueConstraint("account_id", "lock_type", "active", name="uq_paper_active_lock_type"),
)

paper_audit_events = Table(
    "paper_audit_events",
    metadata,
    Column("event_id", String(128), primary_key=True),
    Column("account_id", String(64), ForeignKey("paper_accounts.account_id"), nullable=False),
    Column("order_id", String(96), ForeignKey("paper_orders.order_id")),
    Column("fill_id", String(128), ForeignKey("paper_fills.fill_id")),
    Column("intent_id", String(128)),
    Column("risk_decision_id", String(128)),
    Column("event_type", String(64), nullable=False),
    Column("previous_state", String(32)),
    Column("new_state", String(32)),
    Column("reason", Text),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("metadata", JSON, nullable=False),
)
Index(
    "ix_paper_audit_account_time", paper_audit_events.c.account_id, paper_audit_events.c.timestamp
)

__all__ = [
    "data_ingestion_runs",
    "data_quality_events",
    "data_sources",
    "dataset_versions",
    "instruments",
    "market_data",
    "metadata",
    "paper_accounts",
    "paper_audit_events",
    "paper_fills",
    "paper_orders",
    "paper_positions",
    "paper_reservations",
    "paper_risk_locks",
]
