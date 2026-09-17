# Roadmap

## Phase 0 — Foundation

Repository governance, modular package boundaries, configuration, logging,
testing, linting, typing, CI, and documentation. No trading logic.

## Phase 1 — Data engine — complete

Provider interfaces, UTC-normalized market-data models, validation, storage,
retrieval, calendars, lineage, and data-quality tests. Redis caching remains
deferred until a measured use case exists.

## Phase 2 — Feature engine — complete

Leakage-safe versioned returns, momentum, moving averages, volatility, ATR,
range, breakout, RSI, equity volume features, explicit availability timestamps,
lineage, storage boundary, and future-data invariance tests.

## Phase 3 — Backtesting — complete

Offline, reproducible event-driven orders/fills, explicit next-bar timing,
market/limit/stop execution, costs, spread, slippage, Decimal portfolio and FX
accounting, trade ledger, equity curve, experiment identity, and performance
metrics. Risk-firewall integration and live/paper execution remain later work.

## Phase 4 — Strategies — complete

Versioned signal-only strategy contract, immutable decision-time context,
parameter-aware feature provenance, registry validation, and four
independently testable deterministic baselines: Forex trend, Forex breakout,
equity momentum, and equity breakout. Each baseline is integrated with the
Phase 3 engine using explicit next-bar execution and cost models. No parameter
optimization or profitability claim is included.

## Phase 5 — Regime engine — complete

Interpretable, versioned, causal, stateless market-state classification with
independent trend, volatility, liquidity, and data/session dimensions, plus
offline descriptive stability analysis. It is observation-only: filtering,
strategy coupling, and trade effects are deferred to Phase 6.

## Phase 6 — Signal fusion — complete

Deterministic, versioned strategy-signal normalization, strict causal alignment,
regime compatibility, duplicate-safe conflict resolution, and auditable
quantity-free unified intents. The baseline is explicit unweighted majority
voting; it does not use risk, costs, correlation, performance optimization, or
learned weights.

## Phase 7 — Risk firewall — complete

Fail-closed, versioned deterministic pre-trade gate with kill switch, health,
freshness/quote, regime, exposure, leverage, capital/margin, daily-loss,
drawdown-lock, concurrent-intent, and duplicate-reservation checks. It returns
only auditable bounded quantity-free risk authorizations or vetoes; portfolio
sizing, persistent reservations/locks, paper trading, and execution remain
later phases.

## Phase 8 — Paper trading — complete

Offline durable account, sizing, orders, fills, reservations, locks, recovery,
and audit trail. Every paper order consumes a Phase 7 risk decision; no broker
or network adapter is implemented.

## Phase 9 — Research and validation — complete (empirical decision invalid)

Immutable dataset/experiment/result lineage, fail-closed qualification,
candidate/family lineage, chronological train/validation/test controls,
locked-OOS protection, cost stress, deterministic Monte Carlo/FDR helpers, and
machine-readable evidence/paper-only promotion review are implemented. No versioned
historical market dataset is present in the repository, so the four baseline
strategies have not been empirically evaluated and are INVALID / not evaluable;
this is not a profitability claim or a strategy kill decision. See
`docs/RESEARCH_ENGINE.md` and `docs/RESEARCH_VALIDATION.md`.

Canonical architecture replay is also implemented: causal Phase 2 observations
flow through Phase 5 regime detection, Phase 4 strategy decisions, Phase 6
fusion, Phase 7 risk evaluation, Phase 8 pure sizing, and Phase 3 execution.
This validates software boundaries with fixtures only; empirical eligibility
remains blocked without production-quality immutable market data.

## Phase 10 — Dashboard

Operational views for portfolio, markets, strategies, trades, research, and
risk status.

## Phase 11 — Broker integration

Broker adapters behind the execution interface. Live remains disabled until the
formal validation gate passes.

## Phase 12 — Validation gate

Formal evidence report covering backtests, walk-forward, out-of-sample,
robustness, stress, costs, drawdown, correlation, regimes, paper trading, and
known weaknesses. Inadequate evidence means live trading stays disabled.
