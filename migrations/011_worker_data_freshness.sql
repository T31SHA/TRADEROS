-- Persist the latest admitted market-bar timestamp for operational status.
-- This adds observability only; it does not add execution or live capability.

ALTER TABLE worker_states
    ADD COLUMN IF NOT EXISTS data_latest_at TIMESTAMPTZ;
