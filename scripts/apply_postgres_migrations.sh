#!/usr/bin/env bash
# Apply migrations 001-013 to a fresh, empty PostgreSQL database in one explicit,
# ordered sequence: a disposable CI/verification database or the new, empty first
# deployment database (docs/RENDER_DEPLOYMENT.md). It is never an upgrade path and
# never runs on worker start or restart.
# No credentials or live-execution capability belong in this script.
set -euo pipefail

: "${TRADEROS_POSTGRES_TEST_DSN:?set the libpq DSN of a fresh, empty PostgreSQL database}"
: "${TRADEROS_POSTGRES_FRESH:?set to 1 only for a fresh, empty database}"

if [[ "$TRADEROS_POSTGRES_FRESH" != "1" ]]; then
  echo "refusing migration initialization without TRADEROS_POSTGRES_FRESH=1" >&2
  exit 2
fi

existing_objects="$(psql "$TRADEROS_POSTGRES_TEST_DSN" -Atqc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
if [[ "$existing_objects" != "0" ]]; then
  echo "refusing fresh migration initialization against a non-empty PostgreSQL database" >&2
  exit 2
fi

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
  migrations/013_execution_boundary_hardening.sql
)

for migration in "${migrations[@]}"; do
  psql "$TRADEROS_POSTGRES_TEST_DSN" \
    -v ON_ERROR_STOP=1 \
    -f "$repo_root/$migration"
done
