# Scripts

## Phase 8 PostgreSQL verification

`verify_phase8_postgres.sh` applies migrations 001, 002, and 003 and runs the real
PostgreSQL Phase 8 integration suite. It requires a disposable database and
two caller-supplied environment variables: a libpq DSN for `psql` and its
matching SQLAlchemy URL. Neither values nor credentials belong in the script,
repository, or `.env.example`. Set `TRADEROS_PYTHON` only when the interpreter
is not available as `python` (for example, `TRADEROS_PYTHON=.venv/bin/python`).

Operational and reproducibility scripts belong here. Scripts must be safe by
default and must not contain credentials or silently enable live trading.
