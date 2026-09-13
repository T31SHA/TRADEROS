-- Phase 8 hardening: durable, versioned risk snapshots for the next firewall
-- evaluation.  Earlier migrations remain immutable.

CREATE TABLE IF NOT EXISTS paper_risk_snapshots (
    snapshot_id VARCHAR(128) PRIMARY KEY,
    account_id VARCHAR(64) NOT NULL REFERENCES paper_accounts(account_id),
    timestamp TIMESTAMPTZ NOT NULL,
    cash NUMERIC(28,12) NOT NULL,
    equity NUMERIC(28,12) NOT NULL,
    used_margin NUMERIC(28,12) NOT NULL CHECK (used_margin >= 0),
    available_margin NUMERIC(28,12) NOT NULL CHECK (available_margin >= 0),
    gross_exposure NUMERIC(28,12) NOT NULL CHECK (gross_exposure >= 0),
    net_exposure NUMERIC(28,12) NOT NULL,
    long_exposure NUMERIC(28,12) NOT NULL CHECK (long_exposure >= 0),
    short_exposure NUMERIC(28,12) NOT NULL CHECK (short_exposure >= 0),
    realized_pnl NUMERIC(28,12) NOT NULL,
    unrealized_pnl NUMERIC(28,12) NOT NULL,
    fees NUMERIC(28,12) NOT NULL CHECK (fees >= 0),
    daily_pnl NUMERIC(28,12) NOT NULL,
    high_water_mark NUMERIC(28,12) NOT NULL,
    drawdown NUMERIC(28,12) NOT NULL CHECK (drawdown >= 0),
    reserved_risk NUMERIC(28,12) NOT NULL CHECK (reserved_risk >= 0),
    pending_order_count INTEGER NOT NULL CHECK (pending_order_count >= 0),
    active_locks JSONB NOT NULL,
    mark_timestamp TIMESTAMPTZ,
    positions JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_paper_risk_snapshots_account_time
    ON paper_risk_snapshots(account_id, timestamp);
