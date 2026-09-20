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
    Column("dataset_hash", String(128)),
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
    Column("quote_timestamp", DateTime(timezone=True)),
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
    Column("state_revision", BigInteger, nullable=False, default=0),
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
    CheckConstraint(
        "side IN ('buy', 'sell') AND order_type IN ('market', 'limit', 'stop') "
        "AND time_in_force IN ('gtc', 'day', 'ioc')",
        name="ck_paper_order_values",
    ),
    CheckConstraint(
        "status IN ('created', 'submitted', 'accepted', 'partially_filled', 'filled', "
        "'cancel_requested', 'cancelled', 'rejected', 'expired')",
        name="ck_paper_order_status",
    ),
    CheckConstraint(
        "(order_type = 'limit' AND limit_price > 0 AND stop_price IS NULL) OR "
        "(order_type = 'stop' AND stop_price > 0 AND limit_price IS NULL) OR "
        "(order_type = 'market' AND limit_price IS NULL AND stop_price IS NULL)",
        name="ck_paper_order_prices",
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
    CheckConstraint(
        "lock_type IN ('daily_loss_lock', 'drawdown_lock', 'emergency_lock', 'system_health_lock')",
        name="ck_paper_risk_lock_type",
    ),
)

paper_risk_snapshots = Table(
    "paper_risk_snapshots",
    metadata,
    Column("snapshot_id", String(128), primary_key=True),
    Column("account_id", String(64), ForeignKey("paper_accounts.account_id"), nullable=False),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("cash", Numeric(28, 12), nullable=False),
    Column("equity", Numeric(28, 12), nullable=False),
    Column("used_margin", Numeric(28, 12), nullable=False),
    Column("available_margin", Numeric(28, 12), nullable=False),
    Column("gross_exposure", Numeric(28, 12), nullable=False),
    Column("net_exposure", Numeric(28, 12), nullable=False),
    Column("long_exposure", Numeric(28, 12), nullable=False),
    Column("short_exposure", Numeric(28, 12), nullable=False),
    Column("realized_pnl", Numeric(28, 12), nullable=False),
    Column("unrealized_pnl", Numeric(28, 12), nullable=False),
    Column("fees", Numeric(28, 12), nullable=False),
    Column("daily_pnl", Numeric(28, 12), nullable=False),
    Column("high_water_mark", Numeric(28, 12), nullable=False),
    Column("drawdown", Numeric(28, 12), nullable=False),
    Column("reserved_risk", Numeric(28, 12), nullable=False),
    Column("pending_order_count", Integer, nullable=False),
    Column("active_locks", JSON, nullable=False),
    Column("mark_timestamp", DateTime(timezone=True)),
    Column("positions", JSON, nullable=False),
    CheckConstraint(
        "used_margin >= 0 AND available_margin >= 0 AND gross_exposure >= 0 "
        "AND long_exposure >= 0 AND short_exposure >= 0 AND fees >= 0 "
        "AND drawdown >= 0 AND reserved_risk >= 0 AND pending_order_count >= 0",
        name="ck_paper_risk_snapshot_nonnegative",
    ),
)
Index(
    "ix_paper_risk_snapshots_account_time",
    paper_risk_snapshots.c.account_id,
    paper_risk_snapshots.c.timestamp,
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

# Milestone 2: strategy artifacts and governed lifecycle.  Artifact, evidence,
# approval, revocation, and event rows are immutable records.  The state row is
# only a current projection; its revision is advanced in the same transaction
# as its corresponding lifecycle event.
strategy_artifacts = Table(
    "strategy_artifacts",
    metadata,
    Column("artifact_hash", String(128), primary_key=True),
    Column("strategy_id", String(128), nullable=False),
    Column("strategy_version", String(128), nullable=False),
    Column("schema_version", String(64), nullable=False),
    Column("author", String(128), nullable=False),
    Column("canonical_payload", JSON, nullable=False),
    Column("registered_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("strategy_id", "strategy_version", name="uq_strategy_artifacts_identity"),
)

strategy_evidence = Table(
    "strategy_evidence",
    metadata,
    Column("evidence_id", String(128), primary_key=True),
    Column("evidence_hash", String(128), nullable=False, unique=True),
    Column(
        "artifact_hash", String(128), ForeignKey("strategy_artifacts.artifact_hash"), nullable=False
    ),
    Column("experiment_id", String(128), nullable=False),
    Column("result_hash", String(128), nullable=False),
    Column("canonical_payload", JSON, nullable=False),
    Column("attached_at", DateTime(timezone=True), nullable=False),
)

strategy_approvals = Table(
    "strategy_approvals",
    metadata,
    Column("approval_id", String(128), primary_key=True),
    Column("approval_hash", String(128), nullable=False, unique=True),
    Column(
        "artifact_hash", String(128), ForeignKey("strategy_artifacts.artifact_hash"), nullable=False
    ),
    Column("evidence_id", String(128), ForeignKey("strategy_evidence.evidence_id"), nullable=False),
    Column("target_stage", String(32), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("policy_id", String(128), nullable=False),
    Column("policy_version", String(64), nullable=False),
    Column("canonical_payload", JSON, nullable=False),
    Column("approved_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "target_stage IN ('shadow', 'paper')", name="ck_strategy_approval_target_stage"
    ),
)

strategy_evidence_revocations = Table(
    "strategy_evidence_revocations",
    metadata,
    Column("revocation_id", String(128), primary_key=True),
    Column("evidence_id", String(128), ForeignKey("strategy_evidence.evidence_id"), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("reason", Text, nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=False),
    Column("idempotency_key", String(128), nullable=False, unique=True),
)

strategy_approval_revocations = Table(
    "strategy_approval_revocations",
    metadata,
    Column("revocation_id", String(128), primary_key=True),
    Column(
        "approval_id", String(128), ForeignKey("strategy_approvals.approval_id"), nullable=False
    ),
    Column("actor_id", String(128), nullable=False),
    Column("reason", Text, nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=False),
    Column("idempotency_key", String(128), nullable=False, unique=True),
)

strategy_lifecycle_state = Table(
    "strategy_lifecycle_state",
    metadata,
    Column("strategy_id", String(128), primary_key=True),
    Column("strategy_version", String(128), primary_key=True),
    Column(
        "artifact_hash", String(128), ForeignKey("strategy_artifacts.artifact_hash"), nullable=False
    ),
    Column("stage", String(32), nullable=False),
    Column("health", String(32), nullable=False),
    Column("revision", BigInteger, nullable=False, default=0),
    Column("disabled", Boolean, nullable=False, default=False),
    Column("disable_reason", Text),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("last_event_id", String(128)),
    CheckConstraint(
        "stage IN ('candidate', 'research', 'validation', 'shadow', 'paper', 'canary', 'active', "
        "'retired')",
        name="ck_strategy_state_stage",
    ),
    CheckConstraint(
        "health IN ('unknown', 'healthy', 'weakening', 'degraded', 'failed')",
        name="ck_strategy_state_health",
    ),
    CheckConstraint("revision >= 0", name="ck_strategy_state_revision"),
)

strategy_lifecycle_events = Table(
    "strategy_lifecycle_events",
    metadata,
    Column("event_id", String(128), primary_key=True),
    Column("strategy_id", String(128), nullable=False),
    Column("strategy_version", String(128), nullable=False),
    Column(
        "artifact_hash", String(128), ForeignKey("strategy_artifacts.artifact_hash"), nullable=False
    ),
    Column("event_type", String(32), nullable=False),
    Column("previous_stage", String(32), nullable=False),
    Column("resulting_stage", String(32), nullable=False),
    Column("expected_revision", BigInteger, nullable=False),
    Column("resulting_revision", BigInteger, nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("policy_id", String(128), nullable=False),
    Column("policy_version", String(64), nullable=False),
    Column("evidence_id", String(128), ForeignKey("strategy_evidence.evidence_id")),
    Column("approval_id", String(128), ForeignKey("strategy_approvals.approval_id")),
    Column("reason_code", String(64), nullable=False),
    Column("reason", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("idempotency_key", String(128), nullable=False),
    Column("correlation_id", String(128), nullable=False),
    CheckConstraint("event_type IN ('transition', 'disable')", name="ck_strategy_event_type"),
    CheckConstraint(
        "expected_revision >= 0 AND resulting_revision = expected_revision + 1",
        name="ck_strategy_event_revision",
    ),
    UniqueConstraint(
        "strategy_id",
        "strategy_version",
        "idempotency_key",
        name="uq_strategy_lifecycle_idempotency",
    ),
    UniqueConstraint(
        "strategy_id",
        "strategy_version",
        "resulting_revision",
        name="uq_strategy_lifecycle_revision",
    ),
)
Index(
    "ix_strategy_lifecycle_events_identity",
    strategy_lifecycle_events.c.strategy_id,
    strategy_lifecycle_events.c.strategy_version,
    strategy_lifecycle_events.c.resulting_revision,
)
Index(
    "ix_strategy_lifecycle_events_artifact",
    strategy_lifecycle_events.c.artifact_hash,
    strategy_lifecycle_events.c.occurred_at,
)

# The worker tables are intentionally small operational projections.  Cycle
# rows are append-only and de-duplicated by the deterministic decision key;
# the state row is the latest visible status and the lease row is the single
# workload coordination point.
worker_leases = Table(
    "worker_leases",
    metadata,
    Column("workload_id", String(128), primary_key=True),
    Column("claimed_workload_id", String(128), nullable=False),
    Column("owner_id", String(128), nullable=False),
    Column("acquired_at", DateTime(timezone=True), nullable=False),
    Column("heartbeat_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

worker_states = Table(
    "worker_states",
    metadata,
    Column("workload_id", String(128), primary_key=True),
    Column("worker_state", String(32), nullable=False),
    Column("health", String(32), nullable=False),
    Column("mode", String(16), nullable=False),
    Column("heartbeat_at", DateTime(timezone=True)),
    Column("last_cycle_at", DateTime(timezone=True)),
    Column("stop_requested", Boolean, nullable=False, default=False),
    Column("data_status", String(32), nullable=False),
    Column("data_reason", Text),
    Column("data_latest_at", DateTime(timezone=True)),
    Column("eligible_strategy_count", Integer, nullable=False, default=0),
    Column("active_risk_locks", JSON, nullable=False, default=list),
    Column("reconciliation_status", String(32), nullable=False),
    Column("last_decision", String(64), nullable=False),
    Column("last_reason_codes", JSON, nullable=False, default=list),
    Column("paper_order_count", Integer, nullable=False, default=0),
    Column("paper_fill_count", Integer, nullable=False, default=0),
    Column("last_error", Text),
    Column("last_trace", JSON, nullable=False, default=list),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

worker_cycles = Table(
    "worker_cycles",
    metadata,
    Column("cycle_id", String(128), primary_key=True),
    Column("workload_id", String(128), nullable=False),
    Column("decision_key", String(128), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=False),
    Column("cycle_status", String(32), nullable=False),
    Column("action", String(32), nullable=False),
    Column("reason_codes", JSON, nullable=False),
    Column("decision_trace", JSON, nullable=False),
    Column("replayed", Boolean, nullable=False, default=False),
)
Index("ix_worker_cycles_workload_time", worker_cycles.c.workload_id, worker_cycles.c.completed_at)

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
    "paper_risk_snapshots",
    "strategy_approval_revocations",
    "strategy_approvals",
    "strategy_artifacts",
    "strategy_evidence",
    "strategy_evidence_revocations",
    "strategy_lifecycle_events",
    "strategy_lifecycle_state",
    "worker_cycles",
    "worker_leases",
    "worker_states",
]
