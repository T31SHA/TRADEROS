-- Phase 10-prep: durable, fail-closed PAPER worker supervision.
-- This adds no provider, broker, live, or order-bypass capability.

CREATE TABLE IF NOT EXISTS worker_leases (
    workload_id VARCHAR(128) PRIMARY KEY,
    owner_id VARCHAR(128) NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_states (
    workload_id VARCHAR(128) PRIMARY KEY,
    worker_state VARCHAR(32) NOT NULL,
    health VARCHAR(32) NOT NULL,
    mode VARCHAR(16) NOT NULL,
    heartbeat_at TIMESTAMPTZ,
    last_cycle_at TIMESTAMPTZ,
    stop_requested BOOLEAN NOT NULL DEFAULT FALSE,
    data_status VARCHAR(32) NOT NULL,
    data_reason TEXT,
    eligible_strategy_count INTEGER NOT NULL DEFAULT 0,
    active_risk_locks JSONB NOT NULL DEFAULT '[]'::jsonb,
    reconciliation_status VARCHAR(32) NOT NULL,
    last_decision VARCHAR(64) NOT NULL,
    last_reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    paper_order_count INTEGER NOT NULL DEFAULT 0,
    paper_fill_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_cycles (
    cycle_id VARCHAR(128) PRIMARY KEY,
    workload_id VARCHAR(128) NOT NULL,
    decision_key VARCHAR(128) NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    cycle_status VARCHAR(32) NOT NULL,
    action VARCHAR(32) NOT NULL,
    reason_codes JSONB NOT NULL,
    replayed BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS ix_worker_cycles_workload_time
    ON worker_cycles(workload_id, completed_at);
