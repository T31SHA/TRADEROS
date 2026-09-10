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

## Interface expectations

Each strategy will provide metadata, required features, a deterministic signal
proposal, confidence, expected return/risk, protective levels, holding-period
estimate, applicable regimes, and a version. It must never place an order or
call a broker.

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
