#!/usr/bin/env bash
# Run against a disposable PostgreSQL database only. No credentials are stored here.
set -euo pipefail

: "${TRADEROS_POSTGRES_TEST_DSN:?set a disposable PostgreSQL libpq DSN}"
: "${TRADEROS_POSTGRES_TEST_URL:?set matching SQLAlchemy URL}"
python_bin="${TRADEROS_PYTHON:-python}"

psql "$TRADEROS_POSTGRES_TEST_DSN" -v ON_ERROR_STOP=1 -f migrations/001_market_data_foundation.sql
psql "$TRADEROS_POSTGRES_TEST_DSN" -v ON_ERROR_STOP=1 -f migrations/002_paper_trading.sql
psql "$TRADEROS_POSTGRES_TEST_DSN" -v ON_ERROR_STOP=1 -f migrations/003_paper_trading_hardening.sql
TRADEROS_POSTGRES_TEST_URL="$TRADEROS_POSTGRES_TEST_URL" \
  "$python_bin" -m pytest -q tests/integration/test_paper_trading_postgres.py
