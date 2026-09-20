-- Persist the workload that owns an account-scoped coordination lease so
-- stale status rows cannot appear healthy under a replacement workload.

ALTER TABLE worker_leases
    ADD COLUMN IF NOT EXISTS claimed_workload_id VARCHAR(128);

UPDATE worker_leases
SET claimed_workload_id = workload_id
WHERE claimed_workload_id IS NULL;

ALTER TABLE worker_leases
    ALTER COLUMN claimed_workload_id SET NOT NULL;
