-- Execution-boundary hardening: lease generations fence stale operational work
-- and paper orders retain the original trade-level authorization bounds.

ALTER TABLE worker_leases
    ADD COLUMN IF NOT EXISTS fence_generation BIGINT NOT NULL DEFAULT 1;

ALTER TABLE paper_orders
    ADD COLUMN IF NOT EXISTS authorization_max_new_notional NUMERIC(28,12) NOT NULL DEFAULT 0;

ALTER TABLE paper_orders
    ADD COLUMN IF NOT EXISTS authorization_max_reduction_notional NUMERIC(28,12) NOT NULL DEFAULT 0;

ALTER TABLE paper_orders
    ADD COLUMN IF NOT EXISTS authorization_costs_included BOOLEAN NOT NULL DEFAULT TRUE;
