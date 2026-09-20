-- Bind operational data admission to the immutable content identity used by research.
-- This records lineage only; it does not qualify data or enable execution.

ALTER TABLE dataset_versions
    ADD COLUMN IF NOT EXISTS dataset_hash VARCHAR(128);
