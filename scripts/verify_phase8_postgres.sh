#!/usr/bin/env bash
# Run against a disposable PostgreSQL database only. No credentials are stored here.
set -euo pipefail

: "${TRADEROS_POSTGRES_TEST_DSN:?set a disposable PostgreSQL libpq DSN}"
: "${TRADEROS_POSTGRES_TEST_URL:?set matching SQLAlchemy URL}"
python_bin="${TRADEROS_PYTHON:-python}"

scripts/apply_postgres_migrations.sh
TRADEROS_POSTGRES_TEST_URL="$TRADEROS_POSTGRES_TEST_URL" \
  "$python_bin" -m pytest -q \
    tests/integration/test_paper_trading_postgres.py \
    tests/integration/research/test_strategy_governance_postgres.py \
    tests/integration/test_worker_postgres.py
