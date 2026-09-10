# Strategy Policy

Strategies are research artifacts, not autonomous authorities.

## Lifecycle

Every strategy must progress through:

```text
research → backtest → cost-aware backtest → walk-forward
→ out-of-sample → robustness/stress → paper trading
→ human approval → controlled live deployment
```

No component may bypass a stage or represent an unvalidated result as
production-ready.

## Implemented Phase 4 interface

Phase 4 strategies provide explicit `strategy_id` and `strategy_version`,
supported asset/timeframe scope, required versioned and parameterized features,
warmup requirements, strict parameters, and a deterministic `on_bar` decision.
The decision contains a typed signal and zero or more order intents. An adapter
translates intents to Phase 3 orders; the strategy itself never mutates cash,
positions, fills, the ledger, or equity and never calls a broker.

The initial baselines are deliberately simple and use a deterministic unit
target position. Confidence is not fabricated for these rules; a deterministic
score is used only where it directly represents rule distance. Expected return,
risk sizing, protective levels, regimes, and fusion are deferred to later
domains.

## Research validity

Features, labels, training data, signal generation, sizing, and simulations must
use only information available at the decision timestamp. Time-series
validation must not randomly shuffle observations. Costs, spread, slippage,
latency, liquidity, sessions, corporate actions, and delistings must be
represented where relevant.

## Promotion

Promotion requires evidence from out-of-sample, robustness, stress, and paper
trading results. A scorecard must show its underlying evidence, including
stability, drawdown, parameter sensitivity, regime diversity, costs, trade
count, and implementation complexity. No single scalar score proves an edge.

Phase 4 is not a promotion gate. Its four baselines are research fixtures that
must still pass cost-aware, walk-forward, out-of-sample, robustness, stress,
paper-trading, and human-approval stages before any production consideration.
