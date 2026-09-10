# Strategy Engine

Phase 4 adds a deterministic strategy layer between causal feature
observations and the Phase 3 event-driven backtester. It establishes baseline
research behavior; it does not establish a profitable edge or authorize live
trading.

## Architecture

```text
validated bars → causal FeatureObservation values
    → immutable StrategyContext
    → StrategyResult (StrategySignal + OrderIntent values)
    → BacktestStrategyAdapter
    → Phase 3 Order state machine and execution simulator
```

Strategies do not mutate cash, positions, fills, the ledger, or the equity
curve. The adapter translates intents into Phase 3 orders. Execution remains
responsible for timing, costs, fills, and accounting.

## Information boundary

Phase 1 timestamps identify the opening instant of a bar. A strategy receives a
completed bar at `bar.timestamp + timeframe.duration`. The context contains
only feature observations whose observation timestamp is no later than the
bar and whose availability timestamp is no later than the decision timestamp.
The Phase 3 execution policy is `NEXT_BAR_OPEN`, so an order created from a
decision at `t` cannot fill during the bar that produced the decision.

The context is immutable from the strategy's perspective. Feature requests are
matched by feature name, version, and parameterization. This is important for
strategies such as trend following that require two instances of the same
feature definition with different windows.

Cross-instrument and unsupported-timeframe feature observations are rejected.
Multi-timeframe and cross-instrument strategy dependencies are not implemented
in this phase.

## Contract and provenance

`Strategy` exposes `strategy_id`, `strategy_version`, immutable parameter
configuration, metadata, and `on_bar(context)`. Metadata declares supported
asset classes, supported timeframes, required versioned/parameterized
features, warmup bars, and the parameter schema.

`StrategySignal` records the deterministic signal identity, direction, decision
timestamp, instrument, timeframe, reason, and feature provenance. `OrderIntent`
expresses a desired target-position change without submitting an order. The
adapter verifies that signal and intent identity, instrument, timestamp, and
timeframe agree with the decision context before creating a Phase 3 order.

`StrategyRegistry` is instance-scoped and rejects duplicate `(strategy_id,
strategy_version)` identities, metadata mismatches, and unavailable feature
dependencies when a feature registry is supplied.

## Baseline strategies

All baselines use deterministic unit target quantities by default. Position
sizing, leverage, risk limits, regime filters, and signal fusion are outside
Phase 4.

| Strategy ID | Asset class | Timeframes | Required features | Rule |
| --- | --- | --- | --- | --- |
| `forex_trend_following.v1` | Forex | 15m, 1h, 4h, 1d | EMA v1 at `fast_period` and `slow_period` | Long when fast EMA is above slow EMA; short when below; optional strength threshold can make the result flat. |
| `forex_breakout.v1` | Forex | 15m, 1h, 4h, 1d | Previous-window high/low distance v1 | Long above the previous high or short below the previous low, after the optional buffer. |
| `equity_momentum.v1` | Equity, ETF | 1h, 1d | Rolling return v1 at `lookback` | Long when the causal rolling return exceeds `minimum_momentum`; otherwise flat. |
| `equity_breakout.v1` | Equity, ETF | 1h, 1d | Previous-window high distance v1 | Long when the completed close exceeds the previous high after the optional buffer; otherwise flat. |

Periods are positive integers, trend fast period must be less than slow period,
thresholds are finite, and parameter models reject extra fields and implicit
type coercion. Insufficient feature history produces an explicit `HOLD` signal
with no order intent. A neutral or threshold-failing rule produces `FLAT` and
will close an existing target position through the adapter when appropriate.

## Causality and tests

The strategy suite tests:

- future mutation and future append invariance;
- previous-window breakout thresholds that exclude the current bar;
- feature availability and completed-bar timing;
- warmup and missing-feature behavior;
- instrument and timeframe isolation;
- deterministic signal IDs and replay;
- strict parameter and registry validation;
- signal/order provenance at the Phase 3 adapter boundary; and
- real end-to-end strategy → order → fill → portfolio → ledger → equity tests
  with spread, slippage, and commissions.

The four strategy integration tests use the actual Phase 3 `BacktestEngine`;
there is no separate strategy-specific simulator.

## Deliberate exclusions

Phase 4 does not implement optimization, parameter selection, ML, regime
detection, signal fusion, portfolio optimization, risk-firewall enforcement,
broker connectivity, paper trading, or live trading. These baselines are
research fixtures for testing architecture and honest simulation, not claims
of profitability. Future promotion must follow the complete validation
lifecycle and human approval policy.

## Limitations

The current context exposes only one instrument and one declared timeframe at a
time. Strategies do not receive a dynamic universe, future corporate actions,
future benchmark data, or an unrestricted historical dataset. The simple unit
target is intentionally not a production sizing method. A later phase must add
regime and portfolio/risk controls without weakening these information and
provenance boundaries.
