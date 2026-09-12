-- TRADEROS Phase 8 paper-trading authoritative state (PostgreSQL).
-- Apply through the existing migration runner.  There is no live broker table.

CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id VARCHAR(64) PRIMARY KEY, account_currency VARCHAR(16) NOT NULL,
    starting_cash NUMERIC(28,12) NOT NULL CHECK (starting_cash > 0),
    cash NUMERIC(28,12) NOT NULL, reserved_risk NUMERIC(28,12) NOT NULL CHECK (reserved_risk >= 0),
    risk_capacity NUMERIC(28,12) NOT NULL CHECK (risk_capacity > 0),
    daily_loss_limit NUMERIC(28,12) NOT NULL, max_drawdown NUMERIC(28,12) NOT NULL,
    risk_policy_id VARCHAR(64) NOT NULL, risk_policy_configuration_id VARCHAR(128) NOT NULL,
    risk_day VARCHAR(10) NOT NULL, risk_day_starting_equity NUMERIC(28,12) NOT NULL,
    high_water_mark NUMERIC(28,12) NOT NULL, realized_pnl NUMERIC(28,12) NOT NULL,
    fees NUMERIC(28,12) NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_positions (
    account_id VARCHAR(64) NOT NULL REFERENCES paper_accounts(account_id),
    symbol VARCHAR(64) NOT NULL REFERENCES instruments(canonical_symbol), instrument JSONB NOT NULL,
    quantity NUMERIC(28,12) NOT NULL, average_entry_price NUMERIC(28,12) NOT NULL CHECK (average_entry_price >= 0),
    market_price NUMERIC(28,12) NOT NULL CHECK (market_price >= 0), realized_pnl NUMERIC(28,12) NOT NULL,
    unrealized_pnl NUMERIC(28,12) NOT NULL, fees NUMERIC(28,12) NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (account_id, symbol)
);
CREATE TABLE IF NOT EXISTS paper_orders (
    order_id VARCHAR(96) PRIMARY KEY, idempotency_key VARCHAR(128) NOT NULL,
    account_id VARCHAR(64) NOT NULL REFERENCES paper_accounts(account_id),
    symbol VARCHAR(64) NOT NULL REFERENCES instruments(canonical_symbol), instrument JSONB NOT NULL,
    side VARCHAR(8) NOT NULL, order_type VARCHAR(16) NOT NULL, quantity NUMERIC(28,12) NOT NULL CHECK (quantity > 0),
    filled_quantity NUMERIC(28,12) NOT NULL CHECK (filled_quantity >= 0 AND filled_quantity <= quantity),
    average_fill_price NUMERIC(28,12), limit_price NUMERIC(28,12), stop_price NUMERIC(28,12), time_in_force VARCHAR(8) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL, submitted_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ,
    last_market_event_at TIMESTAMPTZ,
    risk_decision_id VARCHAR(128) NOT NULL, risk_policy_id VARCHAR(64) NOT NULL,
    risk_policy_configuration_id VARCHAR(128) NOT NULL, sizing_configuration_id VARCHAR(128) NOT NULL,
    source_intent_id VARCHAR(128) NOT NULL, authorization_action VARCHAR(32) NOT NULL, status VARCHAR(32) NOT NULL,
    rejection_reason TEXT, UNIQUE(account_id, idempotency_key), UNIQUE(account_id, risk_decision_id)
);
CREATE INDEX IF NOT EXISTS ix_paper_orders_open ON paper_orders(account_id, status, submitted_at);
CREATE TABLE IF NOT EXISTS paper_reservations (
    reservation_id VARCHAR(128) PRIMARY KEY, account_id VARCHAR(64) NOT NULL REFERENCES paper_accounts(account_id),
    order_id VARCHAR(96) NOT NULL UNIQUE REFERENCES paper_orders(order_id), intent_id VARCHAR(128) NOT NULL,
    risk_decision_id VARCHAR(128) NOT NULL UNIQUE, reserved_risk NUMERIC(28,12) NOT NULL CHECK (reserved_risk >= 0),
    reserved_cash NUMERIC(28,12) NOT NULL CHECK (reserved_cash >= 0), active BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL, released_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_paper_reservations_active ON paper_reservations(account_id, active);
CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id VARCHAR(128) PRIMARY KEY, order_id VARCHAR(96) NOT NULL REFERENCES paper_orders(order_id),
    account_id VARCHAR(64) NOT NULL REFERENCES paper_accounts(account_id), symbol VARCHAR(64) NOT NULL REFERENCES instruments(canonical_symbol),
    instrument JSONB NOT NULL, side VARCHAR(8) NOT NULL, quantity NUMERIC(28,12) NOT NULL CHECK (quantity > 0),
    price NUMERIC(28,12) NOT NULL CHECK (price > 0), reference_price NUMERIC(28,12) NOT NULL CHECK (reference_price > 0),
    commission NUMERIC(28,12) NOT NULL CHECK (commission >= 0), timestamp TIMESTAMPTZ NOT NULL, UNIQUE(order_id, fill_id)
);
CREATE TABLE IF NOT EXISTS paper_risk_locks (
    lock_id VARCHAR(128) PRIMARY KEY, account_id VARCHAR(64) NOT NULL REFERENCES paper_accounts(account_id),
    lock_type VARCHAR(32) NOT NULL, reason TEXT NOT NULL, active BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL, cleared_at TIMESTAMPTZ, UNIQUE(account_id, lock_type, active)
);
CREATE TABLE IF NOT EXISTS paper_audit_events (
    event_id VARCHAR(128) PRIMARY KEY, account_id VARCHAR(64) NOT NULL REFERENCES paper_accounts(account_id),
    order_id VARCHAR(96) REFERENCES paper_orders(order_id), fill_id VARCHAR(128) REFERENCES paper_fills(fill_id),
    intent_id VARCHAR(128), risk_decision_id VARCHAR(128), event_type VARCHAR(64) NOT NULL,
    previous_state VARCHAR(32), new_state VARCHAR(32), reason TEXT, timestamp TIMESTAMPTZ NOT NULL, metadata JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_paper_audit_account_time ON paper_audit_events(account_id, timestamp);
