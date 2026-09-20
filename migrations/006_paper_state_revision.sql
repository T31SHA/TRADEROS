-- Foundation hardening: monotonic account identity for same-timestamp races.

ALTER TABLE paper_accounts
    ADD COLUMN IF NOT EXISTS state_revision BIGINT NOT NULL DEFAULT 0;
