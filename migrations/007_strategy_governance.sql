-- Milestone 2: immutable versioned strategy artifacts and governed lifecycle.
-- This migration adds no order, broker, live, or capital-allocation capability.

CREATE TABLE IF NOT EXISTS strategy_artifacts (
    artifact_hash VARCHAR(128) PRIMARY KEY,
    strategy_id VARCHAR(128) NOT NULL,
    strategy_version VARCHAR(128) NOT NULL,
    schema_version VARCHAR(64) NOT NULL,
    author VARCHAR(128) NOT NULL,
    canonical_payload JSONB NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_strategy_artifacts_identity UNIQUE (strategy_id, strategy_version)
);

CREATE TABLE IF NOT EXISTS strategy_evidence (
    evidence_id VARCHAR(128) PRIMARY KEY,
    evidence_hash VARCHAR(128) NOT NULL UNIQUE,
    artifact_hash VARCHAR(128) NOT NULL REFERENCES strategy_artifacts(artifact_hash),
    experiment_id VARCHAR(128) NOT NULL,
    result_hash VARCHAR(128) NOT NULL,
    canonical_payload JSONB NOT NULL,
    attached_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_approvals (
    approval_id VARCHAR(128) PRIMARY KEY,
    approval_hash VARCHAR(128) NOT NULL UNIQUE,
    artifact_hash VARCHAR(128) NOT NULL REFERENCES strategy_artifacts(artifact_hash),
    evidence_id VARCHAR(128) NOT NULL REFERENCES strategy_evidence(evidence_id),
    target_stage VARCHAR(32) NOT NULL CHECK (target_stage IN ('shadow', 'paper')),
    actor_id VARCHAR(128) NOT NULL,
    policy_id VARCHAR(128) NOT NULL,
    policy_version VARCHAR(64) NOT NULL,
    canonical_payload JSONB NOT NULL,
    approved_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_evidence_revocations (
    revocation_id VARCHAR(128) PRIMARY KEY,
    evidence_id VARCHAR(128) NOT NULL REFERENCES strategy_evidence(evidence_id),
    actor_id VARCHAR(128) NOT NULL,
    reason TEXT NOT NULL,
    revoked_at TIMESTAMPTZ NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS strategy_approval_revocations (
    revocation_id VARCHAR(128) PRIMARY KEY,
    approval_id VARCHAR(128) NOT NULL REFERENCES strategy_approvals(approval_id),
    actor_id VARCHAR(128) NOT NULL,
    reason TEXT NOT NULL,
    revoked_at TIMESTAMPTZ NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS strategy_lifecycle_state (
    strategy_id VARCHAR(128) NOT NULL,
    strategy_version VARCHAR(128) NOT NULL,
    artifact_hash VARCHAR(128) NOT NULL REFERENCES strategy_artifacts(artifact_hash),
    stage VARCHAR(32) NOT NULL CHECK (
        stage IN ('candidate', 'research', 'validation', 'shadow', 'paper', 'canary', 'active', 'retired')
    ),
    health VARCHAR(32) NOT NULL CHECK (
        health IN ('unknown', 'healthy', 'weakening', 'degraded', 'failed')
    ),
    revision BIGINT NOT NULL CHECK (revision >= 0),
    disabled BOOLEAN NOT NULL DEFAULT FALSE,
    disable_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL,
    last_event_id VARCHAR(128),
    PRIMARY KEY (strategy_id, strategy_version)
);

CREATE TABLE IF NOT EXISTS strategy_lifecycle_events (
    event_id VARCHAR(128) PRIMARY KEY,
    strategy_id VARCHAR(128) NOT NULL,
    strategy_version VARCHAR(128) NOT NULL,
    artifact_hash VARCHAR(128) NOT NULL REFERENCES strategy_artifacts(artifact_hash),
    event_type VARCHAR(32) NOT NULL CHECK (event_type IN ('transition', 'disable')),
    previous_stage VARCHAR(32) NOT NULL,
    resulting_stage VARCHAR(32) NOT NULL,
    expected_revision BIGINT NOT NULL CHECK (expected_revision >= 0),
    resulting_revision BIGINT NOT NULL CHECK (resulting_revision > 0),
    actor_id VARCHAR(128) NOT NULL,
    policy_id VARCHAR(128) NOT NULL,
    policy_version VARCHAR(64) NOT NULL,
    evidence_id VARCHAR(128) REFERENCES strategy_evidence(evidence_id),
    approval_id VARCHAR(128) REFERENCES strategy_approvals(approval_id),
    reason_code VARCHAR(64) NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    correlation_id VARCHAR(128) NOT NULL,
    CONSTRAINT uq_strategy_lifecycle_idempotency
        UNIQUE (strategy_id, strategy_version, idempotency_key),
    CONSTRAINT uq_strategy_lifecycle_revision
        UNIQUE (strategy_id, strategy_version, resulting_revision)
);

CREATE INDEX IF NOT EXISTS ix_strategy_lifecycle_events_identity
    ON strategy_lifecycle_events(strategy_id, strategy_version, resulting_revision);
CREATE INDEX IF NOT EXISTS ix_strategy_lifecycle_events_artifact
    ON strategy_lifecycle_events(artifact_hash, occurred_at);
