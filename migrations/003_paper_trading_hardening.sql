-- Phase 8 hardening: preserve 002 as an immutable migration and add durable
-- value-domain constraints to both freshly migrated and existing databases.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'paper_orders'::regclass AND conname = 'ck_paper_order_values'
    ) THEN
        ALTER TABLE paper_orders ADD CONSTRAINT ck_paper_order_values CHECK (
            side IN ('buy', 'sell')
            AND order_type IN ('market', 'limit', 'stop')
            AND time_in_force IN ('gtc', 'day', 'ioc')
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'paper_orders'::regclass AND conname = 'ck_paper_order_status'
    ) THEN
        ALTER TABLE paper_orders ADD CONSTRAINT ck_paper_order_status CHECK (
            status IN (
                'created', 'submitted', 'accepted', 'partially_filled', 'filled',
                'cancel_requested', 'cancelled', 'rejected', 'expired'
            )
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'paper_orders'::regclass AND conname = 'ck_paper_order_prices'
    ) THEN
        ALTER TABLE paper_orders ADD CONSTRAINT ck_paper_order_prices CHECK (
            (order_type = 'limit' AND limit_price > 0 AND stop_price IS NULL)
            OR (order_type = 'stop' AND stop_price > 0 AND limit_price IS NULL)
            OR (order_type = 'market' AND limit_price IS NULL AND stop_price IS NULL)
        );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'paper_risk_locks'::regclass AND conname = 'ck_paper_risk_lock_type'
    ) THEN
        ALTER TABLE paper_risk_locks ADD CONSTRAINT ck_paper_risk_lock_type CHECK (
            lock_type IN (
                'daily_loss_lock', 'drawdown_lock', 'emergency_lock', 'system_health_lock'
            )
        );
    END IF;
END $$;
