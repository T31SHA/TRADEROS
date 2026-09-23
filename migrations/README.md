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

Migration 005 adds `market_data.quote_timestamp`, preserving when an
aggregated bid/ask observation became available. Closing observations are not
valid executable quotes for an earlier bar opening.

Migration 006 adds `paper_accounts.state_revision`, a monotonic account-state
identity used to reject approvals built from an older coherent snapshot during
same-timestamp or concurrent paper-trading activity.

Migration 007 adds the governed strategy boundary: immutable content-addressed
artifacts, exact evidence and approval attachments, append-only revocations and
lifecycle events, and a revisioned current-state projection.  The artifact
hash is an integrity check, not authentication.  Lifecycle state is scoped to
one strategy version; it cannot submit orders, change risk policy, allocate
capital, or enable live execution.

Migration 008 adds the durable PAPER worker workload lease, status projection,
and append-only cycle outcomes. It adds no provider, broker, live, or order
bypass capability. The worker-specific additive migrations 009 through 012 add
decision traces, dataset content identity, admitted-bar freshness, and the
claimed workload identity used by account-scoped leases.

Migration 009 adds the append-only PAPER worker cycle decision trace and its
latest-status projection. The trace records stage outcomes and no-trade
reasons; it does not authorize execution or live trading.

Migration 010 adds the optional immutable content hash to dataset versions.
The worker uses it to require an operational dataset identity to match a
governed strategy artifact before any paper activity.

Migration 011 adds the latest admitted-bar timestamp used by the PAPER worker
status command to report data freshness. It adds no execution or live
capability.

Migration 012 adds the claimed workload identity to worker coordination leases.
This lets account-scoped status observation distinguish an expired workload
from a stale status row whose account is now controlled by another workload.

Migration 013 adds the monotonically increasing lease fence generation and
persists the original trade-level authorization bounds on each paper order.
Existing orders retain their historical rows; legacy bound values are zero and
cannot authorize new operational fills until an explicit compatible migration
policy is defined.

Manual schema edits are not an accepted deployment path. The repository
runner is fresh-empty-database-only: it requires
`TRADEROS_POSTGRES_FRESH=1` and refuses a database with any existing public
schema table. Existing-database upgrades are a separate deployment operation
and must apply the numbered files in numeric order through the deployment
migration mechanism, with its backup, lock, and migration-ledger controls;
the runner must not be used for that operation. Existing-database upgrades are
not yet supported. The runner's only non-disposable use is the one-time
initialization of a new, empty first-deployment database
(`docs/RENDER_DEPLOYMENT.md`).
