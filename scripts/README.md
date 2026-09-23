# Scripts

## Phase 8 PostgreSQL verification

`apply_postgres_migrations.sh` is the fresh-disposable ordered migration
runner for migrations 001 through 013. It is used by CI and by
`verify_phase8_postgres.sh`, which then runs the real PostgreSQL Phase 8,
governance, and PAPER worker integration suites. The chain
also restores the PAPER worker’s durable lease, status, trace, data-identity,
freshness, and account-claimant schema. It requires a disposable database and
two caller-supplied environment variables: a libpq DSN for `psql` and its
matching SQLAlchemy URL. Neither values nor credentials belong in the script,
repository, or `.env.example`. Set `TRADEROS_PYTHON` only when the interpreter
is not available as `python` (for example, `TRADEROS_PYTHON=.venv/bin/python`).

The runner requires `TRADEROS_POSTGRES_FRESH=1` and refuses any database that
already contains a public schema table. Both callers here (CI and
`verify_phase8_postgres.sh`) set it explicitly for their disposable databases.
The only other permitted use is the one-time initialization of a new, empty
first-deployment database, run by an operator as described in
`docs/RENDER_DEPLOYMENT.md`; the worker never invokes it on start or restart.
It must never be pointed at an existing database. Existing-database upgrades
are not yet supported: they need a separate deployment migration mechanism with
backup, lock, and migration-ledger controls. Migration 013 is the current
schema boundary.

Operational and reproducibility scripts belong here. Scripts must be safe by
default and must not contain credentials or silently enable live trading.

## APEX Milestone 5

`run_apex_milestone5.py` runs the explicit local-data inventory, immutable
qualification, source-snapshot identity, protocol freeze, and fail-closed
research verdict. It requires `--dataset-id` and does not download data or run
strategy code after a data-gate failure. See `docs/APEX_MILESTONE5.md`.
