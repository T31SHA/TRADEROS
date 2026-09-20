-- Durable stage trace for PAPER worker cycles and the latest status projection.
-- This adds observability only; it does not add execution or live capability.

ALTER TABLE worker_states
    ADD COLUMN IF NOT EXISTS last_trace JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE worker_cycles
    ADD COLUMN IF NOT EXISTS decision_trace JSONB NOT NULL DEFAULT '[]'::jsonb;
