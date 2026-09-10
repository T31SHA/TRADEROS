# TRADEROS

TRADEROS is a modular quantitative research and execution operating system for
Forex and equities. It is designed to discover, challenge, validate, deploy,
and monitor systematic strategies. It does not assume profitability, and it
does not treat an LLM as a trading authority.

## Status

Phase 0 — foundation — is complete. The repository currently contains the
governance documents, Python package boundaries, validated application
configuration, structured logging foundation, tests, static-analysis tooling,
and CI configuration. Trading, broker connectivity, persistence, and market
data ingestion are intentionally not implemented yet.

## Safety defaults

- Default mode: `research`
- Live trading: disabled
- Real broker credentials: not included
- Risk engine: fail-closed by design in the future architecture
- No daily profit target is encoded as a trading rule

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
cp .env.example .env

pytest
ruff check .
mypy src
```

Configuration is read from environment variables or `.env`. `.env` is ignored
by git; only safe placeholders belong in `.env.example`.

## Repository map

- `src/traderos/` — modular monolith packages and shared core contracts
- `apps/` — application entry-point boundaries for API, dashboard, and worker
- `tests/` — unit, integration, and system test boundaries
- `migrations/` — database migration boundary; no manual schema setup
- `experiments/` — reproducible research artifacts
- `security/` — security checks and policy artifacts
- `docker/` — container definitions and operational assets

See `ARCHITECTURE.md` for boundaries and dependency direction, `ROADMAP.md` for
the delivery sequence, and the policy documents for safety requirements.

## Development quality gates

The CI workflow runs the same core checks expected locally:

```bash
ruff check .
mypy src
pytest
```

No phase is complete until its implementation, tests, documentation, and
quality checks agree.
