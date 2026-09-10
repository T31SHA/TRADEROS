# Roadmap

## Phase 0 — Foundation

Repository governance, modular package boundaries, configuration, logging,
testing, linting, typing, CI, and documentation. No trading logic.

## Phase 1 — Data engine — complete

Provider interfaces, UTC-normalized market-data models, validation, storage,
retrieval, calendars, lineage, and data-quality tests. Redis caching remains
deferred until a measured use case exists.

## Phase 2 — Feature engine

Leakage-safe returns, momentum, volatility, ATR, trend, and breakout features
with explicit timestamp tests.

## Phase 3 — Backtesting

Reproducible event-driven orders/fills, costs, slippage, latency assumptions,
portfolio simulation, and performance metrics.

## Phase 4 — Strategies

Four independently testable strategies: Forex trend, Forex breakout, equity
momentum, and equity breakout.

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
