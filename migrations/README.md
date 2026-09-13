# Database migrations

Phase 1 adds `001_market_data_foundation.sql` for the PostgreSQL production
schema. It creates instruments, data sources, dataset versions, market data,
ingestion runs, and quality events with foreign keys, checks, uniqueness, and
time-series indexes. The SQLAlchemy schema in
`src/traderos/database/schema.py` is kept aligned for SQLite integration tests
and application-managed schema creation.

The bar identity includes `symbol`, `timeframe`, `timestamp`, `source_id`, and
`adjustment_policy`, so raw and adjusted observations cannot silently collide.

Phase 8 adds `002_paper_trading.sql`. It creates durable, PostgreSQL-authoritative
paper accounts, projections, orders, fills, reservations, risk locks, and
append-only audit records. `003_paper_trading_hardening.sql` adds durable value
domain constraints without rewriting the already-released 002 migration.
`004_paper_risk_snapshots.sql` adds the append-only, timestamped account view
used as the durable hand-off to the next Phase 7 evaluation. Paper
order/reservation creation and fill/projection updates are each one database
transaction; no migration contains a broker URL, credential, or live execution
capability.

Manual schema edits are not an accepted deployment path. A migration runner
must apply these files in numeric order in deployment automation before a
PostgreSQL store is used.
