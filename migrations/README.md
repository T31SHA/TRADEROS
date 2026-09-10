# Database migrations

Phase 1 adds `001_market_data_foundation.sql` for the PostgreSQL production
schema. It creates instruments, data sources, dataset versions, market data,
ingestion runs, and quality events with foreign keys, checks, uniqueness, and
time-series indexes. The SQLAlchemy schema in
`src/traderos/database/schema.py` is kept aligned for SQLite integration tests
and application-managed schema creation.

The bar identity includes `symbol`, `timeframe`, `timestamp`, `source_id`, and
`adjustment_policy`, so raw and adjusted observations cannot silently collide.

Manual schema edits are not an accepted deployment path. A migration runner
must apply this file in deployment automation before a PostgreSQL store is
used.
