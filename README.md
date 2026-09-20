# TRADEROS

TRADEROS is a modular quantitative research and execution operating system for
Forex and equities. It is designed to discover, challenge, validate, deploy,
and monitor systematic strategies. It does not assume profitability, and it
does not treat an LLM as a trading authority.

## Status

Phase 9 — research validation framework — is complete, but the current
baseline empirical decision is **INVALID / not evaluable** because the
repository has no versioned, empirically eligible historical market dataset;
local ignored artifacts are present but their qualification is blocked. The
repository contains the Phase 1 data foundation, Phase 2 leakage-safe feature
engine, Phase 3 event-driven backtester, and four deterministic Phase 4
baselines: Forex trend, Forex breakout, equity momentum, and equity breakout.
It also contains a structured, versioned, stateless Phase 5 regime state with
trend, volatility, liquidity, and data/session dimensions, Phase 6 versioned
majority-vote signal fusion, and a Phase 7 fail-closed risk firewall. Fusion
creates an auditable quantity-free unified intent; the firewall returns only an
auditable veto or bounded quantity-free authorization. Neither sizes nor
executes a trade. These components are not validated production edges. Brokers,
paper orders now require a Phase 7 approval and use an offline, durable
SQLAlchemy paper engine. Live execution remains intentionally unimplemented.

The Phase 9 framework qualifies immutable datasets, locks temporal OOS
evaluation from candidate selection, records hypothesis/candidate/family and
experiment/result lineage, reuses the Phase 3 causal backtester, and provides
cost stress, dependence-aware uncertainty, FDR controls, canonical evidence
export, and paper-only promotion review.
Historical research replay uses the canonical Phase 2 → 5 → 4 → 6 → 7 →
Phase 8 sizing → Phase 3 path; architecture validation does not constitute
empirical strategy validation without an eligible immutable dataset.

The repository includes offline Dukascopy tick and Twelve Data OHLCV Forex
data-admission gates: raw source bytes are preserved immutably, imported
locally, normalized to UTC with declared timestamp format and bar semantics,
quality-audited against an explicit Forex calendar, and bound into a
deterministic manifest before research can be eligible. Twelve Data is an
independent OHLCV research source; Dukascopy remains the bid/ask, spread, and
microstructure source. The local runners never download or run strategies, and
no source is selected by backtest performance. See
[`docs/RESEARCH_VALIDATION.md`](docs/RESEARCH_VALIDATION.md) for the evidence
inventory and required data-validation gate.

## Safety defaults

- Default mode: `research`
- Live trading: disabled
- Real broker credentials: not included
- Risk firewall: deterministic, fail-closed, and quantity-free
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

Phase 1 has no network provider or CLI command. The deterministic local provider
is exercised through the ingestion service in tests. A PostgreSQL deployment
must apply `migrations/001_market_data_foundation.sql` before using the
SQLAlchemy store; Phase 1 does not connect to a live database automatically.

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
the delivery sequence, and the strategy, regime, and signal-fusion documents in
`docs/` for implemented contracts and safety requirements.
See [`docs/RESEARCH_ENGINE.md`](docs/RESEARCH_ENGINE.md) for the Phase 9
lifecycle, methodology, and limitations.

## Development quality gates

The CI workflow runs the same core checks expected locally:

```bash
ruff check .
mypy src
pytest
```

No phase is complete until its implementation, tests, documentation, and
quality checks agree.
