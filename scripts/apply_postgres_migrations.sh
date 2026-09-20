#!/usr/bin/env bash
# Apply the disposable PostgreSQL schema in one explicit, ordered sequence.
# No credentials or live-execution capability belong in this script.
set -euo pipefail

: "${TRADEROS_POSTGRES_TEST_DSN:?set a disposable PostgreSQL libpq DSN}"

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
migrations=(
  migrations/001_market_data_foundation.sql
  migrations/002_paper_trading.sql
  migrations/003_paper_trading_hardening.sql
  migrations/004_paper_risk_snapshots.sql
  migrations/005_quote_availability.sql
  migrations/006_paper_state_revision.sql
  migrations/007_strategy_governance.sql
  migrations/008_paper_worker.sql
  migrations/009_paper_worker_decision_trace.sql
  migrations/010_dataset_content_identity.sql
  migrations/011_worker_data_freshness.sql
  migrations/012_worker_lease_claimant.sql
)

for migration in "${migrations[@]}"; do
  psql "$TRADEROS_POSTGRES_TEST_DSN" \
    -v ON_ERROR_STOP=1 \
    -f "$repo_root/$migration"
done
