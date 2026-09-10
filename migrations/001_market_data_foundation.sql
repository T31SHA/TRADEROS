-- TRADEROS Phase 1 market-data foundation.
-- PostgreSQL target. Apply through the project's migration runner; do not use
-- ad-hoc manual schema setup in deployments.

CREATE TABLE IF NOT EXISTS instruments (
    canonical_symbol VARCHAR(64) PRIMARY KEY,
    asset_class VARCHAR(16) NOT NULL,
    exchange VARCHAR(64),
    base_currency VARCHAR(16),
    quote_currency VARCHAR(16),
    trading_currency VARCHAR(16),
    provider_symbols JSONB NOT NULL,
    timezone VARCHAR(64) NOT NULL,
    tick_size NUMERIC(28, 12),
    lot_size NUMERIC(28, 12),
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS data_sources (
    source_id VARCHAR(64) PRIMARY KEY,
    provider VARCHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_versions (
    dataset_version VARCHAR(64) PRIMARY KEY,
    provider VARCHAR(64) NOT NULL,
    symbol VARCHAR(64) NOT NULL REFERENCES instruments(canonical_symbol),
    timeframe VARCHAR(8) NOT NULL,
    start TIMESTAMPTZ NOT NULL,
    "end" TIMESTAMPTZ NOT NULL,
    adjustment_policy VARCHAR(16) NOT NULL,
    configuration_version VARCHAR(64) NOT NULL,
    quality_status VARCHAR(16) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS market_data (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(64) NOT NULL REFERENCES instruments(canonical_symbol),
    asset_class VARCHAR(16) NOT NULL,
    timeframe VARCHAR(8) NOT NULL,
    adjustment_policy VARCHAR(16) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    open NUMERIC(28, 12) NOT NULL,
    high NUMERIC(28, 12) NOT NULL,
    low NUMERIC(28, 12) NOT NULL,
    close NUMERIC(28, 12) NOT NULL,
    volume NUMERIC(28, 8),
    source_id VARCHAR(64) NOT NULL REFERENCES data_sources(source_id),
    currency VARCHAR(16),
    ingestion_timestamp TIMESTAMPTZ NOT NULL,
    bid NUMERIC(28, 12),
    ask NUMERIC(28, 12),
    spread NUMERIC(28, 12),
    adjusted_close NUMERIC(28, 12),
    trade_count INTEGER,
    vwap NUMERIC(28, 12),
    CONSTRAINT uq_market_data_logical_bar UNIQUE (symbol, timeframe, timestamp, source_id, adjustment_policy),
    CONSTRAINT ck_market_data_positive_prices CHECK (open > 0 AND high > 0 AND low > 0 AND close > 0),
    CONSTRAINT ck_market_data_high_bound CHECK (high >= open AND high >= close AND high >= low),
    CONSTRAINT ck_market_data_low_bound CHECK (low <= open AND low <= close AND low <= high),
    CONSTRAINT ck_market_data_nonnegative_volume CHECK (volume IS NULL OR volume >= 0),
    CONSTRAINT ck_market_data_positive_bid CHECK (bid IS NULL OR bid > 0),
    CONSTRAINT ck_market_data_positive_ask CHECK (ask IS NULL OR ask > 0),
    CONSTRAINT ck_market_data_nonnegative_trade_count CHECK (trade_count IS NULL OR trade_count >= 0),
    CONSTRAINT ck_market_data_nonnegative_spread CHECK (spread IS NULL OR spread >= 0),
    CONSTRAINT ck_market_data_positive_adjusted_close CHECK (adjusted_close IS NULL OR adjusted_close > 0),
    CONSTRAINT ck_market_data_positive_vwap CHECK (vwap IS NULL OR vwap > 0)
);

CREATE INDEX IF NOT EXISTS ix_market_data_series_time
    ON market_data (symbol, timeframe, timestamp);

CREATE TABLE IF NOT EXISTS data_ingestion_runs (
    run_id VARCHAR(36) PRIMARY KEY,
    dataset_version VARCHAR(64) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    timeframe VARCHAR(8) NOT NULL,
    requested_start TIMESTAMPTZ NOT NULL,
    requested_end TIMESTAMPTZ NOT NULL,
    adjustment_policy VARCHAR(16) NOT NULL,
    configuration_version VARCHAR(64) NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    records_received INTEGER NOT NULL,
    records_accepted INTEGER NOT NULL,
    records_rejected INTEGER NOT NULL,
    duplicates INTEGER NOT NULL,
    warnings INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS data_quality_events (
    event_id VARCHAR(36) PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL,
    severity VARCHAR(16) NOT NULL,
    code VARCHAR(64) NOT NULL,
    message TEXT NOT NULL,
    source_id VARCHAR(64),
    symbol VARCHAR(64),
    timeframe VARCHAR(8),
    bar_timestamp TIMESTAMPTZ,
    metadata JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_data_quality_symbol_time
    ON data_quality_events (symbol, timeframe, bar_timestamp);
