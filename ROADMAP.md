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

## Phase 5 — Regime engine

Interpretable regime detection, filtering, and regime attribution.

## Phase 6 — Signal fusion

Deterministic combination of strategy signals using confidence, risk, regime,
cost, and correlation evidence.

## Phase 7 — Risk firewall

Portfolio-level limits, fail-closed behavior, kill switch, and adversarial tests.

## Phase 8 — Paper trading

The same signal → risk → portfolio → execution path using a paper adapter.

## Phase 9 — Research and learning

Experiment registry, validation gates, model/strategy versions, attribution,
degradation analysis, and proposal-only research assistance.

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
