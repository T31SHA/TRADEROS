# Backtesting Engine

Phase 3 provides an offline, deterministic, event-driven replay engine. It
answers execution and accounting questions under explicit assumptions; it does
not claim that any strategy is profitable.

## Event contract

Phase 1 bars use a bar-start UTC timestamp. A bar beginning at `t` represents
the interval `[t, t + timeframe)`. Its completed OHLC values become observable
at `t + timeframe`. The engine therefore does the following for every
chronologically ordered bar:

```text
bar t opens        → execute orders submitted after the prior decision
bar t completes    → mark positions at the close
decision at t+Δ    → strategy sees the completed bar and may submit orders
next bar opens     → earliest fill for those orders
```

There is no implicit same-bar execution. The only implemented execution policy
is `NEXT_BAR_OPEN`. A strategy receives a `StrategyContext` containing the
current completed bar, currently visible Phase 2 feature observations, and the
portfolio snapshot. It does not receive future bars or future features.

The input sequence is normalized into timestamp/symbol/source order, while
duplicate `(symbol, timestamp)` events, out-of-range bars, mixed adjustment
policies, wrong timeframes, and invalid calendar events are rejected. Missing
bars are never synthesized. Optional calendar-backed gap rejection is explicit.

## Orders and lifecycle

The initial order types are market, limit, and stop. Orders transition through
validated states:

```text
CREATED → SUBMITTED → ACCEPTED → PARTIALLY_FILLED → FILLED
                         ├──────→ CANCELLED / REJECTED / EXPIRED
```

Invalid transitions raise `InvalidOrderTransition`. Order IDs are unique within
a replay. Orders submitted for a different instrument than the current event
are rejected; this keeps the Phase 3 strategy boundary event-local and avoids
cross-asset timing ambiguity. Cross-asset portfolio accounting is supported.

## Execution assumptions

Market orders use the next bar open as the reference price. If bid/ask is
present, buys execute at ask and sells at bid. Otherwise a configured absolute
spread is split around the reference. Fixed adverse slippage is then applied.

Limit orders trigger only if the bar range touches the limit. The fill cannot
be worse than the limit; a spread that would violate that constraint leaves the
order unfilled. Stop orders trigger on the bar range and use the worse of the
stop and next open, so a gap through a stop is not filled at the stop price.

OHLC data does not reveal intrabar path. If more than one conditional order is
triggered by one bar, the default `REJECT` policy rejects all of them. The
optional `FIRST_ORDER_ID` policy deterministically selects the lexicographically
first order. This is a conservative assumption, not a reconstruction of
intrabar truth.

`GTC`, `DAY`, and `IOC` lifetimes are represented. IOC orders expire when not
filled in their eligible event; DAY orders do not cross a calendar date. A
configured maximum fill quantity supports deterministic partial fills.

## Accounting

Portfolio accounting uses `Decimal` values. Positions are signed instrument
quantities: positive is long and negative is short. Cash changes by signed
settlement notional and fees. Equity is always derived as:

```text
cash + marked-to-market net position value
```

Opening, increasing, reducing, closing, and reversing positions are handled in
one accounting implementation. Realized P&L is created only for the quantity
closed by a fill. Unrealized P&L is marked from the current market price.
Entry and exit fees are allocated to closed `TradeRecord` slices, while the
account also retains cumulative fees.

For equities, settlement uses the instrument trading currency. For FX, the
quote currency is the settlement currency. Same-currency accounts work without
configuration. Other currency conversions require an explicit
`StaticCurrencyConverter` (or a later provider-backed converter); no exchange
rate is invented. The current static converter is intentionally time-invariant
and is a bounded Phase 3 assumption.

Every fill produces a `LedgerEntry` containing the order/fill, cash before and
after, position quantity before and after, realized P&L, fees, and equity after
the fill. The equity curve is produced from portfolio snapshots rather than
trade-return shortcuts.

## Metrics and benchmarks

Metrics are calculated only from the recorded equity curve and closed trades.
They include total return, CAGR, periodic returns, annualized volatility,
maximum drawdown, drawdown duration, recovery time, Sharpe, Sortino, Calmar,
win/loss statistics, profit factor, expectancy, holding period, turnover, and
fees. Annualization is configurable and is not silently assumed to be 252.
Metrics with insufficient observations are `None`.

`BuyAndHoldBenchmark` is an offline first-bar-open benchmark helper. It is not
part of the strategy or order path.

## Reproducibility and identity

`BacktestConfig` includes the dataset version, universe, timeframe, date range,
capital, account currency, strategy/version, feature versions, raw/adjusted
policy, costs, spread, slippage, execution policy, ambiguity policy, calendar
identity, and initial positions. Its canonical JSON representation is hashed
with SHA-256 to form `experiment_id`. Timestamps of execution are not included
as an unstable identity input.

Repeating the same bars, causal features, strategy version, and configuration
produces the same orders, fills, ledger, equity curve, metrics, and experiment
identity. The test suite attacks this guarantee with future-price mutation,
future append, same-bar execution, stop-gap, ambiguity, fee, slippage,
multi-asset, ordering, and duplicate-order cases.

## Safety boundary and limitations

The engine is offline only. It has no broker, network, paper-trading, live
trading, risk-firewall, signal-fusion, optimization, or model-promotion path.
The Phase 7 Risk Firewall will later sit between strategy order proposals and
execution. Multi-timeframe strategy state, corporate-action event replay,
intrabar data, margin, financing/rollover, and dynamic FX conversion are not
implemented. These limitations must be resolved or explicitly modeled before
using results for a deployment decision.
